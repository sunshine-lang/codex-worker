"""Embedded into the Langflow node; only stdlib and Langflow's installed httpx are needed."""
import asyncio
from contextlib import ExitStack
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import uuid
import unicodedata
from urllib.parse import urlsplit, urlunsplit

import httpx


REMOTE_TERMINAL = {'completed', 'needs_review', 'failed', 'cancelled', 'interrupted'}


class RemoteWorkerError(Exception):
    def __init__(self, message, *, status=None, rejected=False):
        super().__init__(message)
        self.status = status
        self.rejected = rejected


def worker_url(value, allow_private_http=False, allow_insecure_http=False):
    value = value.strip().rstrip('/')
    url = urlsplit(value)
    if url.username or url.password or url.query or url.fragment or not url.hostname:
        raise RemoteWorkerError('服务地址不能含凭据、查询参数或片段。')
    try:
        url.port
    except ValueError:
        raise RemoteWorkerError('服务地址的端口不正确。') from None
    allowed_http = url.hostname == 'localhost'
    try:
        address = ipaddress.ip_address(url.hostname)
        allowed_http = address.is_loopback or (allow_private_http and address.is_private)
    except ValueError:
        pass
    if url.scheme != 'https' and not (url.scheme == 'http' and (allowed_http or allow_insecure_http)):
        raise RemoteWorkerError('请使用 HTTPS 或受保护私网地址；公网 HTTP 联调需明确开启“临时允许明文 HTTP”。')
    path = url.path.rstrip('/')
    for suffix in ['/healthz', '/v1/ready']:
        if path.endswith(suffix):
            path = path[:-len(suffix)]
            break
    return urlunsplit((url.scheme, url.netloc, path, '', ''))


def remote_text(value, token=''):
    text = str(value)
    if token:
        text = text.replace(token, '[REDACTED]')
    return re.sub(r'\bsk-[A-Za-z0-9_-]{12,}', '[REDACTED]', text)


def remote_id(value, prefix):
    if not isinstance(value, str) or not re.fullmatch(prefix + r'[a-zA-Z0-9_-]{1,100}', value):
        raise RemoteWorkerError('远端返回了无效的任务或产物编号。')
    return value


