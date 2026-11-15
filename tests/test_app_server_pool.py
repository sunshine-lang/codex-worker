import asyncio
import json

import pytest

from codex_remote_worker.app_server_pool import ParallelTextAppServerRunner
from codex_remote_worker.app_server_text import TextAppServerRunner
from codex_remote_worker.common import JobError, digest
from test_app_server_adapter import Rpc
from test_app_server_text import prepared
from test_json_delivery import packet


class GatedRpc(Rpc):
    def __init__(self, name, *, refuse_interrupt=False):
        super().__init__('blocked')
        self.name = name
        self.started = asyncio.Event()
        self.refuse_interrupt = refuse_interrupt

    async def request(self, method, params, **kwargs):
        if method == 'turn/interrupt' and self.refuse_interrupt:
            raise JobError('APP_SERVER_RPC_TIMEOUT', 'fixture interrupt timed out')
        result = await super().request(method, params, **kwargs)
        if method == 'thread/start':
            self.thread_id = self.name + '-' + self.thread_id
            result['thread']['id'] = self.thread_id
        if method == 'turn/start':
            self.started.set()
        return result

    def finish(self, marker):
        source = json.loads((self.cwd.parent / 'control/source.json').read_text())
        value = {'rows': [{'row_id': row['row_id'], 'target_text': marker, 'term_decisions': []}
                          for row in source['rows']]}
        self.queues[self.thread_id].put_nowait({'method': 'turn/completed', 'params': {
            'threadId': self.thread_id, 'turn': {'id': self.turn_id, 'status': 'completed', 'items': [
                {'type': 'agentMessage', 'id': 'final', 'phase': 'final_answer', 'text': json.dumps(value)}]}}})


def make_job(settings, tmp_path, name):
    source = packet()
    source['rows'][0]['record']['source_text'] = name
    root, _ = prepared(settings, tmp_path / name, source)
    (root / 'logs').mkdir()
    (root / 'input').mkdir()
    prompt = b'Translate exact supplied context'
    (root / 'input/prompt.md').write_bytes(prompt)
    return root, {'job_id': name, 'inputs': {'prompt_file': {'filename': 'prompt.md'}},
                  'prompt_sha256': digest(prompt), 'options': {'timeout_seconds': 60}}


def make_pool(settings, monkeypatch, *, refuse_interrupt=False, count=2):
    monkeypatch.setattr(TextAppServerRunner, 'baseline_ready', lambda self: {
        'ready': True, 'code': 'READY', 'service_version': 'fixture', 'codex_version': 'fixture', 'tools': {}})
    async def start(self):
        self.started = True
        self.generation = 1
    async def prepare(self, *args):
        return {'model_called': False}
    monkeypatch.setattr(TextAppServerRunner, 'start', start)
    monkeypatch.setattr(TextAppServerRunner, 'prepare', prepare)
    def factory(settings, slot_id):
        lane = TextAppServerRunner(settings, slot_id=slot_id)
        lane.rpc = GatedRpc(slot_id, refuse_interrupt=refuse_interrupt and slot_id == 'slot-1')
        return lane
    return ParallelTextAppServerRunner(settings.model_copy(update={
        'max_concurrent_jobs': count, 'task_batch_concurrency': count}), lane_factory=factory)


def test_two_real_adapters_overlap_and_third_waits_without_context_mixing(settings, tmp_path, monkeypatch):
    async def exercise():
        pool = make_pool(settings, monkeypatch)
        await pool.start()
        jobs = [make_job(settings, tmp_path, name) for name in ('alpha', 'beta', 'gamma')]
        tasks = [asyncio.create_task(pool.run(job, root, lambda **kw: None, lambda: False, pool.lanes[0].redactor))
                 for root, job in jobs]
        a, b = (lane.rpc for lane in pool.lanes)
        try:
            await asyncio.wait_for(asyncio.gather(a.started.wait(), b.started.wait()), 2)
            assert len(pool.active) == 2 and not tasks[2].done()
            assert sum(rpc.turns for rpc in (a, b)) == 2
            assert pool.lanes[0].server_root != pool.lanes[1].server_root
            assert pool.lanes[0].process_record != pool.lanes[1].process_record
            for rpc, name in ((a, 'alpha'), (b, 'beta')):
                payload = next(params for method, params in rpc.calls if method == 'turn/start')
                data = json.loads(payload['input'][0]['text'])
                assert data['source']['rows'][0]['record']['source_text'] == name
            b.finish('BETA ONLY')
            await asyncio.wait_for(tasks[1], 2)
            for _ in range(100):
                if b.turns == 2: break
                await asyncio.sleep(.01)
            assert b.turns == 2 and not tasks[0].done()
            b.finish('GAMMA ONLY')
            a.finish('ALPHA ONLY')
            results = await asyncio.wait_for(asyncio.gather(*tasks), 2)
            assert results[0]['execution_slot'] != results[1]['execution_slot']
            for (root, _), expected in zip(jobs, ('ALPHA ONLY', 'BETA ONLY', 'GAMMA ONLY')):
                assert json.loads((root / 'control/final_response.raw.md').read_text())['rows'][0]['target_text'] == expected
            assert pool.ready()['max_concurrent_jobs'] == pool.ready()['task_batch_concurrency'] == 2
        finally:
            for task in tasks:
                if not task.done(): task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await pool.close()
    asyncio.run(exercise())


