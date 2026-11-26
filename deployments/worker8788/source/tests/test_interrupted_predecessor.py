import asyncio
from unittest.mock import Mock

import pytest

from codex_remote_worker.common import JobError
from codex_remote_worker.project_recovery import interruption_key, reconciled_after_restart, service_interruption
from codex_remote_worker.translation_tasks import TaskManager


def incident():
    process = dict(pid=321, created_at=123, job_object='Local\\CodexWorkerAppServer8788-'+'b'*32)
    return dict(job_id='job_'+'c'*32, started_at='start', completed_at='finish', process=process,
                status='interrupted', error={'code':'WORKER_RESTARTED'},
                restart_cleanup={'started_at':'start','verified_at':'now','process':process.copy()})


def test_restart_cleanup_belongs_to_the_same_attempt():
    job = incident()
    assert reconciled_after_restart(job)
    assert not reconciled_after_restart({**job,'started_at':'next'})
    assert not reconciled_after_restart({**job,'process':None})
    assert not reconciled_after_restart({**job,'cancel_reason':'user'})
    assert not service_interruption({**job,'status':'failed','error':{'code':'TIMEOUT'}})


@pytest.mark.parametrize('manual,broken', [(False,False),(True,False),(False,True)])
def test_waiter_handles_one_interruption_once(settings, monkeypatch, manual, broken):
    settings=settings.model_copy(update={'port':8788,'continuous_project_session':True})
    jobs, worker = Mock(), Mock()
    job = incident()
    jobs.by_key.return_value = job
    manager=TaskManager(settings,jobs,worker)
    def order(key):
        tid,stage=manager.new_stage()
        fields=dict(task_id=tid, request_key=key, fingerprint=key, project_name='P', created_at=key,
                    status='queued', stage='queued', continuous_project_session=True, batches=[{}])
        return manager.store.accept(fields, stage, manager.root/tid, 20, project_name_key='P')[0]
    prior,waiter=order('B'),order('C')
    prior.update(status='failed',stage='failed',session_job_id=job['job_id'],error=job['error'])
    manager.store.save(prior)
    calls=[]
    def resume(_settings, task_id, *, recovery_key, waiting_task_id):
        calls.append((task_id,recovery_key,waiting_task_id))
        if broken:
            raise JobError('INVALID_PROJECT_CHECKPOINT','bad fixture')
        prior.update(status='queued',stage='waiting_for_worker',error=None,auto_recovery={'state':'scheduled'})
        manager.store.save(prior)
    monkeypatch.setattr('codex_remote_worker.translation_tasks.queue_resume', resume)
    async def exercise():
        if manual:
            await manager.request_stop(prior['task_id'])
        for _ in range(2):
            await manager.advance(manager.store.get(waiter['task_id']))
        result=manager.store.get(waiter['task_id'])
        assert not result.get('session_job_id')
        assert calls == ([] if manual else [(prior['task_id'],interruption_key(job),waiter['task_id'])])
        assert result['waiting_on_reason']==('manual_stop' if manual else 'recovery_blocked' if broken else 'auto_resuming')
    try:
        asyncio.run(exercise())
    finally:
        manager.store.close()
