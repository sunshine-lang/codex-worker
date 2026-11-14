import asyncio
import json

import httpx
import pytest

from codex_remote_worker.app import create_app
from codex_remote_worker.common import JobError
from test_translation_tasks import JsonRunner, headers, row, uploads, wait_for_task


def test_task_store_maintenance_rejects_new_accept_but_preserves_idempotency(settings):
    async def exercise():
        app = create_app(settings, runner=JsonRunner())
        async with app.router.lifespan_context(app):
            manager = app.state.translation_tasks
            manager.stopping = True
            manager.wakeup.set()
            await manager.task
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://worker', headers=headers(settings)) as client:
                accepted = await client.post('/v1/tasks', files=uploads([row(0)]), data={'options': '{}'})
                task = manager.store.get(accepted.json()['task_id'])
                marker = settings.data_root/'.maintenance.json'
                marker.write_text('{}')
                previous, reused = manager.store.accept(task, settings.data_root/'unused-stage', manager.root/task['task_id'], 10)
                assert reused and previous['task_id'] == task['task_id']
                stage = settings.data_root/'isolated-new-stage'
                stage.mkdir()
                record = {**task, 'task_id': 'task_new', 'request_key': 'different-key'}
                with pytest.raises(JobError) as error:
                    manager.store.accept(record, stage, manager.root/'task_new', 10)
                assert error.value.code == 'MAINTENANCE'
                assert stage.is_dir() and not (manager.root/'task_new').exists()
    asyncio.run(exercise())


def test_task_manager_pauses_and_resumes_parent_dispatch_during_maintenance(settings):
    async def exercise():
        runner = JsonRunner()
        app = create_app(settings, runner=runner)
        async with app.router.lifespan_context(app):
            manager = app.state.translation_tasks
            manager.stopping = True
            manager.wakeup.set()
            await manager.task
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://worker', headers=headers(settings)) as client:
                response = await client.post('/v1/tasks', files=uploads([row(0)]), data={'options': '{}'})
                task_id = response.json()['task_id']
                marker = settings.data_root/'.maintenance.json'
                marker.write_text('{}')
                manager.stopping = False
                await manager.start()
                await asyncio.sleep(.65)
                assert manager.store.get(task_id)['status'] == 'queued'
                assert manager.store.get(task_id)['batches'][0]['job_id'] is None
                assert runner.calls == []
                marker.unlink()
                manager.wakeup.set()
                final = await wait_for_task(client, task_id)
                assert final['status'] == 'completed'
                assert len(runner.calls) == 1
    asyncio.run(exercise())


def test_maintenance_race_after_parent_dispatch_does_not_fail_task(settings, monkeypatch):
    async def exercise():
        app = create_app(settings, runner=JsonRunner())
        async with app.router.lifespan_context(app):
            manager = app.state.translation_tasks
            manager.stopping = True
            manager.wakeup.set()
            await manager.task
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://worker', headers=headers(settings)) as client:
                response = await client.post('/v1/tasks', files=uploads([row(0)]), data={'options': '{}'})
                task_id = response.json()['task_id']
                def maintenance(*args):
                    raise JobError('MAINTENANCE', 'fixture', 503)
                monkeypatch.setattr(manager, '_enqueue', maintenance)
                await manager.advance(manager.store.get(task_id))
                state = manager.store.get(task_id)
                assert state['status'] == 'running' and state['error'] is None
                assert state['stage'] == 'waiting_for_maintenance'
                assert state['batches'][0]['job_id'] is None
    asyncio.run(exercise())


def test_completed_result_hash_is_checked_before_download(settings):
    async def exercise():
        app = create_app(settings, runner=JsonRunner())
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://worker', headers=headers(settings)) as client:
                response = await client.post('/v1/tasks', files=uploads([row(0, True)]), data={'options': '{}'})
                final = await wait_for_task(client, response.json()['task_id'])
                path = settings.data_root/'tasks'/final['task_id']/'result.json'
                path.write_text(json.dumps({'tampered': True}), encoding='utf-8')
                result = await client.get(final['result_path'])
                assert result.status_code == 409 and result.json()['error']['code'] == 'ARTIFACT_CHANGED'
    asyncio.run(exercise())
