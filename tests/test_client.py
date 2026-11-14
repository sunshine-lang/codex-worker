import asyncio
import hashlib
import importlib.util
import json
from pathlib import Path

import httpx
import pytest


spec = importlib.util.spec_from_file_location('worker_client', Path(__file__).parents[1] / 'integrations/langflow/client_core.py')
client_core = importlib.util.module_from_spec(spec)
spec.loader.exec_module(client_core)


def test_submit_retry_keeps_same_key_and_original_prompt(tmp_path):
    prompt = tmp_path / 'prompt.md'
    prompt.write_bytes(b'\xef\xbb\xbf  original\r\n ')
    seen = []
    def handler(request):
        seen.append((request.headers['idempotency-key'], request.read()))
        if len(seen) == 1:
            raise httpx.ReadError('response was lost', request=request)
        return httpx.Response(202, json={'job_id': 'job_same', 'status': 'queued'})
    async def run():
        client = client_core.RemoteClient('https://worker.test', 'service-token', transport=httpx.MockTransport(handler))
        try:
            await client.submit({'prompt_file': prompt}, {'timeout_seconds': 1800}, 'same-key')
        finally:
            await client.close()
    asyncio.run(run())
    assert [key for key, _ in seen] == ['same-key', 'same-key']
    assert all(prompt.read_bytes() in body for _, body in seen)


def test_lost_submit_response_then_rejection_remains_uncertain(tmp_path):
    path = tmp_path / 'prompt.md'
    path.write_bytes(b'original')
    seen = []
    def handler(request):
        seen.append(request.headers['idempotency-key'])
        if len(seen) == 1:
            raise httpx.ReadError('accepted response lost', request=request)
        return httpx.Response(401, json={})
    async def run():
        client = client_core.RemoteClient('https://worker.test', 'token', transport=httpx.MockTransport(handler))
        try:
            with pytest.raises(client_core.RemoteWorkerError) as error:
                await client.submit({'prompt_file': path}, {}, 'same-key')
            assert error.value.rejected is False
        finally:
            await client.close()
    asyncio.run(run())
    assert seen == ['same-key', 'same-key']


def test_reference_resumes_then_only_explicit_rerun_rotates_key():
    identity = {'user_id': 'u', 'flow_id': 'f', 'service': 'https://worker.test'}
    first = client_core.choose_reference(None, identity, 'hash', '提交或继续')
    assert client_core.choose_reference(first, identity, 'hash', '重新试跑')['request_key'] == first['request_key']
    with pytest.raises(client_core.RemoteWorkerError):
        client_core.choose_reference(first, identity, 'different', '提交或继续')
    first.update(status='completed', job_id='job_first')
    assert client_core.choose_reference(first, identity, 'hash', '提交或继续')['job_id'] == 'job_first'
    second = client_core.choose_reference(first, identity, 'hash', '重新试跑')
    assert second['request_key'] != first['request_key']
    assert second['history'][0]['job_id'] == 'job_first'
    with pytest.raises(client_core.RemoteWorkerError):
        client_core.choose_reference(first, {**identity, 'user_id': 'another'}, 'hash', '查询')


def test_download_verifies_hash_and_refuses_external_url(tmp_path):
    body = b'CSV artifact fixture'
    entry = {'id': 'artifact_translation', 'filename': 'result.csv', 'bytes': len(body),
             'sha256': hashlib.sha256(body).hexdigest(), 'download_path': '/v1/jobs/job_test/artifacts/artifact_translation'}
    requests = []
    def handler(request):
        requests.append(str(request.url))
        return httpx.Response(200, content=body)
    async def run():
        client = client_core.RemoteClient('https://worker.test/prefix', 'token', transport=httpx.MockTransport(handler))
        try:
            path = await client.download('job_test', entry, tmp_path)
            assert path.read_bytes() == body
            with pytest.raises(client_core.RemoteWorkerError):
                await client.download('job_test', {**entry, 'download_path': 'https://evil.test/steal'}, tmp_path)
            with pytest.raises(client_core.RemoteWorkerError):
                await client.download('job_test', {**entry, 'sha256': '0' * 64}, tmp_path)
        finally:
            await client.close()
    asyncio.run(run())
    assert all(url.startswith('https://worker.test/prefix/v1/jobs/') for url in requests)
    assert not list(tmp_path.glob('*.partial'))


@pytest.mark.parametrize('url', ['http://public.example', 'https://token@worker.test', 'https://worker.test/?secret=x', 'file:///etc/passwd'])
def test_service_url_rejects_unsafe_configuration(url):
    with pytest.raises(client_core.RemoteWorkerError):
        client_core.worker_url(url)


def test_connection_failure_does_not_expose_token():
    def handler(request):
        return httpx.Response(401, json={'error': {'message': 'token-value'}})
    async def run():
        client = client_core.RemoteClient('https://worker.test', 'token-value', transport=httpx.MockTransport(handler))
        try:
            with pytest.raises(client_core.RemoteWorkerError) as error:
                await client.status('job_test')
            assert 'token-value' not in str(error.value)
        finally:
            await client.close()
    asyncio.run(run())


def test_public_http_is_explicit_and_accepts_pasted_health_url():
    url = 'http://172.188.217.22:8787/proxy/healthz/'
    with pytest.raises(client_core.RemoteWorkerError):
        client_core.worker_url(url, allow_private_http=True)
    assert client_core.worker_url(url, allow_insecure_http=True) == 'http://172.188.217.22:8787/proxy'
    assert client_core.worker_url('https://worker.test/v1/ready') == 'https://worker.test'
    for unsafe in ['http://token@worker.test', 'http://worker.test/?token=x', 'file:///tmp/file', 'http://worker.test:wrong']:
        with pytest.raises(client_core.RemoteWorkerError):
            client_core.worker_url(unsafe, allow_insecure_http=True)


