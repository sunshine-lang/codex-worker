from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import hmac
import shutil
import uuid
import time
import re

from fastapi import Depends, FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import ValidationError
from starlette.datastructures import UploadFile
from starlette.exceptions import HTTPException

from .common import JobError, ROLES, canonical, contained_file, digest, file_digest, TERMINAL
from .config import Options, Settings
from .files import ensure_space, new_stage, prepare_record, receive_files
from .runner import CodexRunner, codex_config_digest
from .store import Store, InstanceLock
from .worker import Worker
from . import __version__
from .access_audit import AccessAudit
from .project_archive import archive_job


class BodyLimit:
    def __init__(self, app, maximum):
        self.app, self.maximum = app, maximum

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        headers = dict(scope.get('headers', []))
        raw = headers.get(b'content-length')
        try:
            too_large = raw is not None and int(raw) > self.maximum
        except ValueError:
            too_large = True
        if too_large:
            return await JSONResponse({'error': {'code': 'UPLOAD_TOO_LARGE', 'message': '请求超过服务限制。'}}, status_code=413)(scope, receive, send)
        size = 0
        async def bounded_receive():
            nonlocal size
            message = await receive()
            if message['type'] == 'http.request':
                size += len(message.get('body', b''))
                if size > self.maximum:
                    raise HTTPException(413)
            return message
        return await self.app(scope, bounded_receive, send)


def public_job(job):
    result = {key: job.get(key) for key in ['job_id', 'status', 'stage', 'created_at', 'updated_at',
        'started_at', 'completed_at', 'error', 'progress', 'language_quality', 'artifacts',
        'project_name', 'request_id', 'project_archive', 'archive_error', 'model_rows', 'tm_reused_rows']}
    report = job.get('qa')
    result['qa'] = {key: report.get(key) for key in ['rows', 'errors', 'warnings']} if report else None
    result['configuration_source'] = 'server_codex' if 'model_id' not in job['options'] else 'legacy_request'
    result['poll_after_seconds'] = 3
    return result


