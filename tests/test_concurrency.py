import asyncio
import copy
import json
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from codex_remote_worker.app import create_app
from codex_remote_worker.common import JobError
from codex_remote_worker.store import Store
from test_translation_tasks import JsonRunner, headers, row, uploads, wait_for_task


class GatedRunner(JsonRunner):
    def __init__(self, *, hold_first=False, hold_all=False, fail_first=False):
        super().__init__()
        self.hold_first, self.hold_all, self.fail_first = hold_first, hold_all, fail_first
        self.release = asyncio.Event()
        self.two_started = asyncio.Event()
        self.active = self.peak = 0
        self.started = []

    async def run(self, job, root, touch, stopped, redactor):
        packet = json.loads((root/'work'/job['inputs']['source_file']['filename']).read_text())
        first = int(packet['rows'][0]['row_id'])
        self.started.append((packet['request_id'], first, job['job_id']))
        self.active += 1
        self.peak = max(self.peak, self.active)
        if self.active >= 2:
            self.two_started.set()
        try:
            while (self.hold_all or self.hold_first and first == 0) and not self.release.is_set():
                if stopped():
                    raise JobError('CANCEL_REQUESTED', 'fixture stopped')
                await asyncio.sleep(.005)
            if self.fail_first and first == 0:
                await asyncio.wait_for(self.two_started.wait(), 1)
                raise JobError('FIXTURE_FAILURE', 'fixture failed')
            await asyncio.sleep(.02)
            return await super().run(job, root, touch, stopped, redactor)
        finally:
            self.active -= 1


def parallel(settings, jobs=2, batches=2):
    # model_copy lets the regression exercise the old scheduler before the new config exists.
    return settings.model_copy(update={'max_concurrent_jobs': jobs, 'task_batch_concurrency': batches})


def test_one_order_runs_in_parallel_and_merges_out_of_order(settings):
    async def exercise():
        configured = parallel(settings)
        runner = GatedRunner(hold_first=True)
        app = create_app(configured, runner=runner)
        data = {'project_name': 'Out of order', 'segments': [row(i) for i in range(65)] + [row(65, True)]}
        original = copy.deepcopy(data)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://worker', headers=headers(configured)) as client:
                receipt = await client.post('/v1/tasks', files=uploads(data), data={'options': '{}'})
                task_id = receipt.json()['task_id']
                try:
                    await asyncio.wait_for(runner.two_started.wait(), 1.5)
                    for _ in range(200):
                        progress = (await client.get('/v1/tasks/'+task_id)).json()
                        if progress['progress']['completed_batches'] >= 1:
                            break
                        await asyncio.sleep(.01)
                    assert progress['progress']['completed_batches'] >= 1
                    assert progress['progress']['finished_segments'] > 1
                    assert 0 not in progress['completed_batch_indices']
                    assert len(progress['active_worker_job_ids']) <= 2
                    assert progress['status'] == 'running'
                    assert (await client.get('/v1/tasks/'+task_id+'/result')).status_code == 409
                finally:
                    runner.release.set()
                final = await wait_for_task(client, task_id)
                result = (await client.get(final['result_path'])).json()
                assert final['status'] == 'completed'
                assert runner.peak == 2
                assert len(runner.started) == 3
                assert len({job for _, _, job in runner.started}) == 3
                assert sorted(int(r['row_id']) for packet in runner.calls for r in packet['rows']) == list(range(65))
                assert [r['segment_id'] for r in result['segments']] == [str(i) for i in range(66)]
                for before, after in zip(original['segments'], result['segments']):
                    assert {k:v for k,v in before.items() if k!='target_text'} == {k:v for k,v in after.items() if k!='target_text'}
                assert result['segments'][-1]['target_text'] == 'TM 原样译文'
    asyncio.run(exercise())