@pytest.mark.parametrize('forced_recycle', [False, True])
@pytest.mark.parametrize('task_cancel', [False, True])
def test_cancellation_or_timeout_cleans_only_its_own_daemon(settings, tmp_path, monkeypatch, forced_recycle, task_cancel):
    async def exercise():
        pool = make_pool(settings, monkeypatch, refuse_interrupt=forced_recycle)
        await pool.start()
        stop = False
        jobs = [make_job(settings, tmp_path, name) for name in ('cancelled', 'survivor')]
        tasks = [asyncio.create_task(pool.run(job, root, lambda **kw: None,
            (lambda: stop) if index == 0 else (lambda: False), pool.lanes[0].redactor))
            for index, (root, job) in enumerate(jobs)]
        a, b = (lane.rpc for lane in pool.lanes)
        try:
            await asyncio.wait_for(asyncio.gather(a.started.wait(), b.started.wait()), 2)
            if task_cancel:
                tasks[0].cancel()  # Same cancellation injected by asyncio.timeout in Worker.
            else:
                stop = True
            result = (await asyncio.wait_for(asyncio.gather(tasks[0], return_exceptions=True), 2))[0]
            assert isinstance(result, asyncio.CancelledError if task_cancel else JobError)
            assert a.closed == int(forced_recycle)
            assert b.closed == b.interrupts == 0 and b.connected and not tasks[1].done()
            b.finish('SURVIVOR ONLY')
            completed = await asyncio.wait_for(tasks[1], 2)
            assert completed['turn_completed']
            assert not (jobs[0][0] / 'control/final_response.raw.md').exists()
        finally:
            for task in tasks:
                if not task.done(): task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await pool.close()
    asyncio.run(exercise())


def test_partial_startup_closes_both_slots(settings, monkeypatch):
    async def exercise():
        pool = make_pool(settings, monkeypatch)
        async def fail():
            raise JobError('START_FAILED', 'fixture')
        monkeypatch.setattr(pool.lanes[1], 'start', fail)
        daemons = [lane.rpc for lane in pool.lanes]
        with pytest.raises(JobError):
            await pool.start()
        assert not pool.ready()['ready']
        assert all(rpc.closed == 1 for rpc in daemons)
    asyncio.run(exercise())


@pytest.mark.windows
def test_native_process_recycling_leaves_other_slot_alive(settings, tmp_path):
    from codex_remote_worker.app_server_rpc import AppServerRpc
    from codex_remote_worker.common import Redactor
    from codex_remote_worker.windows import WindowsProcess, same_process_alive
    fixture = tmp_path / 'fixed_echo_rpc.py'
    fixture.write_text('import os,sys,json\nfor line in sys.stdin:\n m=json.loads(line)\n if "id" in m: print(json.dumps({"id":m["id"],"result":{"pid":os.getpid(),"echo":m.get("params")}}),flush=True)\n')
    def factory(command, cwd, env, name):
        return WindowsProcess([str(settings.tool_python), str(fixture)], cwd, env, name)
    async def exercise():
        a, b = [AppServerRpc(settings, tmp_path / name, Redactor(), process_factory=factory)
                for name in ('native-slot-1', 'native-slot-2')]
        try:
            await asyncio.wait_for(asyncio.gather(a.start(), b.start()), 10)
            metadata_a, metadata_b = a.process.metadata, b.process.metadata
            assert metadata_a['pid'] != metadata_b['pid']
            assert metadata_a['job_object'] != metadata_b['job_object']
            replies = await asyncio.gather(a.request('echo', {'marker': 'A'}), b.request('echo', {'marker': 'B'}))
            assert [r['echo']['marker'] for r in replies] == ['A', 'B']
            await a.close()
            assert not same_process_alive(metadata_a)
            assert same_process_alive(metadata_b)
            assert (await b.request('echo', {'marker': 'survived'}, timeout=3))['echo']['marker'] == 'survived'
        finally:
            await asyncio.gather(a.close(), b.close())
        assert not same_process_alive(metadata_b)
    asyncio.run(exercise())
