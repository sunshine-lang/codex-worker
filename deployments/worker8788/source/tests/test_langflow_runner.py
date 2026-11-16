"""Run the actual node method with HTTP and Langflow storage replaced by local fixtures."""
import asyncio
import copy
import json
import inspect
import hashlib
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

pytest.importorskip('lfx', reason='Node execution checks require the Langflow development environment')


def load_runner(monkeypatch, tmp_path, handler, *, preflight=True):
    root = Path(__file__).parents[1] / 'integrations/langflow'
    namespace = {'__name__': 'remote_runner_test'}
    exec(compile((root / 'client_core.py').read_text() + '\n' + (root / 'runner.py').read_text(), str(root / 'runner.py'), 'exec'), namespace)
    original_client = namespace['RemoteClient']
    async def transport(request):
        if preflight is not None and request.url.path == '/healthz':
            return httpx.Response(200, json={'status': 'alive'})
        if preflight is not None and request.url.path == '/v1/ready':
            return httpx.Response(200 if preflight else 503, json={'ready': preflight, 'message': 'fixture readiness'})
        result = handler(request)
        return await result if inspect.isawaitable(result) else result
    monkeypatch.setitem(namespace, 'RemoteClient', lambda *args, **kwargs: original_client(*args, **kwargs, transport=httpx.MockTransport(transport)))
    monkeypatch.setitem(namespace, 'component_file_access_scopes', lambda node: ['fixture-user'])
    monkeypatch.setitem(namespace, 'enforce_local_file_access', lambda path, **kwargs: Path(path))
    monkeypatch.setitem(namespace, 'get_storage_service', lambda: SimpleNamespace(build_full_path=lambda user, name: str(tmp_path / name)))
    node = SimpleNamespace(service_url='https://worker.test', service_token='worker-token', allow_private_http=False,
        allow_insecure_http=False, action='自动运行', user_id='fixture-user', graph=SimpleNamespace(flow_id='fixture-flow'),
        _id='fixture-node', timeout_seconds=1800, wait_seconds=0, langflow_public_url='', bundle=None)
    files = {}
    for role in ['source_file', 'prompt_file', 'project_style', 'general_style']:
        path = tmp_path / role
        path.write_bytes(b'original material')
        files[role] = str(path)
    node.bundle = SimpleNamespace(data={'user_id': node.user_id, 'flow_id': node.graph.flow_id, 'files': files})
    return namespace['RemoteCodexTranslation'].run_remote, node


def test_check_connection_needs_no_files_token_model_or_graph(monkeypatch, tmp_path):
    seen = []
    def handler(request):
        seen.append(request.url.path)
        assert request.method == 'GET' and 'authorization' not in request.headers
        return httpx.Response(200, json={'status': 'alive'})
    run, node = load_runner(monkeypatch, tmp_path, handler, preflight=None)
    node.action, node.bundle, node.service_token, node.graph = '检查连接', None, '', None
    result = asyncio.run(run(node))
    assert '网络：已连通' in result.text and '未调用模型' in result.text
    assert seen == ['/healthz']
    assert not list(tmp_path.rglob('reference.json'))


def test_rejected_material_can_be_corrected_without_resetting_state(monkeypatch, tmp_path):
    keys = []
    def handler(request):
        if request.method == 'POST':
            body = request.read()
            assert b'{"timeout_seconds": 1800}' in body
            assert all(name not in body for name in [b'model_id', b'reasoning_effort', b'web_search'])
            keys.append(request.headers['idempotency-key'])
            if len(keys) == 1:
                return httpx.Response(422, json={'error': {'message': 'Invalid input file'}})
            return httpx.Response(202, json={'job_id': 'job_good', 'status': 'queued'})
        return httpx.Response(200, json={'job_id': 'job_good', 'status': 'running', 'artifacts': []})
    run, node = load_runner(monkeypatch, tmp_path, handler)
    async def scenario():
        first = await run(node)
        assert '提交被拒绝' in first.text
        Path(node.bundle.data['files']['prompt_file']).write_bytes(b'Corrected original material')
        second = await run(node)
        assert 'job_good' in second.text and '执行中' in second.text
    asyncio.run(scenario())
    assert len(keys) == 2 and keys[0] != keys[1]
    assert json.loads(next(tmp_path.rglob('reference.json')).read_text())['status'] == 'running'