@pytest.mark.parametrize('limit',[1,2,4])
def test_global_cap_and_shutdown_cover_all_active_jobs(settings,limit):
    async def exercise():
        configured = parallel(settings,jobs=limit,batches=limit)
        runner = GatedRunner(hold_all=True)
        app = create_app(configured, runner=runner)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://worker', headers=headers(configured)) as client:
                for n in range(2):
                    response = await client.post('/v1/tasks', files=uploads({'project_name': str(n), 'segments': [row(i) for i in range(65)]}),
                                                 data={'options': '{}'}, headers=headers(configured))
                    assert response.status_code == 202
                for _ in range(200):
                    if runner.active==limit:break
                    await asyncio.sleep(.01)
                await asyncio.sleep(.1)
                assert runner.active == runner.peak == limit
                assert len(app.state.store.unfinished()) == limit
        assert runner.active == 0
        assert all(app.state.worker.executions[job].done() for _, _, job in runner.started if job in app.state.worker.executions)
    asyncio.run(exercise())


def test_failed_batch_stops_new_dispatch_but_collects_successful_sibling(settings):
    async def exercise():
        configured = parallel(settings)
        runner = GatedRunner(fail_first=True)
        app = create_app(configured, runner=runner)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://worker', headers=headers(configured)) as client:
                response = await client.post('/v1/tasks', files=uploads([row(i) for i in range(97)]), data={'options': '{}'})
                final = await wait_for_task(client, response.json()['task_id'])
                assert final['status'] == 'failed'
                assert final['progress']['model_translated_segments'] == 32
                assert len(runner.started) == 2
                assert runner.active == 0
                assert (await client.get('/v1/tasks/'+final['task_id']+'/result')).status_code == 409
    asyncio.run(exercise())


def test_store_claim_enforces_cap_transactionally(tmp_path):
    store = Store(tmp_path/'jobs.sqlite3')
    try:
        for i in range(8):
            stage=tmp_path/f'stage{i}';stage.mkdir()
            record={'job_id':f'job{i}','request_key':f'key{i}','fingerprint':'fixture','status':'queued','created_at':str(i)}
            store.accept(record,stage,tmp_path/f'job{i}',10)
        with ThreadPoolExecutor(max_workers=8) as pool:
            claimed=list(pool.map(lambda _:store.claim(max_active=2),range(8)))
        assert len([v for v in claimed if v]) == 2
        assert len(store.unfinished()) == 2
        first=next(v for v in claimed if v)
        store.update(first['job_id'],status='completed')
        assert store.claim(max_active=2) is not None
        assert store.claim(max_active=2) is None
    finally:
        store.close()


def test_concurrency_config_is_bounded_and_server_owned(settings):
    from pydantic import ValidationError
    from codex_remote_worker.config import Settings
    values=settings.model_dump()
    for change in ({'max_concurrent_jobs':0}, {'max_concurrent_jobs':17}, {'max_concurrent_jobs':True},
                   {'task_batch_concurrency':2,'max_concurrent_jobs':1}):
        with pytest.raises(ValidationError):Settings.model_validate({**values,**change})
    assert Settings.model_validate({**values,'max_concurrent_jobs':4,'task_batch_concurrency':2}).task_batch_concurrency == 2


def test_restart_recovers_nonzero_batch_without_duplicate_execution(settings):
    async def exercise():
        configured=parallel(settings);runner=JsonRunner();app=create_app(configured,runner=runner)
        async with app.router.lifespan_context(app):
            manager=app.state.translation_tasks;manager.stopping=True;manager.wakeup.set();await manager.task
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url='http://worker',headers=headers(configured)) as client:
                response=await client.post('/v1/tasks',files=uploads([row(i) for i in range(65)]),data={'options':'{}'})
                task_id=response.json()['task_id'];task=manager.store.get(task_id)
                # Crash window: batch 1 was accepted, but its job_id was never saved in the parent.
                child=await asyncio.to_thread(manager._enqueue,task,task['batches'][1])
                app.state.worker.wakeup.set()
                for _ in range(200):
                    if app.state.store.get(child['job_id'])['status']=='completed':break
                    await asyncio.sleep(.01)
                assert app.state.store.get(child['job_id'])['status']=='completed'
        app2=create_app(configured,runner=runner)
        async with app2.router.lifespan_context(app2):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app2),base_url='http://worker',headers=headers(configured)) as client:
                final=await wait_for_task(client,task_id)
                assert final['status']=='completed'
                assert len(runner.calls)==3
                assert sorted(int(r['row_id']) for packet in runner.calls for r in packet['rows'])==list(range(65))
    asyncio.run(exercise())


