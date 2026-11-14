from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import time
import uuid

from fastapi.testclient import TestClient
import pytest

from codex_remote_worker.app import create_app
from codex_remote_worker.common import TERMINAL, digest
from codex_remote_worker.store import Store
from conftest import FakeRunner


def auth(settings):
    return {'Authorization': 'Bearer ' + settings.service_token.get_secret_value()}


def upload(client, settings, key=None, prompt=b'Original prompt\r\n', options=None, names=None):
    names = names or {}
    files = {
        'source_file': (names.get('source_file', 'case.csv'), b'en,zh-CN,TM\nHello,,reference\n'),
        'prompt_file': (names.get('prompt_file', 'prompt.md'), prompt),
        'project_style': (names.get('project_style', 'style.md'), b'Project style'),
        'general_style': (names.get('general_style', 'general.doc'), b'Example fixture, not a real DOC'),
    }
    return client.post('/v1/jobs', headers={**auth(settings), 'Idempotency-Key': key or str(uuid.uuid4())},
        files=files, data={'options': json.dumps(options if options is not None else {})})


def wait(client, settings, job_id, states=TERMINAL):
    until = time.monotonic() + 5
    while time.monotonic() < until:
        response = client.get('/v1/jobs/' + job_id, headers=auth(settings))
        assert response.status_code == 200, response.text
        job = response.json()
        if job['status'] in states:
            return job
        time.sleep(.01)
    raise AssertionError(job)


def test_delivery_and_verbatim_prompt(settings, runner):
    prompt = b'\xef\xbb\xbf  # Original\r\n  do exactly this\r\n '
    app = create_app(settings, runner=runner)
    with TestClient(app) as client:
        response = upload(client, settings, prompt=prompt)
        assert response.status_code == 202, response.text
        job = wait(client, settings, response.json()['job_id'])
        assert job['status'] == 'completed', job
        assert runner.prompts == [prompt]
        assert job['qa'] == {'rows': 1, 'errors': 0, 'warnings': 0}
        assert job['language_quality'] == 'not_independently_verified'
        assert job['configuration_source'] == 'server_codex'
        for artifact in job['artifacts']:
            content = client.get(artifact['download_path'], headers=auth(settings))
            assert content.status_code == 200
            assert digest(content.content) == artifact['sha256']
            assert len(content.content) == artifact['bytes']
            assert b'fixture-provider-key' not in content.content
        root = settings.data_root / 'jobs' / job['job_id']
        assert not (root / 'work' / 'AGENTS.md').exists()
        assert not (root / 'work' / 'task.json').exists()


def test_authentication_covers_all_private_routes(settings, runner):
    with TestClient(create_app(settings, runner=runner)) as client:
        assert client.get('/healthz').status_code == 200
        for method, path in [('get', '/v1/ready'), ('post', '/v1/jobs'), ('get', '/v1/jobs/unknown'),
                             ('post', '/v1/jobs/unknown/cancel'), ('get', '/v1/jobs/j/artifacts/a')]:
            response = getattr(client, method)(path)
            assert response.status_code == 401
        assert client.get('/openapi.json').status_code == 404


def test_concurrent_idempotency_and_explicit_rerun(settings):
    runner = FakeRunner(blocked=True)
    key = str(uuid.uuid4())
    with TestClient(create_app(settings, runner=runner)) as client:
        with ThreadPoolExecutor(max_workers=4) as pool:
            responses = list(pool.map(lambda _: upload(client, settings, key), range(4)))
        assert sorted(r.status_code for r in responses) == [200, 200, 200, 202]
        assert len({r.json()['job_id'] for r in responses}) == 1
        first = responses[0].json()['job_id']
        wait(client, settings, first, {'running'})
        assert runner.calls == 1
        assert upload(client, settings, key, prompt=b'changed').status_code == 409
        different = upload(client, settings)
        assert different.status_code == 202
        assert different.json()['job_id'] != first
        runner.release.set()
        wait(client, settings, different.json()['job_id'])
        assert runner.calls == 2


def test_queue_capacity_cancel_and_duplicate_when_full(settings):
    settings.queue_capacity = 1
    runner = FakeRunner(blocked=True)
    with TestClient(create_app(settings, runner=runner)) as client:
        first = upload(client, settings).json()['job_id']
        wait(client, settings, first, {'running'})
        key = str(uuid.uuid4())
        second = upload(client, settings, key).json()['job_id']
        assert upload(client, settings).status_code == 429
        assert upload(client, settings, key).status_code == 200
        response = client.post(f'/v1/jobs/{second}/cancel', headers=auth(settings))
        assert response.status_code == 200 and response.json()['status'] == 'cancelled'
        assert client.post(f'/v1/jobs/{second}/cancel', headers=auth(settings)).json()['status'] == 'cancelled'
        assert client.post(f'/v1/jobs/{first}/cancel', headers=auth(settings)).status_code == 202
        wait(client, settings, first, {'cancelled'})
        assert runner.calls == 1


