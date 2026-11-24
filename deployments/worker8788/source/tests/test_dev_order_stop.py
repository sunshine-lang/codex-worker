"""Exercise the 8789 task API and the five real lane adapters with controlled model RPC."""
import asyncio

import httpx
import pytest

from codex_remote_worker.app import create_app
from codex_remote_worker.app_server_pool import ParallelTextAppServerRunner
from codex_remote_worker.app_server_text import TextAppServerRunner
from codex_remote_worker.common import JobError
from test_project_session import ProjectRpc
from test_translation_tasks import headers, row, uploads


class PausedModel(ProjectRpc):
    def __init__(self, lane):
        super().__init__(lane, 'same_turn')
        self.hold = True
        self.interrupting = asyncio.Event()
        self.confirm = asyncio.Event()

    def _emit(self):
        if not self.hold:
            super()._emit()

    async def request(self, method, params, **kwargs):
        if method == 'turn/interrupt':
            self.interrupting.set()
            await self.confirm.wait()
        return await super().request(method, params, **kwargs)

    def complete(self):
        self.hold = False
        if self.queues and self.actions:
            self._emit()


async def until(check):
    async with asyncio.timeout(15):
        while not check():
            await asyncio.sleep(.025)


@pytest.mark.parametrize('port', [8788, 8789])
def test_same_name_orders_own_single_lanes_and_stop_waits_for_cleanup(settings, monkeypatch, project_markdown_runtime, port):
    settings = settings.model_copy(update={'port':port, 'project_context_enabled':True,
        'continuous_project_session':True, 'max_concurrent_jobs':5, 'task_batch_concurrency':1})
    settings.doc_converter.write_bytes(b'fixture')
    monkeypatch.setattr(TextAppServerRunner, 'baseline_ready', lambda self: {'ready':True,
        'service_version':'fixture', 'codex_version':'fixture', 'tools':{}})

    def factory(settings, slot_id):
        lane = TextAppServerRunner(settings, slot_id=slot_id)
        lane.rpc = PausedModel(lane)
        lane.started = True
        lane.generation = 1
        return lane

    async def exercise():
        pool = ParallelTextAppServerRunner(settings, lane_factory=factory)
        for lane in pool.lanes:
            pool.available.put_nowait(lane)
        pool.started = True
        app = create_app(settings, runner=pool)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://worker',
                                         headers=headers(settings)) as c:
                data = {'project_name':'same name', 'segments':[row(i) for i in range(33)]}

                async def submit(h):
                    r = await c.post('/v1/tasks', files=uploads(data), data={'options':'{}'}, headers=h)
                    r.raise_for_status()
                    return r.json()['task_id']

                async def stop(task):
                    r = await c.post('/v1/tasks/'+task+'/stop')
                    r.raise_for_status()
                    return r.json()['status']

                keys = [headers(settings) for _ in range(7)]
                ids = []
                try:
                    for key in keys[:5]:
                        ids.append(await submit(key))
                    await until(lambda: all(lane.rpc.turns == 1 for lane in pool.lanes))
                    ids.append(await submit(keys[5]))
                    assert await submit(keys[0]) == ids[0]
                    cancelled_queue = await submit(keys[6])
                    assert await stop(cancelled_queue) == 'stopped'
                    assert await stop(cancelled_queue) == 'stopped'

                    tracker = app.state.translation_tasks.store
                    session_job = tracker.get(ids[0])['session_job_id']
                    victim = next(lane for lane in pool.lanes if lane.slot_id == pool.active[session_job])
                    assert await stop(ids[0]) == 'stopping'
                    await asyncio.wait_for(victim.rpc.interrupting.wait(), 10)
                    assert await stop(ids[0]) == 'stopping'
                    assert sum(lane.rpc.threads for lane in pool.lanes) == 5
                    assert tracker.get(ids[5])['status'] == 'queued'
                    victim.rpc.confirm.set()
                    await until(lambda: tracker.get(ids[0])['status'] == 'stopped' and victim.rpc.threads == 2)
                    assert victim.rpc.unloaded == 1 and victim.rpc.interrupts == 1
                    assert all(lane.rpc.threads == 1 and lane.rpc.interrupts == 0 for lane in pool.lanes if lane is not victim)
                    for lane in pool.lanes:
                        lane.rpc.complete()
                    await until(lambda: all(tracker.get(t)['status'] in ('failed','completed') for t in ids[1:]))
                    for t in ids[1:]:
                        record = tracker.get(t)
                        assert record['status'] == 'completed', record.get('error')
                        assert len(record['batches']) == 2
                        assert {b['job_id'] for b in record['batches']} == {record['session_job_id']}
                        result = (await c.get('/v1/tasks/'+t+'/result')).json()
                        assert len(result['segments']) == 33
                    assert tracker.get(ids[5])['execution_slot'] == victim.slot_id
                    assert await stop(ids[1]) == 'completed'
                    assert await stop(ids[0]) == 'stopped'
                    assert (await c.get('/v1/tasks/'+ids[0]+'/result')).is_error
                    assert await submit(keys[0]) == ids[0]
                finally:
                    for lane in pool.lanes:
                        lane.rpc.confirm.set()
                        if lane.rpc.hold and lane.rpc.turns:
                            lane.rpc.complete()
                    await pool.close()
    asyncio.run(exercise())