def test_auto_resume_and_cancel_do_not_need_original_files_or_model(monkeypatch, tmp_path):
    cancelled, posts = False, []
    def handler(request):
        nonlocal cancelled
        if request.method == 'POST':
            posts.append(request.url.path)
            if request.url.path.endswith('/cancel'):
                cancelled = True
            else:
                return httpx.Response(202, json={'job_id': 'job_good', 'status': 'queued'})
        return httpx.Response(200, json={'job_id': 'job_good', 'status': 'cancelled' if cancelled else 'running', 'artifacts': []})
    run, node = load_runner(monkeypatch, tmp_path, handler)
    async def scenario():
        await run(node)
        node.bundle, node.action = None, '自动运行'
        assert '执行中' in (await run(node)).text
        node.action = '取消任务'
        assert '已取消' in (await run(node)).text
    asyncio.run(scenario())
    assert posts == ['/v1/jobs', '/v1/jobs/job_good/cancel']


def test_cancel_can_run_while_another_request_is_waiting(monkeypatch, tmp_path):
    async def scenario():
        waiting, release = asyncio.Event(), asyncio.Event()
        cancelled = False
        async def handler(request):
            nonlocal cancelled
            if request.url.path == '/v1/jobs':
                return httpx.Response(202, json={'job_id': 'job_good', 'status': 'queued'})
            if request.url.path.endswith('/cancel'):
                cancelled = True
            elif not cancelled:
                waiting.set()
                await release.wait()
                return httpx.Response(200, json={'job_id': 'job_good', 'status': 'running', 'artifacts': []})
            return httpx.Response(200, json={'job_id': 'job_good', 'status': 'cancelled', 'artifacts': []})
        run, node = load_runner(monkeypatch, tmp_path, handler)
        original = asyncio.create_task(run(node))
        try:
            await asyncio.wait_for(waiting.wait(), 2)
            cancel_node = copy.copy(node)
            cancel_node.action, cancel_node.bundle = '取消任务', None
            assert '已取消' in (await asyncio.wait_for(run(cancel_node), 2)).text
        finally:
            release.set()
            await original
        assert json.loads(next(tmp_path.rglob('reference.json')).read_text())['status'] == 'cancelled'
    asyncio.run(scenario())


def test_auto_reuses_finished_task_and_recovers_download_without_ready_or_model(monkeypatch, tmp_path):
    state = {'completed': False, 'ready': True, 'posts': 0, 'checks': 0, 'downloads': 0, 'publications': 0}
    content = b'completed CSV fixture'
    entry = {'id': 'artifact_translation', 'filename': 'result.csv', 'bytes': len(content),
             'sha256': hashlib.sha256(content).hexdigest(), 'download_path': '/v1/jobs/job_1/artifacts/artifact_translation'}
    def handler(request):
        path = request.url.path
        if path == '/healthz':
            state['checks'] += 1
            assert state['ready'], 'Existing results must not require readiness'
            return httpx.Response(200, json={'status': 'alive'})
        if path == '/v1/ready':
            return httpx.Response(200, json={'ready': True})
        if path == '/v1/jobs':
            state['posts'] += 1
            return httpx.Response(202, json={'job_id': f"job_{state['posts']}", 'status': 'queued'})
        if '/artifacts/' in path:
            state['downloads'] += 1
            if state['downloads'] == 1:
                raise httpx.ReadError('Download interrupted', request=request)
            return httpx.Response(200, content=content)
        job_id = path.rsplit('/', 1)[-1]
        done = state['completed'] and job_id == 'job_1'
        return httpx.Response(200, json={'job_id': job_id, 'status': 'completed' if done else 'running',
                                      'artifacts': [entry] if done else []})
    run, node = load_runner(monkeypatch, tmp_path, handler, preflight=None)
    async def publish(path, filename):
        state['publications'] += 1
        return {'id': 'download-id', 'path': str(path)}
    node.publish_file, node.get_full_path = publish, lambda path: path
    async def scenario():
        assert '执行中' in (await run(node)).text
        state.update(completed=True, ready=False)
        assert 'Download interrupted' in (await run(node)).text
        result = await run(node)
        assert '本次未重新翻译' in result.text and '/api/v2/files/download-id' in result.text
        assert '本次未重新翻译' in (await run(node)).text
        assert state['posts'] == 1 and state['checks'] == 1 and state['publications'] == 1
        state['ready'] = True
        node.action = '重新翻译'
        assert 'job_2' in (await run(node)).text
        assert state['posts'] == 2 and state['checks'] == 2
    asyncio.run(scenario())