def create_app(settings: Settings, *, runner=None):
    """The runner parameter is a test seam, never an HTTP/config option."""
    @asynccontextmanager
    async def lifespan(app):
        root = settings.data_root
        root.mkdir(parents=True, exist_ok=True)
        for directory in ['jobs', 'staging']:
            (root / directory).mkdir(exist_ok=True)
        lock = InstanceLock(root / '.instance.lock')
        store = None
        audit = None
        try:
            audit = AccessAudit(root)
            app.state.access_audit = audit
            store = Store(root / 'jobs.sqlite3')
            worker = Worker(settings, store, runner or CodexRunner(settings))
            app.state.store, app.state.worker = store, worker
            await worker.start()
            try:
                yield
            finally:
                await worker.stop()
        finally:
            if store:
                store.close()
            if audit:
                audit.close()
            lock.close()

    app = FastAPI(title='Codex Remote Worker', version=__version__, lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(BodyLimit, maximum=settings.max_upload_bytes + 128 * 1024)

    @app.middleware('http')
    async def access_audit(request, call_next):
        started = time.monotonic()
        status = 500
        try:
            response = await call_next(request)
            status = response.status_code
            return response
        finally:
            audit = getattr(app.state, 'access_audit', None)
            if audit is not None:
                route = getattr(request.scope.get('route'), 'path', '<unmatched>')
                job_id = getattr(request.state, 'job_id', None) or request.path_params.get('job_id')
                if not isinstance(job_id, str) or not re.fullmatch(r'job_[a-f0-9]{32}', job_id):
                    job_id = None
                audit.record(method=request.method[:16], route=route,
                             client_ip=request.client.host if request.client else None,
                             status=status, elapsed_ms=(time.monotonic() - started) * 1000, job_id=job_id)

    async def authorized(request: Request):
        supplied = request.headers.get('authorization', '')
        expected = 'Bearer ' + settings.service_token.get_secret_value()
        if not hmac.compare_digest(supplied.encode(), expected.encode()):
            raise JobError('UNAUTHORIZED', '需要有效的服务访问令牌。', 401)

    @app.exception_handler(JobError)
    async def job_error(request, exc):
        headers = {'Retry-After': '3'} if exc.http_status in {429, 503} else None
        return JSONResponse({'error': exc.public()}, status_code=exc.http_status, headers=headers)

    @app.exception_handler(HTTPException)
    async def http_error(request, exc):
        status = 413 if exc.status_code == 413 else exc.status_code
        code = 'UPLOAD_TOO_LARGE' if status == 413 else 'INVALID_REQUEST'
        return JSONResponse({'error': {'code': code, 'message': '请求格式或大小不符合接口要求。'}}, status_code=status)

    @app.exception_handler(Exception)
    async def unexpected(request, exc):
        return JSONResponse({'error': {'code': 'INTERNAL_ERROR', 'message': '服务处理失败；请使用原提交编号查询或重试。'}}, status_code=500)

    @app.get('/healthz')
    async def health():
        return {'status': 'alive'}

    @app.get('/v1/ready', dependencies=[Depends(authorized)])
    async def ready():
        result = await app.state.worker.ready()
        result['input_contracts'] = ['siemens-json/2', 'siemens-json/1', 'table-csv/1']
        result['service_version'] = __version__
        return JSONResponse(result, status_code=200 if result['ready'] else 503)

    @app.post('/v1/jobs', dependencies=[Depends(authorized)])
    async def submit(request: Request):
        try:
            request_key = str(uuid.UUID(request.headers.get('idempotency-key', '')))
        except ValueError:
            raise JobError('INVALID_IDEMPOTENCY_KEY', 'Idempotency-Key 必须为 UUID。') from None
        stage = None
        try:
            async with request.form(max_files=4, max_fields=1, max_part_size=64 * 1024) as form:
                items = list(form.multi_items())
                if len(items) != 5 or {key for key, _ in items} != {*ROLES, 'options'}:
                    raise JobError('INVALID_INPUT', '必须提交四份文件及 options，不能重复字段或添加未知字段。')
                if not all(isinstance(form[role], UploadFile) for role in ROLES) or not isinstance(form['options'], str):
                    raise JobError('INVALID_INPUT', '文件字段和 options 类型不正确。')
                try:
                    options = Options.model_validate_json(form['options'])
                except ValidationError:
                    raise JobError('INVALID_OPTIONS', '任务参数无效；新版仅接收 timeout_seconds，模型配置由远端 Codex 管理，请更新 Langflow 节点。') from None
                existing = app.state.store.by_key(request_key)
                if not existing:
                    readiness = await app.state.worker.ready()
                    if not readiness['ready']:
                        raise JobError('NOT_READY', readiness.get('message', '执行环境尚未就绪。'), 503)
                    ensure_space(settings)
                job_id, stage = new_stage(settings)
                manifest = await receive_files(settings, {role: form[role] for role in ROLES}, stage)
                fingerprint = digest(canonical({'files': manifest, 'options': options.model_dump()}))
                if existing:
                    if fingerprint != existing['fingerprint']:
                        raise JobError('IDEMPOTENCY_CONFLICT', '同一提交编号携带了不同资料或参数。', 409)
                    job, reused = existing, True
                else:
                    record = await asyncio.to_thread(prepare_record, job_id, request_key, manifest, options, stage,
                                                     codex_config_sha256=codex_config_digest(settings))
                    job, reused = app.state.store.accept(record, stage, settings.data_root / 'jobs' / job_id, settings.queue_capacity)
                    if job.get('project_name'):
                        try:
                            archive = await asyncio.to_thread(archive_job, settings, job, settings.data_root / 'jobs' / job['job_id'])
                            job, _ = app.state.store.update(job['job_id'], expected={'queued'}, project_archive=archive, archive_error=None)
                        except (JobError, OSError, ValueError):
                            job, _ = app.state.store.update(job['job_id'], expected={'queued'}, archive_error='项目归档待重试；执行目录保留原件。')
                    app.state.worker.wakeup.set()
                request.state.job_id = job['job_id']
                return JSONResponse({'job_id': job['job_id'], 'status': job['status'], 'reused': reused,
                    'status_path': f"/v1/jobs/{job['job_id']}", 'poll_after_seconds': 3}, status_code=200 if reused else 202)
        finally:
            if stage and stage.exists():
                shutil.rmtree(stage)

    @app.get('/v1/jobs/{job_id}', dependencies=[Depends(authorized)])
    async def status(job_id: str):
        return public_job(app.state.store.get(job_id))

    @app.post('/v1/jobs/{job_id}/cancel', dependencies=[Depends(authorized)])
    async def cancel(job_id: str):
        job = app.state.store.cancel(job_id, defer_project=True)
        if job.pop('_finish_queued_cancel', False):
            await app.state.worker._finish(job_id, 'cancelled')
            job = app.state.store.get(job_id)
        app.state.worker.wakeup.set()
        return JSONResponse(public_job(job), status_code=200 if job['status'] in TERMINAL else 202)

    @app.get('/v1/jobs/{job_id}/artifacts/{artifact_id}', dependencies=[Depends(authorized)])
    async def download(job_id: str, artifact_id: str):
        job = app.state.store.get(job_id)
        entry = next((a for a in job['artifacts'] if a['id'] == artifact_id), None)
        if entry is None:
            raise JobError('NOT_FOUND', '产物不存在或尚未发布。', 404)
        root = settings.data_root / 'jobs' / job_id / 'artifacts'
        try:
            path = contained_file(root.resolve(), root / entry['filename'])
            if path.stat().st_size != entry['bytes'] or await asyncio.to_thread(file_digest, path) != entry['sha256']:
                raise JobError('ARTIFACT_CORRUPTED', '产物完整性检查失败，已停止下载。', 409)
        except OSError:
            raise JobError('ARTIFACT_MISSING', '已发布的产物当前不可读取。', 409) from None
        return FileResponse(path, filename=entry['filename'], media_type='application/octet-stream',
                            headers={'X-Content-SHA256': entry['sha256'], 'Cache-Control': 'no-store'})

    from .translation_task_api import install_translation_tasks
    install_translation_tasks(app, settings, authorized)
    return app
