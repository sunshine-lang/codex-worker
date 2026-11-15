import asyncio
import json
from pathlib import Path

import pytest

from codex_remote_worker.app_server_runner import AppServerRunner
from codex_remote_worker.app_server_rpc import AppServerRpc
from codex_remote_worker.app_server_pool import ParallelTextAppServerRunner
from codex_remote_worker.common import Redactor
from test_app_server_adapter import Rpc
from test_app_server_pool import make_pool, make_job


@pytest.mark.parametrize('signal', ['thread/closed', 'thread/status/changed'])
def test_unsubscribe_ack_does_not_prove_unloaded(settings, signal):
    async def exercise():
        runner = AppServerRunner(settings)
        rpc = runner.rpc = Rpc()
        queue = rpc.subscribe('owned')
        requested = asyncio.Event()
        async def request(method, params, **kwargs):
            assert method == 'thread/unsubscribe'
            requested.set()
            return {'status':'unsubscribed'}
        rpc.request = request
        task = asyncio.create_task(runner._finish_thread('owned', 'turn', True, queue))
        await requested.wait()
        await asyncio.sleep(.01)
        assert not task.done()
        queue.put_nowait({'method':signal, 'params':{'threadId':'foreign', 'status':{'type':'notLoaded'}}})
        await asyncio.sleep(.01)
        assert not task.done()
        queue.put_nowait({'method':signal, 'params':{'threadId':'owned', 'status':{'type':'notLoaded'}}})
        result = await asyncio.wait_for(task, 1)
        assert result['thread_unloaded'] and not result['daemon_recycled']
        assert rpc.closed == 0 and 'owned' not in rpc.queues
    asyncio.run(exercise())


def test_missing_unload_confirmation_recycles_only_owned_daemon(settings):
    async def exercise():
        runner = AppServerRunner(settings)
        rpc = runner.rpc = Rpc()
        queue = rpc.subscribe('owned')
        async def request(*args, **kwargs):
            return {'status':'unsubscribed'}
        rpc.request = request
        runner._unload_thread = lambda *args: AppServerRunner._unload_thread(runner, *args, timeout=.02)
        result = await runner._finish_thread('owned', 'turn', True, queue)
        assert result['daemon_recycled'] and not result['thread_unloaded']
        assert runner.restart_pending and runner.rpc is None and rpc.closed == 1
    asyncio.run(exercise())


def test_runtime_identity_checks_detect_extra_or_replaced_process(settings, tmp_path):
    rpc = AppServerRpc(settings, tmp_path, Redactor())
    class Process:
        members = ((10, 100.), (11, 101.))
        def active_processes(self): return self.members
    rpc.process = Process()
    rpc.runtime_processes = rpc.process.members
    assert rpc.runtime_processes_unchanged()
    rpc.process.members = ((10, 100.), (11, 999.))
    assert not rpc.runtime_processes_unchanged()
    rpc.process.members = ((10, 100.), (11, 101.), (12, 102.))
    assert not rpc.runtime_processes_unchanged()


def test_four_slots_overlap_fifth_waits_and_reuses_freed_slot(settings, tmp_path, monkeypatch):
    async def exercise():
        pool = make_pool(settings, monkeypatch, count=4)
        await pool.start()
        jobs = [make_job(settings, tmp_path, f'row-{i}') for i in range(5)]
        tasks = [asyncio.create_task(pool.run(job, root, lambda **kw:None, lambda:False, pool.lanes[0].redactor))
                 for root,job in jobs]
        rpcs = [lane.rpc for lane in pool.lanes]
        try:
            await asyncio.wait_for(asyncio.gather(*(r.started.wait() for r in rpcs)),2)
            assert len(pool.active) == 4 and sum(r.turns for r in rpcs) == 4
            assert not tasks[4].done()
            rpcs[2].finish('third')
            await asyncio.wait_for(tasks[2], 2)
            for _ in range(100):
                if rpcs[2].turns == 2: break
                await asyncio.sleep(.01)
            assert rpcs[2].turns == 2 and rpcs[2].closed == 0
            for index,rpc in enumerate(rpcs): rpc.finish('fifth' if index == 2 else f'answer-{index}')
            results = await asyncio.wait_for(asyncio.gather(*tasks),2)
            assert len({r['execution_slot'] for r in results}) == 4
            assert results[4]['execution_slot'] == results[2]['execution_slot']
            assert results[4]['thread_id'] != results[2]['thread_id']
            for (root,_),target in zip(jobs,['answer-0','answer-1','third','answer-3','fifth']):
                assert json.loads((root/'control/final_response.raw.md').read_text())['rows'][0]['target_text'] == target
        finally:
            for task in tasks:
                if not task.done(): task.cancel()
            await asyncio.gather(*tasks,return_exceptions=True)
            await pool.close()
    asyncio.run(exercise())


