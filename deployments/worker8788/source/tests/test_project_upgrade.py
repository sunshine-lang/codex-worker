import copy
import json
from pathlib import Path
import uuid

from fastapi.testclient import TestClient
import pytest

from codex_remote_worker.app import create_app
from codex_remote_worker.common import JobError, digest, write_json
from codex_remote_worker.json_delivery import read_json_source
from codex_remote_worker.project_archive import project_directory
from codex_remote_worker.tm_policy import select_tm100
from test_jobs import auth, wait


def row(number, rate=None):
    record = {'segment_id': 'segment-' + str(number), 'source_language': 'en', 'target_language': 'zh-CN',
        'source_text': 'Press Start.', 'target_text': '', 'context': None, 'tb_list': [], 'tm_list': []}
    value = {'row_id': str(number), 'record': record, 'memory': {}, 'source_units': [], 'term_decisions': []}
    if rate is not None:
        record['tm_list'] = [{'source_text': record['source_text'], 'target_text': '按启动键。', 'match_rate': rate}]
        if rate == 100 and type(rate) is not bool:
            record['target_text'] = record['tm_list'][0]['target_text']
            value['selection'] = select_tm100(record)
    return value


def packet(*, pure=False, phase='translate'):
    value = {'contract': 'siemens-json/2', 'phase': phase, 'project_name': 'Siemens/A 项目', 'request_id': 'request-test',
        'rows': [] if pure else [row(1), row(3)], 'tm_reuse': [row(0, 100)]}
    if phase == 'review':
        for item in value['rows']:
            item['record']['target_text'] = '已存在候选。'
    return value


class Runner:
    def __init__(self, fail=False):
        self.calls = 0
        self.fail = fail
        self.received = []

    def ready(self):
        return {'ready': True}

    def recover(self, job):
        return True

    async def run(self, job, root, touch, stopped, redactor):
        self.calls += 1
        data = read_json_source(root / 'work' / job['inputs']['source_file']['filename'])
        self.received.append(data)
        if self.fail:
            raise JobError('SYNTHETIC_FAILURE', '固定模拟失败。')
        if data['phase'] == 'translate':
            rows = [{'row_id': r['row_id'], 'target_text': '按启动键。', 'term_decisions': []} for r in data['rows']]
        else:
            rows = [{'row_id': r['row_id'], 'decision': 'pass', 'issues': [], 'coverage': [], 'new_senses': []} for r in data['rows']]
        write_json(root / 'control/final_response.raw.md', {'rows': rows})
        return {'exit_code': 0, 'thread_id': 'fixture-thread', 'turn_completed': True}


def submit(client, settings, data):
    files = {'source_file': ('input.json', json.dumps(data, ensure_ascii=False).encode('utf-8')),
        'prompt_file': ('prompt.md', b'Return only JSON'), 'project_style': ('project.md', b'Project style'),
        'general_style': ('general.md', b'General style')}
    return client.post('/v1/jobs', headers={**auth(settings), 'Idempotency-Key': str(uuid.uuid4())}, files=files, data={'options': '{}'})


@pytest.mark.parametrize('pure,phase', [(True, 'translate'), (False, 'translate'), (True, 'review'), (False, 'review')])
def test_project_delivery_archive_and_zero_model_path(settings, pure, phase):
    runner = Runner()
    source = packet(pure=pure, phase=phase)
    with TestClient(create_app(settings, runner=runner)) as client:
        accepted = submit(client, settings, source)
        assert accepted.status_code == 202, accepted.text
        job = wait(client, settings, accepted.json()['job_id'])
        assert job['status'] == 'completed', job
        assert job['model_rows'] == (0 if pure else 2) and job['tm_reused_rows'] == 1
        assert runner.calls == (0 if pure else 1)
        assert job['archive_error'] is None and job['project_name'] == source['project_name']
        archive = settings.data_root / job['project_archive']
        assert json.loads((archive / 'job.json').read_text(encoding='utf-8'))['status'] == job['status']
        candidates = json.loads((archive / 'batch-candidates.json').read_text(encoding='utf-8'))
        assert candidates['segments'][0] == source['tm_reuse'][0]['record']
        assert len(candidates['segments']) == (1 if pure else 3)
        assert (archive / 'input/source_file.json').is_file()
        for artifact in job['artifacts']:
            data = (archive / 'artifacts' / artifact['filename']).read_bytes()
            assert digest(data) == artifact['sha256']
        run = json.loads((archive / 'artifacts/run.json').read_text(encoding='utf-8'))
        assert run['project_name'] == source['project_name'] and run['request_id'] == source['request_id']
        delivery = next(a for a in job['artifacts'] if a['kind'] == 'translation')
        result = client.get(delivery['download_path'], headers=auth(settings)).json()
        assert [r['row_id'] for r in result['rows']] == ([] if pure else ['1', '3'])
        if pure:
            assert run['execution'] == {'harness': 'tm-reuse', 'model_called': False, 'exit_code': 0, 'thread_id': None}
            root = settings.data_root / 'jobs' / job['job_id']
            assert not (root / 'control/preparation.json').exists()


