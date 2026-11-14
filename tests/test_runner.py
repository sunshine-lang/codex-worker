import asyncio
import copy
import json
import os
from pathlib import Path
import signal
import subprocess
import sys

import pytest

from codex_remote_worker.common import JobError, Redactor, digest
from codex_remote_worker.runner import CodexRunner


FIXTURE = r'''
import json,pathlib,sys,time
root=pathlib.Path(sys.argv[1]);mode=sys.argv[2]
prompt=sys.stdin.buffer.read()
(root/'control'/'received.bin').write_bytes(prompt)
print(json.dumps({'type':'thread.started','thread_id':'fixture-thread'}),flush=True)
print(json.dumps({'type':'item.completed','item':{'type':'reasoning','text':'PRIVATE_THOUGHT'}}),flush=True)
print(json.dumps({'type':'item.completed','item':{'type':'command_execution','output':'fixture-provider-key'}}),flush=True)
if mode=='overflow':
 print(json.dumps({'type':'item.completed','item':{'type':'command_execution','output':'x'*4096}}),flush=True)
if mode in ('cancel','deadline'): time.sleep(120)
if mode=='invalid': print('not json',flush=True)
if mode=='error': sys.exit(3)
if mode=='turn_failed': print(json.dumps({'type':'turn.failed','error':{'message':'fixture'}}),flush=True)
elif mode!='incomplete': print(json.dumps({'type':'turn.completed','usage':{'input_tokens':1}}),flush=True)
'''


@pytest.mark.parametrize('mode,code', [('success', None), ('error', 'EXECUTION_FAILED'), ('invalid', 'INVALID_TRACE'),
    ('overflow', 'LIMIT_EXCEEDED'), ('incomplete', 'INCOMPLETE_TURN'), ('turn_failed', 'INCOMPLETE_TURN'),
    ('cancel', 'CANCEL_REQUESTED'), ('cancel_before', 'CANCEL_REQUESTED'), ('deadline', 'TIMEOUT')])
def test_real_pipe_lifecycle_with_non_model_executable(settings, tmp_path, monkeypatch, mode, code):
    from codex_remote_worker import runner as module
    root = tmp_path / 'run'
    for name in ['input', 'work', 'control', 'logs']:
        (root / name).mkdir(parents=True)
    prompt = b'\xef\xbb\xbf exact\r\n '
    (root / 'input' / 'prompt.md').write_bytes(prompt)
    script = tmp_path / 'fixture.py'
    script.write_text(FIXTURE)
    owned = []

    class FixtureProcess:
        def __init__(self, command, cwd, env, name, on_created):
            self.process = subprocess.Popen([sys.executable, str(script), str(root), mode], stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=os.name != 'nt')
            self.stdin, self.stdout, self.stderr = self.process.stdin, self.process.stdout, self.process.stderr
            on_created({'pid': self.process.pid, 'created_at': 0, 'job_object': 'fixture'})
            owned.append(self)
        def poll(self):
            return self.process.poll()
        def terminate_tree(self):
            try:
                if os.name == 'nt':
                    if self.process.poll() is None:
                        self.process.kill()
                else:
                    os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            self.process.wait(timeout=5)
        def close(self):
            for stream in [self.stdin, self.stdout, self.stderr]:
                stream.close()

    monkeypatch.setattr(module, 'WindowsProcess', FixtureProcess)
    async def prepared_runtime(*args):
        return {'runtime_version': 'fixture', 'model_called': False}
    monkeypatch.setattr(module, 'prepare_task_runtime', prepared_runtime)
    runner = CodexRunner(settings)
    monkeypatch.setattr(runner, 'ready', lambda: {'ready': True, 'codex_version': 'fixture', 'tools': {}})
    settings.max_log_bytes = 1024 if mode == 'overflow' else 1024**2
    job = {'job_id': 'job_fixture', 'inputs': {'prompt_file': {'filename': 'prompt.md'}}, 'prompt_sha256': digest(prompt),
           'options': {'timeout_seconds': 1800}}
    changes = {}
    def touch(**fields):
        changes.update(copy.deepcopy(fields))
    async def run():
        return await asyncio.wait_for(runner.run(job, root, touch, lambda: mode == 'cancel_before' or (mode == 'cancel' and bool(owned)), Redactor(['fixture-provider-key'])),
                                      .1 if mode == 'deadline' else 5)
    if code == 'TIMEOUT':
        with pytest.raises(TimeoutError):
            asyncio.run(run())
    elif code:
        with pytest.raises(JobError) as error:
            asyncio.run(run())
        assert error.value.code == code
    else:
        execution = asyncio.run(run())
        assert execution['turn_completed'] and execution['thread_id'] == 'fixture-thread'
        assert execution['prompt_delivered']
        assert (root / 'control' / 'received.bin').read_bytes() == prompt
    assert all(p.poll() is not None for p in owned)
    trace_path = root / 'logs' / 'trace.jsonl'
    trace = trace_path.read_text() if trace_path.exists() else ''
    if mode == 'cancel_before':
        assert not owned
    assert 'PRIVATE_THOUGHT' not in trace and 'fixture-provider-key' not in trace
    if mode == 'overflow':
        assert changes['execution']['trace_complete'] is False


def test_cleanup_failure_is_not_converted_to_cancelled(settings, monkeypatch):
    from test_worker import prepared
    from conftest import FakeRunner
    from codex_remote_worker.worker import Worker
    store, job_id = prepared(settings, timeout=60)
    fake = FakeRunner()
    async def fail(*args):
        raise JobError('PROCESS_CLEANUP_FAILED', 'fixture cannot prove stop')
    fake.run = fail
    async def run():
        worker = Worker(settings, store, fake)
        await worker._execute(store.claim())
        assert store.get(job_id)['status'] == 'cancelling'
        assert (await worker.ready())['ready'] is False
    try:
        asyncio.run(run())
    finally:
        store.close()
