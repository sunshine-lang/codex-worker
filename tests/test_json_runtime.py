import copy
import json
from pathlib import Path
import subprocess
import sys
import uuid

from fastapi.testclient import TestClient
import pytest

from codex_remote_worker import __version__
from codex_remote_worker.app import create_app
from codex_remote_worker.common import JobError, digest
from codex_remote_worker.json_delivery import CONTRACT, check_json_result
from codex_remote_worker.qa import read_source
from codex_remote_worker.runner import config_arguments
from codex_remote_worker.task_runtime import layout, runtime_instructions
from codex_remote_worker.task_tools import TaskTools
from test_json_delivery import JsonRunner, packet, reply, submit
from test_jobs import auth, wait


def setup_json_task(settings, tmp_path, phase='translate'):
    root = tmp_path / 'json-task'
    for folder in ('input', 'work', 'control', 'logs', 'artifacts'):
        (root / folder).mkdir(parents=True)
    settings.doc_converter.write_bytes(b'fixture-converter')
    data = packet(phase)
    data['rows'][0]['record']['business_id'] = {'code': '00001', 'null': None, 'flag': False}
    source = json.dumps(data, ensure_ascii=False).encode('utf-8')
    inputs = {'source_file': {'filename': 'input.json', 'sha256': digest(source)}}
    for folder in ('input', 'work'):
        (root / folder / 'input.json').write_bytes(source)
    for role in ('project_style', 'general_style'):
        guide = ('中文指南 ' + role).encode('utf-8')
        for folder in ('input', 'work'):
            (root / folder / (role + '.md')).write_bytes(guide)
        inputs[role] = {'filename': role + '.md', 'sha256': digest(guide)}
    snapshot = read_source(root / 'work' / 'input.json')
    (root / 'control' / 'source.json').write_text(json.dumps(snapshot), encoding='utf-8')
    job = {'inputs': inputs, 'output_filename': 'input_result.json'}
    manifest = layout(settings, job, root, snapshot, cache=False)
    return root, manifest, data, job


@pytest.mark.parametrize('phase', ['translate', 'review'])
def test_json_tools_preserve_original_records_without_csv_assumptions(settings, tmp_path, phase):
    root, manifest, data, job = setup_json_task(settings, tmp_path, phase)
    tools = TaskTools(root / 'work' / '.worker' / 'manifest.json')
    assert tools.prepare()['rows'] == 1
    assert tools.source() == data['rows']
    assert tools.source(offset=1) == []
    info = tools.inspect()
    assert info['input_contract'] == CONTRACT and info['phase'] == phase
    assert info['delivery_mode'] == 'final_message_json'
    assert 'headers' not in info and 'output_csv' not in info
    assert 'output_csv' not in manifest and 'translations' not in manifest
    assert tools.path(info['source_path']).read_bytes() == (root / 'input' / 'input.json').read_bytes()
    with pytest.raises(ValueError, match='CSV build is not applicable'):
        tools.build()
    assert not list(root.rglob('*.csv'))


@pytest.mark.parametrize('phase', ['translate', 'review'])
def test_json_standalone_helper_works_from_unrelated_cwd(settings, tmp_path, phase):
    root, manifest, data, job = setup_json_task(settings, tmp_path, phase)
    helper = root / 'work' / '.worker' / 'task_tools.py'
    for command in ('smoke', 'source', 'inspect'):
        done = subprocess.run([sys.executable, '-X', 'utf8', str(helper), command], cwd=tmp_path,
                              capture_output=True, timeout=15)
        assert done.returncode == 0, done.stderr.decode('utf-8', errors='replace')
        result = json.loads(done.stdout)
        if command == 'source':
            assert result['rows'] == data['rows']
            assert result['has_more'] is False
        elif command == 'smoke':
            assert result['marker'] == 'RUNTIME_JSON_HELPER_OK' and result['utf8'] == '中文测试'
    assert not list(root.rglob('*.csv'))


@pytest.mark.parametrize('phase', ['translate', 'review'])
def test_json_notes_request_only_final_json_and_preserve_server_settings(settings, tmp_path, phase):
    root, manifest, data, job = setup_json_task(settings, tmp_path, phase)
    notes = runtime_instructions(settings, root)
    assert 'FINAL MESSAGE' in notes and 'phase=' + phase in notes
    assert 'Then build the required CSV' not in notes and "' build" not in notes
    assert 'row_id' in notes and 'task_tools.py' in notes
    values = config_arguments(settings, root)
    assert not any(value.startswith(('model=', 'model_provider=', 'model_reasoning_effort=')) for value in values)
    assert any('":root"="read"' in value for value in values)