def local_hash(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def validate_upload_files(files):
    labels = {'source_file': '待译文件', 'prompt_file': '翻译 Prompt',
              'project_style': '项目风格指南', 'general_style': '通用准则'}
    if set(files) != set(labels):
        raise RemoteWorkerError('新任务需要四份有效资料；请在第一个节点重新上传或绑定缺少的文件。')
    seen = {}
    for role, value in files.items():
        path = Path(value)
        if not path.is_file():
            raise RemoteWorkerError(f'「{labels[role]}」的文件不可读取，请在当前 Langflow 重新上传或绑定。')
        key = unicodedata.normalize('NFC', path.name).casefold()
        if key in seen:
            raise RemoteWorkerError(f'「{labels[seen[key]]}」与「{labels[role]}」的上传文件名相同：{path.name}。请检查两个上传框是否重复绑定，或调整实际文件名后重新上传。')
        seen[key] = role


def save_reference(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with temporary.open('x', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class ReferenceLock:
    def __init__(self, path):
        self.stream = Path(path).open('a+b')
        self.stream.seek(0)
        if not self.stream.read(1):
            self.stream.write(b'0')
            self.stream.flush()
        self.stream.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.stream.close()
            raise RemoteWorkerError('这个节点正在处理另一条请求，请稍后继续查询。') from None

    def close(self):
        self.stream.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def read_reference(path):
    return json.loads(Path(path).read_text(encoding='utf-8')) if Path(path).exists() else None


def reserve_reference(path, identity, fingerprint, action):
    path = Path(path)
    with ReferenceLock(path.with_suffix('.lock')):
        previous = read_reference(path)
        reference = choose_reference(previous, identity, fingerprint, action)
        save_reference(path, reference)
        return reference


def update_reference(path, identity, key, *, only_unsubmitted=False, **fields):
    path = Path(path)
    with ReferenceLock(path.with_suffix('.lock')):
        current = read_reference(path)
        if not current or current.get('identity') != identity or current.get('request_key') != key:
            return False
        if only_unsubmitted and current.get('job_id'):
            return False
        if fields.get('job_id') and current.get('job_id') not in {None, fields['job_id']}:
            raise RemoteWorkerError('同一提交编号返回了不同任务号，已停止覆盖记录。')
        if current.get('job_id') and current.get('status') in REMOTE_TERMINAL and fields.get('status') not in REMOTE_TERMINAL:
            fields.pop('status', None)
        published = fields.pop('published', None)
        current.update(fields)
        if published:
            current.setdefault('published', {}).update(published)
        save_reference(path, current)
        return True


def choose_reference(previous, identity, fingerprint, action):
    if previous and previous.get('identity') != identity:
        raise RemoteWorkerError('保存的任务不属于当前用户、流程或服务地址。')
    if action in {'查询', '取消'}:
        if not previous:
            raise RemoteWorkerError('当前节点尚未保存远端任务。')
        return previous
    if previous and previous.get('status') not in REMOTE_TERMINAL:
        if previous.get('fingerprint') != fingerprint:
            raise RemoteWorkerError('上一任务尚未结束，请使用原资料再次运行以续查，或在高级设置取消任务后再提交不同资料。')
        return previous
    if previous and previous.get('fingerprint') == fingerprint and action != '重新试跑':
        return previous
    return {'identity': identity, 'fingerprint': fingerprint, 'request_key': str(uuid.uuid4()),
            'job_id': None, 'status': 'submitting', 'submission_attempted': False, 'published': {},
            'history': (previous.get('history', []) + [{k: previous.get(k) for k in ['job_id', 'request_key', 'fingerprint', 'status']}]) if previous else []}


class RemoteClient:
    def __init__(self, base_url, token='', *, allow_private_http=False, allow_insecure_http=False, transport=None):
        self.base = worker_url(base_url, allow_private_http, allow_insecure_http)
        self.token = token
        self.headers = {'Authorization': 'Bearer ' + token} if token else {}
        self.http = httpx.AsyncClient(transport=transport, trust_env=False, follow_redirects=False,
            timeout=httpx.Timeout(30, connect=10, write=120))

    async def close(self):
        await self.http.aclose()

    def error(self, response):
        try:
            payload = response.json()
            error = payload.get('error', {}) if isinstance(payload, dict) else {}
            detail = error.get('message', '') if isinstance(error, dict) else str(error)
            if not detail and isinstance(payload, dict):
                detail = payload.get('message', '')
            if (response.status_code == 422 and isinstance(error, dict) and error.get('code') == 'INVALID_OPTIONS'
                    and detail == '模型参数无效或包含不允许的字段。'):
                detail = '远端仍使用旧版 Worker，请升级至 0.2.0 或更新版本。新版节点不发送模型 ID，直接使用远端 Codex 配置。'
        except (ValueError, TypeError):
            detail = ''
        if response.status_code in {401, 403}:
            detail = '服务访问令牌无效或权限不足；这里需要 Worker 令牌，不是 OpenRouter API Key。'
        detail = remote_text(detail, self.token)[:400]
        rejected = response.status_code in {400, 401, 403, 404, 405, 413, 415, 422, 429}
        return RemoteWorkerError(f'远程服务 HTTP {response.status_code}：{detail or "请求未完成"}',
                                 status=response.status_code, rejected=rejected)

    async def request(self, method, suffix, **kwargs):
        kwargs['headers'] = {**kwargs.pop('headers', {}), **self.headers}
        response = await self.http.request(method, self.base + suffix, **kwargs)
        if not 200 <= response.status_code < 300:
            raise self.error(response)
        try:
            value = response.json()
        except ValueError:
            raise RemoteWorkerError('远端响应不是有效 JSON。') from None
        if not isinstance(value, dict):
            raise RemoteWorkerError('远端响应结构不正确。')
        return value

    async def probe(self):
        # A public health check must not send a token or upload any task material.
        response = await self.http.get(self.base + '/healthz', timeout=10)
        if response.status_code != 200:
            raise self.error(response)
        try:
            payload = response.json()
        except ValueError:
            raise RemoteWorkerError('健康接口没有返回 JSON，请核对服务地址和代理路径。') from None
        if not isinstance(payload, dict) or payload.get('status') != 'alive':
            raise RemoteWorkerError('健康接口响应不符合 Worker 约定，请核对服务地址。')
        result = {'reachable': True, 'authenticated': None, 'ready': None, 'service_url': self.base,
                  'model_called': False, 'http_plaintext': urlsplit(self.base).scheme == 'http'}
        if not self.token:
            result['message'] = '网络已连通。请填写服务访问令牌，再检查鉴权和执行环境。'
            return result
        response = await self.http.get(self.base + '/v1/ready', headers=self.headers, timeout=15)
        if response.status_code not in {200, 503}:
            raise self.error(response)
        try:
            payload = response.json()
        except ValueError:
            raise RemoteWorkerError('就绪接口没有返回 JSON。') from None
        if not isinstance(payload, dict) or type(payload.get('ready')) is not bool:
            raise RemoteWorkerError('就绪响应结构不正确，不能判断服务是否可接收任务。')
        result.update(authenticated=True, ready=payload['ready'], codex_version=payload.get('codex_version'),
                      code=payload.get('code'), message=remote_text(payload.get('message', ''), self.token))
        if response.status_code == 503:
            result['ready'] = False
        return result

    async def submit(self, files, options, key):
        # A lost response can be retried because the caller persisted this exact key first.
        for attempt in range(2):
            try:
                with ExitStack() as stack:
                    uploads = {role: (Path(path).name, stack.enter_context(Path(path).open('rb')))
                               for role, path in files.items()}
                    response = await self.request('POST', '/v1/jobs', files=uploads,
                        data={'options': json.dumps(options)}, headers={'Idempotency-Key': key})
                    remote_id(response.get('job_id'), 'job_')
                    if response.get('status') not in REMOTE_TERMINAL | {'queued', 'running', 'checking', 'cancelling'}:
                        raise RemoteWorkerError('提交响应的任务状态无效；请保留相同资料并选择“提交或继续”找回任务。')
                    return response
            except RemoteWorkerError as exc:
                if attempt:
                    exc.rejected = False  # An earlier lost response may already have created the task.
                raise
            except (httpx.TransportError, httpx.TimeoutException):
                if attempt:
                    raise RemoteWorkerError('提交响应未收到，任务可能已被接受；保留资料并选择“提交或继续”。') from None
                await asyncio.sleep(.2)

    async def status(self, job_id):
        remote_id(job_id, 'job_')
        value = await self.request('GET', f'/v1/jobs/{job_id}')
        if value.get('job_id') != job_id or value.get('status') not in REMOTE_TERMINAL | {'queued', 'running', 'checking', 'cancelling'}:
            raise RemoteWorkerError('远端任务状态或编号不一致。')
        return value

    async def cancel(self, job_id):
        remote_id(job_id, 'job_')
        return await self.request('POST', f'/v1/jobs/{job_id}/cancel')

    async def download(self, job_id, entry, directory):
        remote_id(job_id, 'job_')
        artifact_id = remote_id(entry.get('id'), 'artifact_')
        expected_path = f'/v1/jobs/{job_id}/artifacts/{artifact_id}'
        if entry.get('download_path') != expected_path:
            raise RemoteWorkerError('远端产物路径不属于当前任务。')
        name = entry.get('filename', '')
        if (not isinstance(name, str) or not name or name in {'.', '..'} or len(name) > 200
                or re.search(r'[<>:"/\\|?*\x00-\x1f]', name) or name[-1] in {' ', '.'}):
            raise RemoteWorkerError('远端产物文件名不安全。')
        size = entry.get('bytes')
        sha = entry.get('sha256', '')
        if (type(size) is not int or not 0 <= size <= 64 * 1024**2
                or not isinstance(sha, str) or not re.fullmatch('[a-f0-9]{64}', sha)):
            raise RemoteWorkerError('远端产物大小或哈希无效。')
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / (artifact_id + '_' + name)
        if target.is_file() and not target.is_symlink() and target.stat().st_size == size and local_hash(target) == sha:
            return target
        temp = target.with_name(target.name + '.' + uuid.uuid4().hex + '.partial')
        received, checksum = 0, hashlib.sha256()
        try:
            async with self.http.stream('GET', self.base + expected_path, headers=self.headers) as response:
                if response.status_code != 200:
                    await response.aread()
                    raise self.error(response)
                with temp.open('xb') as stream:
                    async for chunk in response.aiter_bytes():
                        received += len(chunk)
                        if received > size:
                            raise RemoteWorkerError('下载文件超过声明大小。')
                        checksum.update(chunk)
                        stream.write(chunk)
                    stream.flush()
                    os.fsync(stream.fileno())
            if received != size or checksum.hexdigest() != sha:
                raise RemoteWorkerError('下载文件校验失败；可继续查询并重新下载，不需要重新翻译。')
            os.replace(temp, target)
            return target
        finally:
            temp.unlink(missing_ok=True)


from lfx.custom import Component
from lfx.io import HandleInput, StrInput, SecretStrInput, DropdownInput, IntInput, BoolInput, Output
from lfx.schema import Message
from lfx.services.deps import get_storage_service, get_settings_service, session_scope
from lfx.utils.file_path_security import component_file_access_scopes, enforce_local_file_access


class RemoteCodexTranslation(Component):
    display_name = '02 远程 Codex · 自主翻译'
    description = '上传资料后直接运行：自动提交、续查或取回已有结果。重新翻译、取消和连接诊断在高级设置中；使用远端 Codex 现有配置。'
    name = 'RemoteCodexTranslation'
    icon = 'Server'
    inputs = [
        HandleInput(name='bundle', display_name='原始任务文件', input_types=['Data'], required=False),
        StrInput(name='service_url', display_name='Windows 服务地址', value='', required=True),
        SecretStrInput(name='service_token', display_name='服务访问令牌', required=False,
            info='填写 Windows Worker 的访问令牌，不是 OpenRouter Key；留空仅能检查网络。'),
        DropdownInput(name='action', display_name='高级操作', options=['自动运行', '重新翻译', '取消任务', '检查连接'],
            value='自动运行', advanced=True,
            info='通常保持自动运行。重新翻译会新建任务并再次调用 Codex；操作选项会保留，续查前请切回自动运行。'),
        IntInput(name='timeout_seconds', display_name='远端执行时限（秒）', value=1800, advanced=True),
        IntInput(name='wait_seconds', display_name='本次等待（秒）', value=900, advanced=True,
            info='等待结束会保留远端任务；再次运行即可继续查看和取回结果。'),
        StrInput(name='langflow_public_url', display_name='Langflow 对外地址（可选）', value='', advanced=True,
            info='留空使用同源下载路径；反向代理有路径前缀时请填写完整基址。'),
        BoolInput(name='allow_private_http', display_name='允许受保护私网 HTTP', value=False, advanced=True),
        BoolInput(name='allow_insecure_http', display_name='临时允许明文 HTTP', value=False, advanced=True,
            info='仅临时联调。开启后公网 HTTP 会明文传送服务令牌和文件；正式使用请改为 HTTPS 并关闭。'),
    ]
    outputs = [Output(name='result', display_name='译文、检查与 Trace', method='run_remote')]

    async def publish_file(self, path, filename):
        from fastapi import UploadFile
        from langflow.api.v2.files import upload_user_file
        from langflow.services.database.models.user.crud import get_user_by_id
        async with session_scope() as session:
            user = await get_user_by_id(session, self.user_id)
            with path.open('rb') as stream:
                result = await upload_user_file(file=UploadFile(filename=filename, file=stream, size=path.stat().st_size),
                    session=session, current_user=user, storage_service=get_storage_service(),
                    settings_service=get_settings_service(), append=False)
            return {'id': str(result.id), 'path': result.path}

    async def run_remote(self) -> Message:
        token = self.service_token.get_secret_value() if hasattr(self.service_token, 'get_secret_value') else str(self.service_token or '')
        client, reference, state_path, submit_lock = None, None, None, None
        action = {'自动运行': '提交或继续', '重新翻译': '重新试跑', '取消任务': '取消'}.get(self.action, self.action)
        try:
            client = RemoteClient(str(self.service_url), token, allow_private_http=bool(self.allow_private_http),
                                  allow_insecure_http=bool(self.allow_insecure_http))
            if action == '检查连接':
                check = await client.probe()
                lines = ['网络：已连通。']
                if check['authenticated']:
                    lines += ['服务令牌：鉴权通过。', '执行环境：' + ('已就绪。' if check['ready'] else '未就绪。')]
                    if check.get('codex_version'):
                        lines.append('Codex 版本：' + remote_text(check['codex_version'], token))
                else:
                    lines.append('鉴权和执行环境：未检查，请填写服务访问令牌后再检查。')
                if check['authenticated'] and check.get('message'):
                    lines.append(check['message'])
                if check['http_plaintext']:
                    lines.append('当前为 HTTP 明文连接；令牌和资料会明文传输。')
                lines.append('本次未上传资料、未创建翻译任务，也未调用模型。')
                self.status = '连接检查完成' if check['ready'] is not False else '网络已连通，执行环境未就绪'
                return Message(text='\n\n'.join(lines))
            query_only = action in {'查询', '取消'}
            if action not in {'提交或继续', '重新试跑', '查询', '取消'}:
                raise RemoteWorkerError('请选择有效的本次操作。')
            if not token:
                raise RemoteWorkerError('请先填写服务访问令牌，可使用“检查连接”确认鉴权。')
            if not self.user_id or not self.graph.flow_id:
                raise RemoteWorkerError('请在已保存的 Langflow 流程中运行此节点。')
            if not 60 <= int(self.timeout_seconds) <= 7200 or not 0 <= int(self.wait_seconds) <= 7200:
                raise RemoteWorkerError('执行时限须为 60–7200 秒，本次等待须为 0–7200 秒。')
            origin = str(self.langflow_public_url or '').rstrip('/')
            if origin:
                parsed = urlsplit(origin)
                if parsed.scheme not in {'https', 'http'} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
                    raise RemoteWorkerError('Langflow 对外地址必须为无凭据的 HTTP(S) 基址。')
            scope = component_file_access_scopes(self)
            files = {}
            if not query_only:
                bundle = getattr(getattr(self, 'bundle', None), 'data', {})
                if bundle and (bundle.get('user_id') != str(self.user_id) or bundle.get('flow_id') != str(self.graph.flow_id)):
                    raise RemoteWorkerError('资料必须来自当前用户和当前流程的第一个节点。')
                files = {role: enforce_local_file_access(path, scope_ids=scope) for role, path in bundle.get('files', {}).items()}
                if files:
                    validate_upload_files(files)
            options = {'timeout_seconds': int(self.timeout_seconds)}
            identity = {'user_id': str(self.user_id), 'flow_id': str(self.graph.flow_id), 'node_id': str(self._id), 'service': client.base}
            identity_hash = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
            root = enforce_local_file_access(get_storage_service().build_full_path(str(self.user_id), 'codex_remote/' + identity_hash), scope_ids=scope)
            root.mkdir(parents=True, exist_ok=True)
            state_path = root / 'reference.json'
            previous = read_reference(state_path)
            if previous and previous.get('identity') != identity:
                raise RemoteWorkerError('保存的任务不属于当前用户、流程或服务地址。')
            if not files and not query_only and (not previous or action == '重新试跑'):
                raise RemoteWorkerError('首次运行或重新翻译需要四份有效资料，请在第一个节点上传。')
            fingerprint = previous.get('fingerprint', '') if previous else ''
            if files:
                fingerprint = hashlib.sha256(json.dumps({'options': options,
                    'files': {role: {'name': Path(path).name, 'sha256': await asyncio.to_thread(local_hash, path)} for role, path in files.items()}},
                    sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            cached_job = None
            if previous and previous.get('job_id') and action != '取消':
                cached_job = await client.status(previous['job_id'])
                if not update_reference(state_path, identity, previous['request_key'], status=cached_job['status']):
                    raise RemoteWorkerError('另一请求已更新当前任务，请再次运行以读取最新记录。')
            reference = reserve_reference(state_path, identity, fingerprint, action)
            submitted_now = False
            if not reference.get('job_id'):
                if query_only or not files:
                    if reference.get('status') == 'failed':
                        return Message(text='上次提交尚未创建远端任务。修正令牌、资料或服务配置，提供四份资料后再次运行。')
                    return Message(text='上次提交尚未收到任务编号。请恢复相同的四份资料和参数，再次运行可按原提交编号找回任务；当前不能确认已取消。')
                submit_lock = ReferenceLock(root / 'submit.lock')
                current = read_reference(state_path)
                if current.get('request_key') != reference['request_key']:
                    raise RemoteWorkerError('当前提交记录已变化，请再次运行。')
                attempted_before = current.get('submission_attempted', True)
                # Missing markers on old references are treated as uncertain submissions.
                if not attempted_before:
                    self.status = '正在检查连接与执行环境'
                    try:
                        check = await client.probe()
                        if check['ready'] is not True:
                            raise RemoteWorkerError('执行环境尚未就绪：' + (check.get('message') or '请检查 Windows Worker 自检结果。'))
                    except (RemoteWorkerError, httpx.TransportError, asyncio.CancelledError):
                        update_reference(state_path, identity, reference['request_key'], status='failed', only_unsubmitted=True)
                        raise
                self.status = '正在提交原始资料'
                if not update_reference(state_path, identity, reference['request_key'], submission_attempted=True):
                    raise RemoteWorkerError('当前提交记录已变化，请再次运行。')
                try:
                    submitted = await client.submit(files, options, reference['request_key'])
                except RemoteWorkerError as exc:
                    if exc.rejected and not attempted_before:
                        update_reference(state_path, identity, reference['request_key'], status='failed', submission_attempted=False, only_unsubmitted=True)
                    elif exc.rejected:
                        exc.rejected = False
                    raise
                submitted_now = not submitted.get('reused', False)
                reference.update(job_id=submitted['job_id'], status=submitted['status'])
                update_reference(state_path, identity, reference['request_key'], job_id=submitted['job_id'], status=submitted['status'])
                submit_lock.close()
                submit_lock = None
            job_id = reference['job_id']
            if action == '取消':
                await client.cancel(job_id)
                cached_job = None
            deadline = asyncio.get_running_loop().time() + (0 if query_only else int(self.wait_seconds))
            while True:
                job = cached_job if cached_job and cached_job['job_id'] == job_id else await client.status(job_id)
                cached_job = None
                update_reference(state_path, identity, reference['request_key'], status=job['status'])
                self.status = f"{job_id}：{job['status']}"
                if job['status'] in REMOTE_TERMINAL or asyncio.get_running_loop().time() >= deadline:
                    break
                await asyncio.sleep(3)
            labels = {'completed': '文件已生成，程序检查通过', 'needs_review': '文件已生成，需复核',
                      'failed': '任务失败', 'cancelled': '已取消', 'interrupted': '任务中断',
                      'queued': '排队中', 'running': '执行中', 'checking': '检查中', 'cancelling': '取消处理中，尚未确认停止'}
            links, attachments = [], []
            for entry in job.get('artifacts', []):
                path = await client.download(job_id, entry, root / job_id)
                saved = reference.setdefault('published', {}).get(entry['id'])
                if saved:
                    existing = enforce_local_file_access(self.get_full_path(saved['path']), scope_ids=scope)
                    if not existing.is_file() or await asyncio.to_thread(local_hash, existing) != entry['sha256']:
                        saved = None
                if saved is None:
                    saved = await self.publish_file(path, entry['filename'])
                    reference['published'][entry['id']] = saved
                    update_reference(state_path, identity, reference['request_key'], published={entry['id']: saved})
                url = origin + '/api/v2/files/' + saved['id']
                label = entry['filename'].replace('[', '\\[').replace(']', '\\]')
                links.append(f"[{label}]({url})")
                attachments.append(str(self.get_full_path(saved['path'])))
            error = job.get('error') or {}
            message = f"{labels[job['status']]}。任务编号：`{job_id}`。"
            if not submitted_now and action != '取消':
                if job['status'] in {'completed', 'needs_review'}:
                    message += '\n\n已取回已有任务的结果，本次未重新翻译。'
                elif job['status'] in REMOTE_TERMINAL:
                    message += '\n\n这是已有任务的状态，本次未重新翻译。确认需要重跑时，在高级设置选择“重新翻译”。'
                else:
                    message += '\n\n正在继续已有任务，本次未重复提交。'
            if action == '重新试跑':
                message += '\n\n续查请将高级操作切回“自动运行”；保留“重新翻译”会在任务结束后再次新建任务。'
            if error.get('message'):
                message += '\n\n' + remote_text(error['message'], token)
            if job['status'] not in REMOTE_TERMINAL:
                message += '\n\n任务引用已保存。在自动运行模式下再次点击运行即可续查；本次等待结束不会自动取消远端任务。'
            if links:
                message += '\n\n' + '\n\n'.join(links)
            if job['status'] in {'completed', 'needs_review'}:
                message += '\n\n程序检查不代表语言质量已通过独立审校。'
            return Message(text=message, files=attachments)
        except asyncio.CancelledError:
            if reference and reference.get('job_id'):
                async def request_cancel():
                    try:
                        await client.cancel(reference['job_id'])
                        job = await client.status(reference['job_id'])
                        update_reference(state_path, identity, reference['request_key'], status=job['status'])
                    except Exception:
                        update_reference(state_path, identity, reference['request_key'], cancel_unconfirmed=True)
                try:
                    await asyncio.shield(asyncio.wait_for(request_cancel(), 10))
                except (Exception, asyncio.CancelledError):
                    pass
            raise
        except (RemoteWorkerError, httpx.TransportError) as exc:
            message = remote_text(str(exc), token)
            self.status = '连接或操作未完成'
            if reference:
                job_id = reference.get('job_id')
                if not job_id and getattr(exc, 'rejected', False):
                    message += '\n\n提交被拒绝，未取得任务号。修正后再次运行即可重试。'
                else:
                    message += f"\n\n任务编号：`{job_id or '尚未取得'}`。任务引用已保留，请保持资料和参数一致，在自动运行模式下再次运行。"
            return Message(text=message)
        finally:
            if submit_lock:
                submit_lock.close()
            if client:
                await client.close()