@pytest.mark.parametrize('terminal', ['failed', 'cancelled', 'interrupted'])
def test_auto_does_not_rerun_failed_or_cancelled_task(monkeypatch, tmp_path, terminal):
    posts = []
    def handler(request):
        if request.method == 'POST':
            posts.append(request.headers['idempotency-key'])
            return httpx.Response(202, json={'job_id': 'job_existing', 'status': 'queued'})
        return httpx.Response(200, json={'job_id': 'job_existing', 'status': terminal, 'artifacts': []})
    run, node = load_runner(monkeypatch, tmp_path, handler)
    async def scenario():
        await run(node)
        assert '本次未重新翻译' in (await run(node)).text
    asyncio.run(scenario())
    assert len(posts) == 1


def test_auto_recovers_unknown_submission_without_ready_check(monkeypatch, tmp_path):
    keys, checks = [], []
    def handler(request):
        if request.url.path in {'/healthz', '/v1/ready'}:
            checks.append(request.url.path)
            assert len(checks) <= 2
            return httpx.Response(200, json={'status': 'alive'} if request.url.path == '/healthz' else {'ready': True})
        if request.method == 'POST':
            keys.append(request.headers['idempotency-key'])
            if len(keys) <= 2:
                raise httpx.ReadError('Accepted response lost', request=request)
            return httpx.Response(200, json={'job_id': 'job_existing', 'status': 'completed', 'reused': True})
        return httpx.Response(200, json={'job_id': 'job_existing', 'status': 'completed', 'artifacts': []})
    run, node = load_runner(monkeypatch, tmp_path, handler, preflight=None)
    async def scenario():
        assert '提交响应未收到' in (await run(node)).text
        assert '本次未重新翻译' in (await run(node)).text
    asyncio.run(scenario())
    assert len(keys) == 3 and len(set(keys)) == 1


def test_new_task_readiness_failure_does_not_submit_and_can_be_corrected(monkeypatch, tmp_path):
    state = {'ready': False, 'posts': 0}
    def handler(request):
        if request.url.path == '/healthz':
            return httpx.Response(200, json={'status': 'alive'})
        if request.url.path == '/v1/ready':
            return httpx.Response(200 if state['ready'] else 503, json={'ready': state['ready'], 'message': 'fixture not ready'})
        if request.method == 'POST':
            state['posts'] += 1
            return httpx.Response(202, json={'job_id': 'job_new', 'status': 'queued'})
        return httpx.Response(200, json={'job_id': 'job_new', 'status': 'running', 'artifacts': []})
    run, node = load_runner(monkeypatch, tmp_path, handler, preflight=None)
    async def scenario():
        assert '尚未就绪' in (await run(node)).text
        assert state['posts'] == 0
        Path(node.bundle.data['files']['prompt_file']).write_bytes(b'Changed prompt')
        state['ready'] = True
        assert 'job_new' in (await run(node)).text
    asyncio.run(scenario())
    assert state['posts'] == 1


def test_duplicate_files_are_named_before_network_or_reference_write(monkeypatch, tmp_path):
    def handler(request):
        pytest.fail('Duplicate materials must be caught before any HTTP request')
    run, node = load_runner(monkeypatch, tmp_path, handler, preflight=None)
    node.bundle.data['files']['project_style'] = node.bundle.data['files']['prompt_file']
    result = asyncio.run(run(node))
    assert '翻译 Prompt' in result.text and '项目风格指南' in result.text and '文件名相同' in result.text
    assert not list(tmp_path.rglob('reference.json'))


def test_auto_refreshes_stale_running_state_before_accepting_different_files(monkeypatch, tmp_path):
    posts, status = [], {'value': 'running'}
    def handler(request):
        if request.method == 'POST':
            posts.append(request.headers['idempotency-key'])
            return httpx.Response(202, json={'job_id': f'job_{len(posts)}', 'status': 'queued'})
        return httpx.Response(200, json={'job_id': request.url.path.rsplit('/', 1)[-1], 'status': status['value'], 'artifacts': []})
    run, node = load_runner(monkeypatch, tmp_path, handler)
    async def scenario():
        await run(node)
        Path(node.bundle.data['files']['prompt_file']).write_bytes(b'New input')
        assert '上一任务尚未结束' in (await run(node)).text
        status['value'] = 'completed'
        assert 'job_2' in (await run(node)).text
    asyncio.run(scenario())
    assert len(posts) == 2 and posts[0] != posts[1]