def test_uncertain_cleanup_keeps_slot_reserved(settings):
    class BrokenLane:
        def __init__(self, settings, slot_id):
            self.slot_id = slot_id
            self.generation = 1

        def ready(self):
            return {'ready': True}

        async def run(self, *args):
            raise JobError('PROCESS_CLEANUP_FAILED', 'fixture')

    async def exercise():
        pool = ParallelTextAppServerRunner(settings, lane_factory=BrokenLane)
        pool.available.put_nowait(pool.lanes[0])
        pool.started = True
        with pytest.raises(JobError, match='fixture'):
            await pool.run({'job_id':'held'}, None, None, lambda:False, None)
        assert pool.available.empty() and pool.active == {'held':'slot-1'}
        assert pool.ready()['ready'] is False
    asyncio.run(exercise())


@pytest.mark.parametrize('port,continuous,expected', [(8787,True,False),(8788,True,True),(8789,True,True),(8788,False,False)])
def test_stop_api_for_independent_whole_orders(settings, port, continuous, expected):
    from test_translation_tasks import JsonRunner
    app = create_app(settings.model_copy(update={
        'port':port, 'continuous_project_session':continuous}), runner=JsonRunner())
    assert ('/v1/tasks/{task_id}/stop' in app.openapi()['paths']) == expected


def test_stop_intent_survives_reopen_and_uncertain_cleanup(settings):
    from unittest.mock import Mock
    from codex_remote_worker.translation_tasks import TaskManager

    async def exercise():
        jobs, worker = Mock(), Mock()
        child = {'job_id':'job_123', 'status':'running'}
        jobs.by_key.return_value = child
        jobs.get.return_value = child
        jobs.cancel.return_value = {**child, 'status':'cancelling'}
        manager = TaskManager(settings, jobs, worker)
        task_id, stage = manager.new_stage()
        task = {'task_id':task_id, 'status':'queued', 'created_at':'test', 'request_key':'key',
                'fingerprint':'fingerprint', 'continuous_project_session':True, 'batches':[{}]}
        manager.store.accept(task, stage, manager.root/task_id, 10)
        assert (await manager.request_stop(task_id))['status'] == 'stopping'
        manager.store.close()

        manager = TaskManager(settings, jobs, worker)
        try:
            assert manager.store.active()[0]['session_job_id'] == child['job_id']
            child.update(status='cancelling', stage='cleanup_failed', error={'code':'PROCESS_CLEANUP_FAILED'})
            pending = await manager.request_stop(task_id)
            assert pending['status'] == 'stopping' and pending['error'] == child['error']
            child.update(status='cancelled')
            assert (await manager.request_stop(task_id))['status'] == 'stopped'
            assert not manager.store.active()
            jobs.cancel.assert_called_once()
        finally:
            manager.store.close()
    asyncio.run(exercise())