@pytest.mark.parametrize('token,ready_status', [('', 200), ('worker-token', 200), ('worker-token', 503)])
def test_probe_never_submits_a_job_and_health_never_sends_token(token, ready_status):
    seen = []
    def handler(request):
        seen.append(request.url.path)
        assert request.method == 'GET'
        if request.url.path == '/healthz':
            assert 'Authorization' not in request.headers
            return httpx.Response(200, json={'status': 'alive'})
        assert request.url.path == '/v1/ready'
        assert request.headers['Authorization'] == 'Bearer worker-token'
        return httpx.Response(ready_status, json={'ready': ready_status == 200, 'message': 'Sandbox not ready' if ready_status == 503 else '',
                                                 'codex_version': 'codex-cli fixture'})
    async def run():
        client = client_core.RemoteClient('https://worker.test', token, transport=httpx.MockTransport(handler))
        try:
            result = await client.probe()
            assert result['reachable'] and result['model_called'] is False
            assert result['ready'] is (ready_status == 200 if token else None)
            if ready_status == 503:
                assert result['message'] == 'Sandbox not ready'
        finally:
            await client.close()
    asyncio.run(run())
    assert seen == (['/healthz', '/v1/ready'] if token else ['/healthz'])


def test_rejected_request_error_and_empty_token_redaction():
    async def run():
        client = client_core.RemoteClient('https://worker.test')
        try:
            error = client.error(httpx.Response(422, json={'message': 'Model is not allowed'}))
            assert error.rejected and error.status == 422
            assert 'Model is not allowed' in str(error)
            legacy = client.error(httpx.Response(422, json={'error': {'code': 'INVALID_OPTIONS', 'message': '模型参数无效或包含不允许的字段。'}}))
            assert '0.2.0' in str(legacy) and '不发送模型 ID' in str(legacy)
            assert not client.error(httpx.Response(502)).rejected
            assert not client.error(httpx.Response(409)).rejected
        finally:
            await client.close()
    asyncio.run(run())


def test_reference_updates_allow_queries_without_overwriting_a_later_run(tmp_path):
    path, identity = tmp_path / 'reference.json', {'user_id': 'u'}
    first = client_core.reserve_reference(path, identity, 'first', '提交或继续')
    client_core.update_reference(path, identity, first['request_key'], job_id='job_one', status='running')
    assert not client_core.update_reference(path, identity, first['request_key'], status='failed', only_unsubmitted=True)
    assert client_core.read_reference(path)['status'] == 'running'
    assert client_core.reserve_reference(path, identity, '', '取消')['job_id'] == 'job_one'
    client_core.update_reference(path, identity, first['request_key'], status='completed')
    client_core.update_reference(path, identity, first['request_key'], status='running')
    assert client_core.read_reference(path)['status'] == 'completed'
    second = client_core.reserve_reference(path, identity, 'second', '提交或继续')
    assert not client_core.update_reference(path, identity, first['request_key'], status='completed')
    assert client_core.read_reference(path)['request_key'] == second['request_key']


@pytest.mark.parametrize('lose_response', [False, True])
def test_real_http_contract_roundtrip_with_simulated_codex(settings, tmp_path, lose_response):
    from codex_remote_worker.app import create_app
    from conftest import FakeRunner
    import uuid
    uploads = tmp_path / 'caller-files'
    uploads.mkdir()
    files = {}
    for role, name, data in [('source_file', 'case.csv', b'en,zh-CN\nHello,\n'),
                             ('prompt_file', 'prompt.md', b'\xef\xbb\xbfOriginal\r\n'),
                             ('project_style', 'style.md', b'style'), ('general_style', 'general.doc', b'fixture')]:
        path = uploads / name
        path.write_bytes(data)
        files[role] = path
    fake = FakeRunner()
    app = create_app(settings, runner=fake)
    class LostResponseTransport(httpx.AsyncBaseTransport):
        def __init__(self):
            self.backend = httpx.ASGITransport(app=app)
            self.dropped = False
        async def handle_async_request(self, request):
            response = await self.backend.handle_async_request(request)
            if lose_response and not self.dropped and request.method == 'POST' and request.url.path == '/v1/jobs':
                self.dropped = True
                await response.aclose()
                raise httpx.ReadError('Accepted response lost', request=request)
            return response
        async def aclose(self):
            await self.backend.aclose()
    async def run():
        async with app.router.lifespan_context(app):
            client = client_core.RemoteClient('https://worker.test', settings.service_token.get_secret_value(),
                                              transport=LostResponseTransport())
            try:
                submitted = await client.submit(files, {'timeout_seconds': 1800}, str(uuid.uuid4()))
                for _ in range(200):
                    job = await client.status(submitted['job_id'])
                    if job['status'] in client_core.REMOTE_TERMINAL:
                        break
                    await asyncio.sleep(.01)
                assert job['status'] == 'completed', job
                entry = next(a for a in job['artifacts'] if a['kind'] == 'translation')
                result = await client.download(job['job_id'], entry, tmp_path / 'caller-results')
                assert '你好' in result.read_text(encoding='utf-8-sig')
                assert fake.prompts == [files['prompt_file'].read_bytes()]
                assert fake.calls == 1
            finally:
                await client.close()
    asyncio.run(run())
