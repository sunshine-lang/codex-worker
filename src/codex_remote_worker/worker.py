from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
import re
import traceback

from .common import JobError, Redactor, now, TERMINAL, write_json
from .files import ensure_space, publish, verify_result
from .project_archive import archive_job


class Worker:
    def __init__(self, settings, store, runner):
        self.settings, self.store, self.runner = settings, store, runner
        self.shutdown_requested = False
        self.blocked = None
        self.task = None
        self.wakeup = asyncio.Event()
        self.redactor = Redactor(settings.secret_values(), [settings.data_root, settings.codex_home])

    async def start(self):
        for job in self.store.unfinished():
            if not await asyncio.to_thread(self.runner.recover, job):
                self._cleanup_failed(job['job_id'])
                continue
            if job['status'] == 'checking' and job.get('execution', {}).get('exit_code') == 0:
                await self._check_and_finish(job)
            elif job['status'] == 'cancelling':
                await self._finish(job['job_id'], 'cancelled')
            else:
                await self._finish(job['job_id'], 'interrupted',
                                   error={'code': 'WORKER_RESTARTED', 'message': '服务重启中断了任务；未自动重跑。'})
        self.task = asyncio.create_task(self._loop())

    async def ready(self):
        if self.blocked:
            return {'ready': False, **self.blocked}
        try:
            ensure_space(self.settings)
        except JobError as exc:
            return {'ready': False, **exc.public()}
        return await asyncio.to_thread(self.runner.ready)

    async def _loop(self):
        while not self.shutdown_requested:
            try:
                job = None
                if self.store.has_queued() and (await self.ready())['ready']:
                    job = self.store.claim()
                if job:
                    await self._execute(job)
                    continue
                self.wakeup.clear()
                try:
                    await asyncio.wait_for(self.wakeup.wait(), .5)
                except TimeoutError:
                    pass
            except asyncio.CancelledError:
                raise
            except Exception:
                self.blocked = {'code': 'WORKER_STORAGE_ERROR', 'message': '任务记录或存储失败，已暂停派发；请检查服务存储。'}

    def _cleanup_failed(self, job_id):
        self.blocked = {'code': 'PROCESS_CLEANUP_FAILED', 'message': '无法确认任务进程已停止，已暂停派发。'}
        self.store.update(job_id, expected={'running', 'checking', 'cancelling'},
                          status='cancelling', stage='cleanup_failed', error=self.blocked)

    async def _execute(self, job):
        root = self.settings.data_root / 'jobs' / job['job_id']
        def touch(**fields):
            self.store.update(job['job_id'], expected={'running', 'checking', 'cancelling'}, **fields)
        def stopped():
            ensure_space(self.settings)
            return self.shutdown_requested or self.store.get(job['job_id'])['status'] == 'cancelling'
        try:
            async with asyncio.timeout(job['options']['timeout_seconds']):
                if job.get('model_rows') == 0 and job.get('tm_reused_rows', 0) > 0:
                    # Revalidate the original, not just untrusted delivery counters.
                    from .qa import read_source
                    snapshot = read_source(root / 'input' / job['inputs']['source_file']['filename'])
                    if snapshot.get('contract') != 'siemens-json/2' or snapshot['rows'] or not snapshot['tm_reuse']:
                        raise JobError('INVALID_JSON_SOURCE', '纯 TM 任务与原始输入不一致。')
                    write_json(root / 'control' / 'final_response.raw.md', {'rows': []})
                    execution = {'harness': 'tm-reuse', 'model_called': False, 'exit_code': 0, 'thread_id': None}
                else:
                    execution = await self.runner.run(job, root, touch, stopped, self.redactor)
                if stopped():
                    raise JobError('CANCEL_REQUESTED', '任务已请求停止。')
                updated, ok = self.store.update(job['job_id'], expected={'running'}, status='checking',
                                                stage='delivery_check', execution=execution)
                if not ok:
                    raise JobError('CANCEL_REQUESTED', '任务已请求停止。')
                await self._check_and_finish(updated)
        except TimeoutError:
            await self._finish(job['job_id'], 'failed', error={'code': 'TIMEOUT', 'message': '任务超过总执行时限，已停止。'})
        except JobError as exc:
            if exc.code == 'PROCESS_CLEANUP_FAILED':
                self._cleanup_failed(job['job_id'])
            elif exc.code == 'CANCEL_REQUESTED':
                state = 'interrupted' if self.shutdown_requested else 'cancelled'
                await self._finish(job['job_id'], state)
            else:
                await self._finish(job['job_id'], 'failed', error=exc.public())
        except asyncio.CancelledError:
            await self._finish(job['job_id'], 'interrupted', error={'code': 'WORKER_STOPPED', 'message': '执行器已停止，任务未自动重跑。'})
            raise
        except Exception as exc:
            await self._unexpected_failure(job['job_id'], exc)

    async def _unexpected_failure(self, job_id, exc, *, enforce_deadline=True):
        stage = self.store.get(job_id)['stage']
        # Do not retain exception strings or locals: they can contain prompts and credentials.
        diagnostic = {'stage': stage, 'exception_type': type(exc).__name__,
            'frames': [{'file': Path(f.filename).name, 'line': f.lineno, 'function': f.name}
                       for f in traceback.extract_tb(exc.__traceback__)[-12:]],
            'errno': getattr(exc, 'errno', None), 'winerror': getattr(exc, 'winerror', None)}
        if isinstance(exc, UnicodeError):
            diagnostic['encoding'] = getattr(exc, 'encoding', None)
        self.store.update(job_id, host_error=diagnostic)
        try:
            write_json(self.settings.data_root / 'jobs' / job_id / 'control' / 'host_error.json', diagnostic)
        except OSError:
            pass  # run.json still carries the diagnostic if the separate file cannot be written.
        delivery = stage == 'delivery_check'
        await self._finish(job_id, 'failed', error={
            'code': 'DELIVERY_CHECK_FAILED' if delivery else 'EXECUTION_FAILED',
            'message': ('译文交付检查发生异常' if delivery else '执行器发生异常') + '，请查看 host_error.json 或 run.json。'},
            enforce_deadline=enforce_deadline)

    async def _check_and_finish(self, job, *, enforce_deadline=True):
        root = self.settings.data_root / 'jobs' / job['job_id']
        try:
            if enforce_deadline and job.get('started_at'):
                elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(job['started_at'].replace('Z', '+00:00'))).total_seconds()
                if elapsed >= job['options']['timeout_seconds']:
                    raise JobError('TIMEOUT', '任务超过总执行时限，未发布完成结果。')
            check = asyncio.create_task(asyncio.to_thread(verify_result, self.settings, job, root))
            try:
                path, report = await asyncio.shield(check)
            except asyncio.CancelledError:
                await asyncio.gather(check, return_exceptions=True)
                raise
            if enforce_deadline and self._expired(job):
                raise JobError('TIMEOUT', '任务超过总执行时限，未发布完成结果。')
            state = 'needs_review' if report['issues'] else 'completed'
            await self._finish(job['job_id'], state, qa=report, translation=path, enforce_deadline=enforce_deadline)
        except JobError as exc:
            await self._finish(job['job_id'], 'failed', error=exc.public(), enforce_deadline=enforce_deadline)

    async def _finish(self, job_id, status, error=None, qa=None, translation=None, *, enforce_deadline=True):
        current = self.store.get(job_id)
        if current['status'] in TERMINAL:
            return
        if current['status'] == 'cancelling' and status in {'completed', 'needs_review'}:
            status, translation = 'cancelled', None
        record = {**current, 'status': status, 'stage': 'finished', 'completed_at': now(), 'error': error}
        if qa is not None:
            record['qa'] = qa
        root = self.settings.data_root / 'jobs' / job_id
        try:
            publication = asyncio.create_task(asyncio.to_thread(publish, self.settings, record, root, self.redactor, translation))
            try:
                artifacts = await asyncio.shield(publication)
            except asyncio.CancelledError:
                # Finish the bounded atomic write before a failure manifest can replace it.
                await asyncio.gather(publication, return_exceptions=True)
                raise
        except (JobError, OSError):
            artifacts = []
            record.update(status='failed', error={'code': 'ARTIFACT_PUBLISH_FAILED', 'message': '产物发布失败；原任务资料仍保存在服务端。'})
        if enforce_deadline and record['status'] in {'completed', 'needs_review'} and self._expired(record):
            return await self._finish(job_id, 'failed', error={'code': 'TIMEOUT', 'message': '任务超过总执行时限，未发布完成结果。'})
        archive_fields = {}
        if current.get('project_name'):
            archiving = asyncio.create_task(asyncio.to_thread(archive_job, self.settings, {**record, 'artifacts': artifacts}, root))
            try:
                archived = await asyncio.shield(archiving)
                archive_fields = {'project_archive': archived, 'archive_error': None}
            except asyncio.CancelledError:
                await asyncio.gather(archiving, return_exceptions=True)
                raise
            except (JobError, OSError, ValueError):
                archive_fields = {'archive_error': '项目归档失败；执行目录的原件与产物仍保留。'}
        expected = {current['status']}
        _, updated = self.store.update(job_id, expected=expected, status=record['status'], stage=record['stage'],
            completed_at=record['completed_at'], error=record['error'], qa=record.get('qa'), artifacts=artifacts, **archive_fields)
        if not updated and self.store.get(job_id)['status'] == 'cancelling':
            await self._finish(job_id, 'cancelled')

    @staticmethod
    def _expired(job):
        if not job.get('started_at'):
            return False
        elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(job['started_at'].replace('Z', '+00:00'))).total_seconds()
        return elapsed >= job['options']['timeout_seconds']

    async def stop(self):
        self.shutdown_requested = True
        self.wakeup.set()
        if self.task:
            try:
                await asyncio.wait_for(asyncio.shield(self.task), 20)
            except TimeoutError:
                self.task.cancel()
                await asyncio.gather(self.task, return_exceptions=True)