@pytest.mark.parametrize('bad', [
    {'rows': []},
    {'rows': [{'row_id': '0', 'target_text': '你好', 'term_decisions': [], 'extra': 'bad'}]},
    {'rows': [{'row_id': '0', 'target_text': 3, 'term_decisions': []}]},
    {'rows': [{'row_id': '0', 'target_text': '  ', 'term_decisions': []}]},
    {'rows': [{'row_id': '0', 'target_text': '你好', 'term_decisions': [{'source_text': 'Hello'}]}]},
    {'rows': [{'row_id': False, 'target_text': '你好', 'term_decisions': []}]},
])
def test_output_schema_is_exact_and_complete(bad):
    with pytest.raises(JobError) as error:
        check_json_result(packet(), bad)
    assert error.value.code == 'INVALID_JSON_RESULT'


def test_ten_row_delivery_maps_by_id_and_preserves_business_fields(settings):
    data = packet()
    original = data['rows'][0]
    data['rows'] = [{**copy.deepcopy(original), 'row_id': str(i)} for i in range(10)]
    for i, row in enumerate(data['rows']):
        row['record']['business_id'] = {'code': '000' + str(i), 'null': None, 'flag': False}
    result = {'rows': [{'row_id': str(i), 'target_text': '译文' + str(i), 'term_decisions': []} for i in reversed(range(10))]}
    runner = JsonRunner(json.dumps(result, ensure_ascii=False))
    with TestClient(create_app(settings, runner=runner)) as client:
        ready = client.get('/v1/ready', headers=auth(settings)).json()
        assert ready['service_version'] == __version__ == '0.4.0'
        assert ready['input_contracts'] == ['siemens-json/2', CONTRACT, 'table-csv/1']
        key = str(uuid.uuid4())
        accepted = submit(client, settings, data, key)
        job = wait(client, settings, accepted.json()['job_id'])
        assert job['status'] == 'completed'
        artifacts = {a['kind']: a for a in job['artifacts']}
        assert artifacts['final_response']['filename'] == 'final_response.json'
        assert client.get(artifacts['translation']['download_path'], headers=auth(settings)).json() == result
        task_root = settings.data_root / 'jobs' / job['job_id']
        assert json.loads((task_root / 'input' / 'input.json').read_text()) == data
        assert (task_root / 'input' / 'input.json').read_bytes() == (task_root / 'work' / 'input.json').read_bytes()
        assert json.loads((task_root / 'control' / 'source.json').read_text()) == data
        assert not list(task_root.rglob('*.csv'))
        assert submit(client, settings, data, key).json()['job_id'] == job['job_id']
        assert runner.calls == 1


@pytest.mark.parametrize('mode', ['missing', 'duplicate', 'wrong'])
def test_mismatched_row_ids_are_never_successful(settings, mode):
    data = packet()
    data['rows'].append({**copy.deepcopy(data['rows'][0]), 'row_id': '1'})
    result = reply()
    if mode == 'duplicate':
        result['rows'].append(copy.deepcopy(result['rows'][0]))
    elif mode == 'wrong':
        result['rows'].append({**copy.deepcopy(result['rows'][0]), 'row_id': '2'})
    with TestClient(create_app(settings, runner=JsonRunner(json.dumps(result)))) as client:
        accepted = submit(client, settings, data, str(uuid.uuid4()))
        job = wait(client, settings, accepted.json()['job_id'])
        assert job['status'] == 'failed' and job['error']['code'] == 'INVALID_JSON_RESULT'
        assert not any(a['kind'] == 'translation' for a in job['artifacts'])


@pytest.mark.parametrize('value', [r'{"rows":[{"row_id":"0","target_text":"\ud800","term_decisions":[]}]}',
                                   'Explanation\n' + json.dumps(reply()), json.dumps(reply()) + '\nDone'])
def test_non_json_or_non_utf8_result_is_rejected(settings, value):
    with TestClient(create_app(settings, runner=JsonRunner(value))) as client:
        accepted = submit(client, settings, packet(), str(uuid.uuid4()))
        job = wait(client, settings, accepted.json()['job_id'])
        assert job['status'] == 'failed' and job['error']['code'] == 'INVALID_JSON_RESULT'


def test_invalid_source_never_starts_runner(settings):
    data = packet()
    data['rows'][0]['record']['source_text'] = '\ud800'
    runner = JsonRunner('{}')
    with TestClient(create_app(settings, runner=runner)) as client:
        response = submit(client, settings, data, str(uuid.uuid4()))
        assert response.status_code == 422 and response.json()['error']['code'] == 'INVALID_JSON_SOURCE'
        assert runner.calls == 0


def test_service_secret_cannot_be_published_in_translation(settings):
    result = reply()
    result['rows'][0]['target_text'] = settings.service_token.get_secret_value()
    with TestClient(create_app(settings, runner=JsonRunner(json.dumps(result)))) as client:
        accepted = submit(client, settings, packet(), str(uuid.uuid4()))
        job = wait(client, settings, accepted.json()['job_id'])
        assert job['status'] == 'failed' and job['error']['code'] == 'INVALID_ARTIFACT'
        assert not any(a['kind'] == 'translation' for a in job['artifacts'])
