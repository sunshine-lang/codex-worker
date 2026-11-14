import copy
import json
import uuid

from fastapi.testclient import TestClient
import pytest

from codex_remote_worker.app import create_app
from codex_remote_worker.common import JobError
from codex_remote_worker.json_delivery import CONTRACT, load_json, check_json_result
from test_jobs import auth, wait


def packet(phase='translate'):
    return {'contract': CONTRACT, 'phase': phase, 'rows': [{
        'row_id': '0', 'record': {'source_language': 'en', 'target_language': 'zh-CN',
        'source_text': 'Hello', 'target_text': '你好' if phase == 'review' else '',
        'context': None, 'tb_list': [], 'tm_list': []},
        'source_units': [{'id': 1, 'source': 'Hello'}], 'memory': {}, 'term_decisions': []}]}


def reply(phase='translate'):
    if phase == 'translate':
        return {'rows': [{'row_id': '0', 'target_text': '你好', 'term_decisions': []}]}
    return {'rows': [{'row_id': '0', 'decision': 'pass', 'issues': [],
                     'coverage': [{'id': 1, 'evidence': '你好'}], 'new_senses': []}]}


class JsonRunner:
    def __init__(self, result):
        self.result, self.calls = result, 0

    def ready(self):
        return {'ready': True}

    def recover(self, job):
        return True

    async def run(self, job, root, touch, stopped, redactor):
        self.calls += 1
        source = root / 'work' / job['inputs']['source_file']['filename']
        assert source.suffix == '.json'
        assert json.loads(source.read_text())['contract'] == CONTRACT
        # Deliberately create no output file: the final JSON message is the delivery.
        (root / 'control' / 'final_response.raw.md').write_text(self.result, encoding='utf-8')
        return {'exit_code': 0, 'turn_completed': True, 'turn_failed': False,
                'trace_complete': True, 'thread_id': 'fixture-independent-session'}


def submit(client, settings, data, key):
    files = {'source_file': ('input.json', json.dumps(data).encode()),
             'prompt_file': ('prompt.md', b'Return pure JSON'),
             'project_style': ('project.md', b'Style'), 'general_style': ('general.md', b'Style')}
    return client.post('/v1/jobs', headers={**auth(settings), 'Idempotency-Key': key},
                       files=files, data={'options': '{}'})


@pytest.mark.parametrize('phase', ['translate', 'review'])
def test_json_only_delivery_and_idempotency(settings, phase):
    result = reply(phase)
    runner = JsonRunner(json.dumps(result, ensure_ascii=False))
    with TestClient(create_app(settings, runner=runner)) as client:
        ready = client.get('/v1/ready', headers=auth(settings)).json()
        assert CONTRACT in ready['input_contracts']
        key = str(uuid.uuid4())
        accepted = submit(client, settings, packet(phase), key)
        assert accepted.status_code == 202, accepted.text
        job = wait(client, settings, accepted.json()['job_id'])
        assert job['status'] == 'completed', job
        artifact = next(a for a in job['artifacts'] if a['kind'] == 'translation')
        assert artifact['filename'].endswith('.json')
        assert client.get(artifact['download_path'], headers=auth(settings)).json() == result
        assert not list((settings.data_root / 'jobs' / job['job_id']).rglob('*.csv'))
        assert submit(client, settings, packet(phase), key).json()['job_id'] == job['job_id']
        assert runner.calls == 1


@pytest.mark.parametrize('bad', [
    '```json\n{}\n```', '{"rows":[],"rows":[]}', '{"rows":[]}',
    '{"rows":[{"row_id":"0","target_text":"","term_decisions":[]}]}',
    '{"rows":[{"row_id":"wrong","target_text":"你好","term_decisions":[]}]}',
])
def test_invalid_json_cannot_publish_success(settings, bad):
    with TestClient(create_app(settings, runner=JsonRunner(bad))) as client:
        response = submit(client, settings, packet(), str(uuid.uuid4()))
        job = wait(client, settings, response.json()['job_id'])
        assert job['status'] == 'failed'
        assert job['error']['code'] == 'INVALID_JSON_RESULT'
        assert not any(a['kind'] == 'translation' for a in job['artifacts'])


def test_review_cannot_replace_candidate_and_revise_is_not_pass(settings):
    result = reply('review')
    result['rows'][0]['target_text'] = '改写'
    with pytest.raises(JobError):
        check_json_result(packet('review'), result)
    result = reply('review')
    result['rows'][0].update(decision='revise', issues=['缺少条件语义'])
    with TestClient(create_app(settings, runner=JsonRunner(json.dumps(result)))) as client:
        response = submit(client, settings, packet('review'), str(uuid.uuid4()))
        job = wait(client, settings, response.json()['job_id'])
        assert job['status'] == 'needs_review'


def test_invalid_input_does_not_call_model(settings):
    runner = JsonRunner('{}')
    with TestClient(create_app(settings, runner=runner)) as client:
        data = packet()
        data['rows'].append(copy.deepcopy(data['rows'][0]))
        assert submit(client, settings, data, str(uuid.uuid4())).status_code == 422
        assert runner.calls == 0


@pytest.mark.parametrize('value', ['NaN', 'Infinity', '1e9999', '{"a":1,"a":2}'])
def test_strict_json(value):
    with pytest.raises(ValueError):
        load_json(value)