def test_dispatch_error_drains_an_existing_child(settings,monkeypatch):
    async def exercise():
        configured=parallel(settings);runner=JsonRunner();app=create_app(configured,runner=runner)
        async with app.router.lifespan_context(app):
            manager=app.state.translation_tasks;original=manager._enqueue
            def enqueue(task,batch):
                if task['batches'].index(batch)==1:raise JobError('BATCH_TOO_LARGE','fixture')
                return original(task,batch)
            monkeypatch.setattr(manager,'_enqueue',enqueue)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url='http://worker',headers=headers(configured)) as client:
                response=await client.post('/v1/tasks',files=uploads([row(i) for i in range(97)]),data={'options':'{}'})
                final=await wait_for_task(client,response.json()['task_id'])
                assert final['status']=='failed' and final['error']['code']=='BATCH_TOO_LARGE'
                assert final['progress']['model_translated_segments']==32
                assert len(runner.calls)==1
    asyncio.run(exercise())


def test_cancel_one_order_does_not_stop_the_other(settings):
    async def exercise():
        configured=parallel(settings);runner=GatedRunner(hold_all=True);app=create_app(configured,runner=runner)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url='http://worker',headers=headers(configured)) as client:
                ids=[]
                for i in range(2):
                    r=await client.post('/v1/tasks',files=uploads([row(i)]),data={'options':'{}'},headers=headers(configured));ids.append(r.json()['task_id'])
                try:
                    await asyncio.wait_for(runner.two_started.wait(),1.5)
                    first_job=next(job for task,_,job in runner.started if task==ids[0])
                    second_job=next(job for task,_,job in runner.started if task==ids[1])
                    await client.post('/v1/jobs/'+first_job+'/cancel')
                    for _ in range(200):
                        if app.state.store.get(first_job)['status']=='cancelled':break
                        await asyncio.sleep(.01)
                    assert app.state.store.get(first_job)['status']=='cancelled'
                    assert app.state.store.get(second_job)['status']=='running'
                finally:runner.release.set()
                a=await wait_for_task(client,ids[0]);b=await wait_for_task(client,ids[1])
                assert a['status']=='failed' and b['status']=='completed'
                cap=(await client.get('/v1/tasks/capabilities')).json()
                assert cap['scheduler_version']=='bounded-concurrency/1'
                assert cap['concurrency']=={'max_concurrent_jobs':2,'task_batch_concurrency':2}
    asyncio.run(exercise())


def test_queue_pressure_does_not_fail_or_starve_orders(settings):
    async def exercise():
        configured=parallel(settings);runner=JsonRunner();app=create_app(configured,runner=runner)
        async with app.router.lifespan_context(app):
            manager=app.state.translation_tasks;manager.stopping=True;manager.wakeup.set();await manager.task
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url='http://worker',headers=headers(configured)) as client:
                ids=[]
                for i in range(3):
                    response=await client.post('/v1/tasks',files=uploads({'project_name':str(i),'segments':[row(j) for j in range(65)]}),
                                               data={'options':'{}'},headers=headers(configured))
                    ids.append(response.json()['task_id'])
                configured.queue_capacity=1
                manager.stopping=False;await manager.start()
                for task_id in ids:
                    final=await wait_for_task(client,task_id)
                    assert final['status']=='completed'
                assert len(runner.calls)==9
                assert set(packet['request_id'] for packet in runner.calls)==set(ids)
    asyncio.run(exercise())