async def recheck_delivery(settings, job_id):
    """Recover an existing file after a host failure; never starts Codex or a scheduler."""
    from .store import Store, InstanceLock
    from .runner import CodexRunner
    if not re.fullmatch(r'job_[a-f0-9]{32}', job_id or ''):
        raise JobError('INVALID_JOB_ID', '请提供完整任务编号。')
    database = settings.data_root / 'jobs.sqlite3'
    if not database.is_file():
        raise JobError('NOT_FOUND', '任务数据库不存在，请核对原服务配置。', 404)
    try:
        lock = InstanceLock(settings.data_root / '.instance.lock')
    except RuntimeError:
        raise JobError('ALREADY_RUNNING', '请先停止使用此数据目录的 Worker 服务，再重检交付。', 409) from None
    store = None
    try:
        store = Store(database)
        job = store.get(job_id)
        execution = job.get('execution') or {}
        if (job['status'] != 'failed' or (job.get('error') or {}).get('code') not in {'EXECUTION_FAILED', 'DELIVERY_CHECK_FAILED', 'ARTIFACT_PUBLISH_FAILED'}
                or execution.get('exit_code') != 0 or execution.get('turn_completed') is not True
                or execution.get('turn_failed') is not False or execution.get('trace_complete') is not True):
            raise JobError('RECHECK_NOT_ALLOWED', '仅允许对 Codex 已正常完成、但宿主检查或发布失败的任务补做交付。')
        runner = CodexRunner(settings)
        if not await asyncio.to_thread(runner.recover, job):
            raise JobError('PROCESS_CLEANUP_FAILED', '无法确认原任务进程已停止，不能重检交付。')
        worker = Worker(settings, store, runner)
        history = job.get('delivery_rechecks', []) + [{'requested_at': now(), 'previous_error': job.get('error'),
            'previous_completed_at': job.get('completed_at'), 'model_called': False}]
        current, updated = store.update(job_id, expected={'failed'}, status='checking', stage='delivery_check', delivery_rechecks=history)
        if not updated:
            raise JobError('RECHECK_CONFLICT', '任务状态已变化，未执行重检。', 409)
        try:
            # This rechecks existing files without extending or restarting the finished model run.
            await worker._check_and_finish(current, enforce_deadline=False)
        except Exception as exc:
            await worker._unexpected_failure(job_id, exc, enforce_deadline=False)
        result = store.get(job_id)
        return {'job_id': job_id, 'status': result['status'], 'error': result.get('error'),
                'qa': result.get('qa'), 'artifacts': result['artifacts'], 'model_called': False}
    finally:
        if store:
            store.close()
        lock.close()