@pytest.mark.parametrize('mutation', [
    lambda p: p['tm_reuse'][0]['selection'].update(target_text='伪造译文'),
    lambda p: p['tm_reuse'][0]['selection'].update(tm_index=False),
    lambda p: p['tm_reuse'][0]['selection'].update(source_exact=1),
    lambda p: p['tm_reuse'][0]['record'].update(target_text='修改过的译文'),
    lambda p: p['rows'].append(copy.deepcopy(p['tm_reuse'][0])),
    lambda p: p['rows'][0].update(row_id='0'),
    lambda p: p.update(project_name=''),
    lambda p: p.update(request_id='x' * 129),
    lambda p: p.update(rows=[], tm_reuse=[]),
])
def test_invalid_project_packet_is_rejected_without_job(settings, mutation):
    source = packet()
    mutation(source)
    runner = Runner()
    with TestClient(create_app(settings, runner=runner)) as client:
        response = submit(client, settings, source)
        assert response.status_code == 422
        assert response.json()['error']['code'] == 'INVALID_JSON_SOURCE'
        assert runner.calls == 0
        assert client.app.state.store.db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0] == 0


@pytest.mark.parametrize('rate', [True, '100', -1, 101, float('nan'), float('inf'), 10 ** 1000])
def test_rate_validation_cannot_overflow(rate):
    record = row(0)['record']
    record['tm_list'] = [{'source_text': record['source_text'], 'target_text': '译文', 'match_rate': rate}]
    with pytest.raises(ValueError):
        select_tm100(record)


def test_archive_failure_is_explicit_and_does_not_destroy_delivery(settings, monkeypatch):
    from codex_remote_worker import project_archive
    def fail(*args):
        raise OSError('synthetic archive failure')
    monkeypatch.setattr(project_archive, '_archive_job', fail)
    with TestClient(create_app(settings, runner=Runner())) as client:
        job = wait(client, settings, submit(client, settings, packet()).json()['job_id'])
        assert job['status'] == 'completed' and job['archive_error']
        assert (settings.data_root / 'jobs' / job['job_id'] / 'input/input.json').is_file()
        assert any(a['kind'] == 'translation' for a in job['artifacts'])


def test_execution_failure_is_archived(settings):
    with TestClient(create_app(settings, runner=Runner(fail=True))) as client:
        job = wait(client, settings, submit(client, settings, packet()).json()['job_id'])
        assert job['status'] == 'failed' and job['archive_error'] is None
        meta = settings.data_root / job['project_archive'] / 'job.json'
        assert json.loads(meta.read_text(encoding='utf-8'))['status'] == 'failed'


@pytest.mark.parametrize('name', ['CON.report', '../../outside', 'A/B', 'A\\B', '项目' * 100])
def test_labels_are_not_paths(tmp_path, name):
    path = project_directory(tmp_path, name)
    assert path.parent == tmp_path / 'projects'
    assert json.loads((path / 'project.json').read_text(encoding='utf-8'))['project_name'] == name