@pytest.mark.parametrize('mode,state', [('missing', 'failed'), ('error', 'failed'), ('changed', 'failed'), ('empty', 'needs_review')])
def test_no_false_success(settings, mode, state):
    with TestClient(create_app(settings, runner=FakeRunner(mode))) as client:
        job_id = upload(client, settings).json()['job_id']
        job = wait(client, settings, job_id)
        assert job['status'] == state, job
        if state == 'failed':
            assert not any(a['kind'] == 'translation' for a in job['artifacts'])


@pytest.mark.parametrize('names', [
    {'source_file': '../case.csv'}, {'source_file': r'C:\case.csv'}, {'source_file': 'CON.csv'},
    {'source_file': 'case.csv:stream'}, {'source_file': 'case.csv.'}, {'prompt_file': 'AGENTS.md'},
    {'prompt_file': 'Same.md', 'project_style': 'same.MD'},
])
def test_unsafe_names_rejected_before_execution(settings, runner, names):
    with TestClient(create_app(settings, runner=runner)) as client:
        assert upload(client, settings, names=names).status_code == 422
        assert not list((settings.data_root / 'staging').iterdir())
        assert not list((settings.data_root / 'jobs').iterdir())
        assert runner.calls == 0


@pytest.mark.parametrize('options', [
    {'model_id': 'test/model'}, {'reasoning_effort': 'low'}, {'web_search': False},
    {'timeout_seconds': 2}, {'timeout_seconds': '1800'}, {'command': 'anything'}, {'codex_home': 'C:/other'},
])
def test_request_cannot_override_codex_configuration(settings, runner, options):
    with TestClient(create_app(settings, runner=runner)) as client:
        assert upload(client, settings, options=options).status_code == 422
        assert runner.calls == 0


def test_upload_limit(settings, runner):
    settings.max_prompt_bytes = 8
    with TestClient(create_app(settings, runner=runner)) as client:
        assert upload(client, settings, prompt=b'x' * 9).status_code == 413
        assert runner.calls == 0
        assert client.post('/v1/jobs', headers={**auth(settings), 'Content-Length': str(100 * 1024**2)}).status_code == 413


def test_disk_full_admission(settings, runner, monkeypatch):
    import collections
    usage = collections.namedtuple('Usage', 'total used free')
    settings.min_free_bytes = 100
    monkeypatch.setattr('codex_remote_worker.files.shutil.disk_usage', lambda _: usage(100, 100, 0))
    with TestClient(create_app(settings, runner=runner)) as client:
        assert client.get('/v1/ready', headers=auth(settings)).status_code == 503
        assert upload(client, settings).status_code == 503


def test_artifact_tampering_and_links_fail_closed(settings, runner, tmp_path):
    with TestClient(create_app(settings, runner=runner)) as client:
        job = wait(client, settings, upload(client, settings).json()['job_id'])
        entry = next(a for a in job['artifacts'] if a['kind'] == 'translation')
        path = settings.data_root / 'jobs' / job['job_id'] / 'artifacts' / entry['filename']
        data = path.read_bytes()
        path.write_bytes(b'corrupt')
        assert client.get(entry['download_path'], headers=auth(settings)).status_code == 409
        path.unlink()
        external = tmp_path / 'outside.csv'
        external.write_bytes(data)
        try:
            path.symlink_to(external)
        except OSError:
            pytest.skip('symlinks unavailable to this account')
        assert client.get(entry['download_path'], headers=auth(settings)).status_code == 422


def test_restart_preserves_results_and_never_reruns_interrupted(settings):
    with TestClient(create_app(settings, runner=FakeRunner())) as client:
        first = wait(client, settings, upload(client, settings).json()['job_id'])
    store = Store(settings.data_root / 'jobs.sqlite3')
    store.update(first['job_id'], status='running')
    store.close()
    runner = FakeRunner()
    with TestClient(create_app(settings, runner=runner)) as client:
        job = wait(client, settings, first['job_id'])
        assert job['status'] == 'interrupted'
        assert runner.calls == 0


def test_restart_rechecks_without_model_and_completion_survives(settings):
    with TestClient(create_app(settings, runner=FakeRunner())) as client:
        first = wait(client, settings, upload(client, settings).json()['job_id'])
    store = Store(settings.data_root / 'jobs.sqlite3')
    store.update(first['job_id'], status='checking')
    store.close()
    runner = FakeRunner()
    with TestClient(create_app(settings, runner=runner)) as client:
        job = wait(client, settings, first['job_id'])
        assert job['status'] == 'completed'
        assert runner.calls == 0
        entry = next(a for a in job['artifacts'] if a['kind'] == 'translation')
        assert client.get(entry['download_path'], headers=auth(settings)).status_code == 200


def test_unsafe_recovery_halts_dispatch(settings):
    with TestClient(create_app(settings, runner=FakeRunner())) as client:
        job = wait(client, settings, upload(client, settings).json()['job_id'])
    store = Store(settings.data_root / 'jobs.sqlite3')
    store.update(job['job_id'], status='running')
    store.close()
    runner = FakeRunner()
    runner.recoverable = False
    with TestClient(create_app(settings, runner=runner)) as client:
        current = client.get('/v1/jobs/' + job['job_id'], headers=auth(settings)).json()
        assert current['status'] == 'cancelling'
        assert current['error']['code'] == 'PROCESS_CLEANUP_FAILED'
        assert upload(client, settings).status_code == 503
        assert runner.calls == 0
