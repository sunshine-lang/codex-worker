import asyncio
import json
from pathlib import Path
import time
import uuid
import pytest

from codex_remote_worker.common import digest
from codex_remote_worker.config import Options
from codex_remote_worker.files import new_stage, prepare_record
from codex_remote_worker.store import Store
from codex_remote_worker.worker import Worker
from conftest import FakeRunner


def prepared(settings, timeout=.03):
    settings.data_root.mkdir()
    (settings.data_root / 'jobs').mkdir()
    job_id, stage = new_stage(settings)
    manifest = {}
    for role, name, content in [('source_file', 'source.csv', b'en,zh-CN\nHello,\n'), ('prompt_file', 'prompt.md', b'prompt'),
                                ('project_style', 'style.md', b'style'), ('general_style', 'general.doc', b'general')]:
        (stage / 'input' / name).write_bytes(content)
        manifest[role] = {'filename': name, 'bytes': len(content), 'sha256': digest(content)}
    record = prepare_record(job_id, str(uuid.uuid4()), manifest, Options(), stage)
    # Test-only shorter deadline; public requests still enforce the 60-second lower bound.
    record['options']['timeout_seconds'] = timeout
    store = Store(settings.data_root / 'jobs.sqlite3')
    store.accept(record, stage, settings.data_root / 'jobs' / job_id, 10)
    return store, job_id


def test_deadline_stops_execution_and_never_marks_success(settings):
    store, job_id = prepared(settings)
    runner = FakeRunner(blocked=True)
    async def run():
        worker = Worker(settings, store, runner)
        await worker._execute(store.claim())
        assert store.get(job_id)['status'] == 'failed'
        assert store.get(job_id)['error']['code'] == 'TIMEOUT'
        assert runner.stops == 1
    try:
        asyncio.run(run())
    finally:
        store.close()


def test_deadline_during_checks_does_not_race_published_manifest(settings, monkeypatch):
    store, job_id = prepared(settings)
    from codex_remote_worker import worker as module
    original = module.verify_result
    def slow(*args):
        time.sleep(.08)
        return original(*args)
    monkeypatch.setattr(module, 'verify_result', slow)
    async def run():
        worker = Worker(settings, store, FakeRunner())
        await worker._execute(store.claim())
        result = store.get(job_id)
        assert result['status'] == 'failed' and result['error']['code'] == 'TIMEOUT'
        run_manifest = json.loads((settings.data_root / 'jobs' / job_id / 'artifacts' / 'run.json').read_text())
        assert run_manifest['status'] == 'failed'
        assert not any(a['kind'] == 'translation' for a in result['artifacts'])
    try:
        asyncio.run(run())
    finally:
        store.close()


def test_delivery_reads_utf8_snapshot_under_windows_legacy_encoding(settings, monkeypatch):
    store, job_id = prepared(settings, timeout=60)
    from codex_remote_worker.common import write_json
    snapshot_path = settings.data_root / 'jobs' / job_id / 'control' / 'source.json'
    snapshot = json.loads(snapshot_path.read_text(encoding='utf-8'))
    snapshot['records'][0]['context'] = '中文规范与安全'
    write_json(snapshot_path, snapshot)
    original_open = Path.open
    def windows_open(path, mode='r', buffering=-1, encoding=None, errors=None, newline=None):
        if path.name == 'source.json' and encoding in (None, 'locale') and 'b' not in mode:
            encoding = 'cp936'
        return original_open(path, mode, buffering, encoding, errors, newline)
    monkeypatch.setattr(Path, 'open', windows_open)
    async def run():
        await Worker(settings, store, FakeRunner())._execute(store.claim())
        job = store.get(job_id)
        assert job['status'] == 'completed', job['error']
        assert job['qa']['rows'] == 1
        assert any(a['kind'] == 'translation' for a in job['artifacts'])
    try:
        asyncio.run(run())
    finally:
        store.close()


def test_host_failure_records_stage_and_frames_without_secret_values(settings, monkeypatch):
    from codex_remote_worker import worker as module
    store, job_id = prepared(settings, timeout=60)
    def broken_check(*args):
        raise ValueError('fixture-provider-key sensitive input')
    monkeypatch.setattr(module, 'verify_result', broken_check)
    async def run():
        await Worker(settings, store, FakeRunner())._execute(store.claim())
        job = store.get(job_id)
        assert job['error']['code'] == 'DELIVERY_CHECK_FAILED'
        assert job['host_error']['exception_type'] == 'ValueError'
        assert job['host_error']['stage'] == 'delivery_check'
        assert job['host_error']['frames'][-1]['function'] == 'broken_check'
        root = settings.data_root / 'jobs' / job_id / 'artifacts'
        for name in ['host_error.json', 'run.json']:
            text = (root / name).read_text(encoding='utf-8')
            assert 'fixture-provider-key' not in text and 'sensitive input' not in text
    try:
        asyncio.run(run())
    finally:
        store.close()


@pytest.mark.parametrize('tampered', [False, True])
def test_recheck_existing_output_never_calls_codex_even_after_original_deadline(settings, monkeypatch, tampered):
    from codex_remote_worker.worker import recheck_delivery
    from codex_remote_worker.runner import CodexRunner
    store, job_id = prepared(settings, timeout=60)
    async def prepare():
        await Worker(settings, store, FakeRunner())._execute(store.claim())
    asyncio.run(prepare())
    root = settings.data_root / 'jobs' / job_id
    store.update(job_id, status='failed', started_at='2020-01-01T00:00:00Z',
        error={'code': 'EXECUTION_FAILED', 'message': 'old host failure'},
        execution={'exit_code': 0, 'turn_completed': True, 'turn_failed': False, 'trace_complete': True})
    if tampered:
        (root / 'work' / 'source.csv').write_bytes(b'changed input')
    store.close()
    monkeypatch.setattr(CodexRunner, 'recover', lambda self, job: True)
    async def forbidden(*args):
        pytest.fail('Recovery must not call Codex')
    monkeypatch.setattr(CodexRunner, 'run', forbidden)
    result = asyncio.run(recheck_delivery(settings, job_id))
    assert result['model_called'] is False
    if tampered:
        assert result['status'] == 'failed' and result['error']['code'] == 'INPUT_CHANGED'
        assert not any(a['kind'] == 'translation' for a in result['artifacts'])
    else:
        assert result['status'] == 'completed'
        assert result['qa']['rows'] == 1
        assert any(a['kind'] == 'translation' for a in result['artifacts'])
    saved = json.loads((root / 'artifacts' / 'run.json').read_text(encoding='utf-8'))
    assert saved['started_at'] == '2020-01-01T00:00:00Z'
    assert saved['delivery_rechecks'][0]['previous_error']['code'] == 'EXECUTION_FAILED'


def test_recheck_refuses_incomplete_codex_run_and_running_service(settings):
    from codex_remote_worker.worker import recheck_delivery
    from codex_remote_worker.common import JobError
    from codex_remote_worker.store import InstanceLock
    store, job_id = prepared(settings)
    store.update(job_id, status='failed', error={'code': 'EXECUTION_FAILED'}, execution={'exit_code': 0})
    store.close()
    with pytest.raises(JobError, match='仅允许'):
        asyncio.run(recheck_delivery(settings, job_id))
    lock = InstanceLock(settings.data_root / '.instance.lock')
    try:
        with pytest.raises(JobError) as error:
            asyncio.run(recheck_delivery(settings, job_id))
        assert error.value.code == 'ALREADY_RUNNING'
    finally:
        lock.close()