@pytest.mark.parametrize('target', ['project.json', 'jobs', 'job.json', 'input', 'artifacts', 'tm-reuse.json', 'batch-candidates.json', 'input/source_file.json', 'artifacts/run.json'])
def test_archive_links_and_metadata_cannot_escape(settings, tmp_path, target):
    with TestClient(create_app(settings, runner=Runner())) as client:
        job = wait(client, settings, submit(client, settings, packet()).json()['job_id'])
        archive = settings.data_root / job['project_archive']
        relative = archive.parent.parent / 'project.json' if target == 'project.json' else archive / target
        if target == 'jobs':
            # Use a fresh project, whose jobs directory can be linked without deleting data.
            project = project_directory(settings.data_root, 'new-project')
            relative = project / 'jobs'
        outside = tmp_path / 'outside'
        outside.mkdir()
        if relative.is_dir():
            relative.rename(relative.with_name(relative.name + '.original'))
            relative.symlink_to(outside, target_is_directory=True)
        elif relative.exists():
            relative.unlink()
            relative.symlink_to(outside / 'never-created.json')
        else:
            relative.symlink_to(outside, target_is_directory=True)
        from codex_remote_worker.project_archive import archive_job
        original = client.app.state.store.get(job['job_id'])
        if target == 'jobs':
            original['project_name'] = 'new-project'
        with pytest.raises((JobError, OSError)):
            archive_job(settings, original, settings.data_root / 'jobs' / job['job_id'])
        assert not list(outside.iterdir())


def test_v2_helper_preserves_global_model_ids_and_reports_counts(settings, tmp_path):
    from codex_remote_worker.task_runtime import layout, runtime_instructions
    from codex_remote_worker.task_tools import TaskTools
    settings.doc_converter.write_bytes(b'converter fixture')
    root = tmp_path / 'task'
    (root / 'work').mkdir(parents=True)
    (root / 'control').mkdir()
    data = packet()
    write_json(root / 'work/input.json', data)
    write_json(root / 'control/source.json', data)
    inputs = {'source_file': {'filename': 'input.json'}}
    for role in ('project_style', 'general_style'):
        path = root / 'work' / (role + '.md')
        path.write_text('中文指南', encoding='utf-8')
        inputs[role] = {'filename': path.name, 'sha256': digest(path.read_bytes())}
    layout(settings, {'inputs': inputs, 'output_filename': 'input_result.json'}, root, data, cache=False)
    tools = TaskTools(root / 'work/.worker/manifest.json')
    assert tools.source() == data['rows']
    meta = tools.inspect()
    assert meta['input_contract'] == 'siemens-json/2' and meta['model_rows'] == 2 and meta['tm_reused_rows'] == 1
    instructions = runtime_instructions(settings, root)
    assert 'No CSV' in instructions and 'packet.tm_reuse is locked' in instructions and 'siemens-json/2' in instructions


def test_portable_rejects_ready_old_worker():
    from test_portable import portable
    with pytest.raises(portable.PortableError) as error:
        portable.require_service({'ready': True, 'service_version': '0.3.0', 'input_contracts': ['siemens-json/1', 'table-csv/1']})
    assert error.value.code == 'WORKER_UPGRADE_REQUIRED'


def test_queued_project_cancel_archived_before_public_terminal(settings):
    from conftest import FakeRunner
    from test_jobs import upload
    runner = FakeRunner(blocked=True)
    with TestClient(create_app(settings, runner=runner)) as client:
        first = upload(client, settings).json()['job_id']
        wait(client, settings, first, {'running'})
        queued = submit(client, settings, packet()).json()['job_id']
        response = client.post('/v1/jobs/' + queued + '/cancel', headers=auth(settings))
        assert response.status_code == 200
        job = response.json()
        assert job['status'] == 'cancelled' and job['archive_error'] is None
        archived = settings.data_root / job['project_archive'] / 'job.json'
        assert json.loads(archived.read_text(encoding='utf-8'))['status'] == 'cancelled'
        assert runner.calls == 1
        runner.release.set()
        wait(client, settings, first)


def test_late_registration_cannot_overwrite_terminal_archive(settings):
    from codex_remote_worker.project_archive import archive_job
    with TestClient(create_app(settings, runner=Runner())) as client:
        job = wait(client, settings, submit(client, settings, packet()).json()['job_id'])
        current = client.app.state.store.get(job['job_id'])
        archive_job(settings, {**current, 'status': 'queued', 'artifacts': []}, settings.data_root / 'jobs' / job['job_id'])
        archived = settings.data_root / job['project_archive'] / 'job.json'
        assert json.loads(archived.read_text(encoding='utf-8'))['status'] == 'completed'
