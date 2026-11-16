"""Up to eight independently owned, reusable App Servers with isolated cancellation."""
from __future__ import annotations

import asyncio

from .app_server_text import TextAppServerRunner
from .common import JobError


class ParallelTextAppServerRunner:
    def __init__(self, settings, *, lane_factory=TextAppServerRunner):
        count = settings.max_concurrent_jobs
        if type(count) is not int or not 1 <= count <= 8:
            raise ValueError('The 8788 instance supports one to eight execution slots')
        if not 1 <= settings.task_batch_concurrency <= count:
            raise ValueError('Invalid batch concurrency')
        self.settings = settings
        self.lanes = [lane_factory(settings, slot_id=f'slot-{i+1}') for i in range(count)]
        self.available = asyncio.Queue(maxsize=count)
        self.active = {}
        self.started = False
        self.blocked = None

    async def start(self):
        try:
            for lane in self.lanes:
                await lane.start()
                self.available.put_nowait(lane)
            self.started = True
        except BaseException:
            await self.close()
            raise

    def ready(self):
        states = [lane.ready() for lane in self.lanes]
        value = dict(next((s for s in states if not s['ready']), states[0]))
        value.update(execution_backend='codex-app-server', scheduler_version='app-server-pool/1',
            max_concurrent_jobs=len(self.lanes), task_batch_concurrency=self.settings.task_batch_concurrency,
            supported_max_concurrent_jobs=8, app_server_reuse=True,
            independent_app_server_processes=len(self.lanes), active_execution_slots=len(self.active),
            app_server_generations={lane.slot_id: lane.generation for lane in self.lanes})
        if not self.started:
            value.update(ready=False, code='APP_SERVER_UNAVAILABLE', message='App Server 执行池尚未启动。')
        if self.blocked:
            value.update(ready=False, **self.blocked)
        return value

    def recover(self, job):
        return self.lanes[0].recover(job)

    async def run(self, job, root, touch, stopped, redactor):
        state = self.ready()
        if not state['ready']:
            raise JobError('NOT_READY', state.get('message', state['code']), 503)
        lane = await self.available.get()
        self.active[job['job_id']] = lane.slot_id
        def tagged_touch(**fields):
            if 'execution' in fields:
                fields['execution'] = {**fields['execution'], 'execution_slot': lane.slot_id}
            touch(**fields)
        try:
            if stopped():
                raise JobError('CANCEL_REQUESTED', '任务在取得执行通道前已请求停止。')
            result = await lane.run(job, root, tagged_touch, stopped, redactor)
            return {**result, 'execution_slot': lane.slot_id}
        except JobError as exc:
            if exc.code == 'PROCESS_CLEANUP_FAILED':
                self.blocked = exc.public()
            raise
        finally:
            self.active.pop(job['job_id'], None)
            self.available.put_nowait(lane)

    async def close(self):
        self.started = False
        results = await asyncio.gather(*(lane.close() for lane in self.lanes), return_exceptions=True)
        for result in results:
            if isinstance(result, BaseException):
                raise result
