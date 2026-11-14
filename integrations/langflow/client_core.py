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