@pytest.mark.parametrize('count', [0,5,True])
def test_pool_refuses_out_of_range_or_boolean_counts(settings,count):
    with pytest.raises(ValueError):
        ParallelTextAppServerRunner(settings.model_copy(update={'max_concurrent_jobs':count}))


def test_subscription_override_is_isolated_and_drops_provider_credentials(settings,tmp_path,monkeypatch):
    from codex_remote_worker import app_server_service as service
    base=tmp_path/'baseline'; base.mkdir()
    settings.data_root.mkdir()
    (base/'config.local.json').write_text(json.dumps({'data_root':str(settings.data_root)}))
    exe=tmp_path/'native.exe';exe.touch()
    data=tmp_path/'trial-data';home=data/'private/codex-home';home.mkdir(parents=True)
    config={'baseline_root':str(base),'data_root':str(data),'port':8788,'bind_host':'127.0.0.1',
            'codex_executable':str(exe),'expected_codex_version':'fixture',
            'authentication_mode':'chatgpt','codex_home':str(home),
            'max_concurrent_jobs':4,'task_batch_concurrency':4}
    path=tmp_path/'trial.json';path.write_text(json.dumps(config))
    monkeypatch.setattr(service,'load_settings',lambda *args:settings)
    result=service.trial_settings(path)
    assert result.authentication_mode=='chatgpt' and result.provider_env=={}
    assert result.codex_home==home and result.max_concurrent_jobs==result.task_batch_concurrency==4
    assert settings.provider_env and settings.codex_home!=home
    config['codex_home']=str(settings.codex_home);path.write_text(json.dumps(config))
    with pytest.raises(ValueError,match='own private'):
        service.trial_settings(path)


def test_acceptance_retries_sharing_conflict_without_duplicate_or_overwrite(tmp_path,monkeypatch):
    from codex_remote_worker.common import accept_directory
    stage=tmp_path/'stage';stage.mkdir();(stage/'original').write_text('exact')
    target=tmp_path/'accepted'
    original=Path.rename;calls=[]
    def rename(path,destination):
        calls.append((path,destination))
        if len(calls)==1:
            error=PermissionError('synthetic file scanner');error.winerror=5;raise error
        return original(path,destination)
    monkeypatch.setattr(Path,'rename',rename)
    accept_directory(stage,target,tmp_path)
    assert len(calls)==2 and not stage.exists() and (target/'original').read_text()=='exact'
    stage.mkdir();(stage/'original').write_text('new')
    with pytest.raises(FileExistsError):accept_directory(stage,target,tmp_path)
    assert (target/'original').read_text()=='exact' and len(calls)==2


def test_acceptance_persistent_denial_remains_bounded(tmp_path,monkeypatch):
    from codex_remote_worker import common
    stage=tmp_path/'stage';stage.mkdir();calls=[]
    def rename(*args):
        calls.append(1);error=PermissionError('synthetic permanent denial');error.winerror=5;raise error
    monkeypatch.setattr(Path,'rename',rename)
    monkeypatch.setattr(common.time,'sleep',lambda _:None)
    with pytest.raises(PermissionError):common.accept_directory(stage,tmp_path/'target',tmp_path)
    assert len(calls)==6 and stage.exists()
