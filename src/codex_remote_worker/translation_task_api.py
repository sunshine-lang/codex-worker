"""Two-line integration into an existing Worker app; runner and sandbox stay owned by that app."""
from contextlib import asynccontextmanager
import asyncio
import re
import shutil
import uuid

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import ValidationError
from starlette.datastructures import UploadFile

from .common import JobError, ROLES
from .config import Options
from .files import ensure_space, receive_files
from .translation_tasks import BatchPolicy, TaskManager, TASK_CONTRACT, public_task


def install_translation_tasks(app, settings, authorized, *, policy=None):
    policy = policy or BatchPolicy()
    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application):
        async with original_lifespan(application):
            manager = TaskManager(settings, application.state.store, application.state.worker, policy)
            application.state.translation_tasks = manager
            await manager.start()
            try:
                yield
            finally:
                await manager.stop()

    app.router.lifespan_context = lifespan
    router = APIRouter(prefix='/v1/tasks', dependencies=[Depends(authorized)])

    @router.get('/capabilities')
    async def capabilities():
        return {'contract': TASK_CONTRACT, 'whole_request_submission': True, 'tm100_routing': True,
                'automatic_batching': True, 'status_query': True, 'result_download': True,
                'semantic_review': False, 'max_segments': 10000,
                'batch_policy': {'max_rows': policy.max_rows, 'max_chars': policy.max_chars},
                'scheduler_version': 'bounded-concurrency/1', 'bounded_parallel_execution': True,
                'concurrency': {'max_concurrent_jobs': settings.max_concurrent_jobs,
                                'task_batch_concurrency': settings.task_batch_concurrency}}

    @router.post('')
    async def submit(request: Request):
        try:
            key = str(uuid.UUID(request.headers.get('idempotency-key', '')))
        except ValueError:
            raise JobError('INVALID_IDEMPOTENCY_KEY', 'Idempotency-Key 必须为 UUID。') from None
        manager = app.state.translation_tasks
        task_id, stage = manager.new_stage()
        try:
            async with request.form(max_files=4, max_fields=1, max_part_size=64*1024) as form:
                items = list(form.multi_items())
                if len(items) != 5 or {k for k, _ in items} != {*ROLES, 'options'}:
                    raise JobError('INVALID_INPUT', '需要完整 JSON 文件、三份文档及 options。')
                if any(not isinstance(form[role], UploadFile) for role in ROLES) or not isinstance(form['options'], str):
                    raise JobError('INVALID_INPUT', '上传字段类型不正确。')
                try:
                    options = Options.model_validate_json(form['options'])
                except ValidationError:
                    raise JobError('INVALID_OPTIONS', '仅接受 timeout_seconds；分批参数由 Worker 配置。') from None
                ensure_space(settings)
                manifest = await receive_files(settings, {role: form[role] for role in ROLES}, stage)
                if manager.store.by_key(key) is None:
                    ready = await app.state.worker.ready()
                    if not ready.get('ready'):
                        raise JobError('NOT_READY', ready.get('message', 'Worker 尚未就绪。'), 503)
                task, reused = await asyncio.to_thread(manager.accept, task_id, stage, manifest, options, key)
                manager.wakeup.set()
                return JSONResponse({**public_task(task), 'reused_submission': reused}, status_code=200 if reused else 202)
        finally:
            if stage.exists():
                shutil.rmtree(stage)

    def identifier(task_id):
        if not re.fullmatch(r'task_[a-f0-9]{32}', task_id):
            raise JobError('INVALID_TASK_ID', '任务编号格式无效。')
        return task_id

    @router.get('/{task_id}')
    async def status(task_id: str):
        return public_task(app.state.translation_tasks.store.get(identifier(task_id)))

    @router.get('/{task_id}/result')
    async def result(task_id: str):
        path = await asyncio.to_thread(app.state.translation_tasks.result_path, identifier(task_id))
        return FileResponse(path, filename=task_id+'.json', media_type='application/json', headers={'Cache-Control': 'no-store'})

    app.include_router(router)
