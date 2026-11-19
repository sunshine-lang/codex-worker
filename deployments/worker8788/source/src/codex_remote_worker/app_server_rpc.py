"""Private stdio App Server transport; never exposed as a public RPC proxy."""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import uuid

from .common import JobError, file_digest
from .windows import WindowsProcess

# A tool result can contain a whole project (outbound RPC accepts up to 8 MiB).
# Echoed item/completed events need room for the result plus event metadata.
MAX_EVENT_BYTES = 16 * 1024 * 1024


class AppServerRpcError(JobError):
    def __init__(self, code, detail):
        super().__init__('APP_SERVER_RPC_ERROR', 'Codex App Server 拒绝了执行请求；请查看脱敏日志。')
        self.rpc_code, self.detail = code, str(detail)


class AppServerRpc:
    def __init__(self, settings, root, redactor, *, process_factory=WindowsProcess):
        self.settings, self.root, self.redactor = settings, Path(root), redactor
        self.process_factory = process_factory
        self.process = None
        self.pool = None
        self.tasks = []
        self.pending = {}
        self.queues = {}
        self.tool_threads = set()
        self.sequence = 0
        self.connected = False
        self.failure = None
        self.write_lock = asyncio.Lock()
        self.transport_bytes = 0
        self.server_info = {}
        self.on_event = None
        self.runtime_processes = None
        self.trusted_tool_images = {}

    async def _blocking(self, function, *args):
        return await asyncio.get_running_loop().run_in_executor(self.pool, function, *args)

    async def start(self):
        from .runner import runtime_environment
        import os
        # Capture immutable expected images BEFORE any model turn can run.
        images=[self.settings.codex_executable.parent/'codex-code-mode-host.exe',
                Path(os.environ.get('SystemRoot',r'C:\Windows'))/'System32/conhost.exe']
        self.trusted_tool_images={str(p.resolve()).casefold():file_digest(p) for p in images if p.is_file()}
        (self.root / 'work').mkdir(parents=True, exist_ok=True)
        (self.root / 'logs').mkdir(parents=True, exist_ok=True)
        command = [str(self.settings.codex_executable), '-c', 'features.shell_snapshot=false',
                   '-c', 'windows.sandbox=' + json.dumps(self.settings.windows_sandbox),
                   '-c', 'approval_policy="never"', '-c', 'thread_unload_delay_secs=0',
                   'app-server', '--listen', 'stdio://']
        self.pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix='app-server-pipes')
        try:
            self.process = self.process_factory(command, self.root / 'work',
                {**runtime_environment(self.settings, self.root), 'RUST_LOG': 'codex_app_server=info,codex_app_server::outgoing_message=warn,codex_core=warn,codex_app_server_transport::transport::remote_control=error'},
                f'Local\\CodexWorkerAppServer{self.settings.port}-' + uuid.uuid4().hex)
            if self.root.name.startswith('appserver-protocol-'):
                import os
                from .common import write_json
                import ctypes
                from ctypes import wintypes
                kernel = ctypes.WinDLL('kernel32', use_last_error=True)
                length = wintypes.UINT(0)
                kernel.GetPackageFamilyName.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.UINT), wintypes.LPWSTR]
                kernel.GetPackageFamilyName.restype = wintypes.LONG
                identity_code = kernel.GetPackageFamilyName(int(self.process.handle), ctypes.byref(length), None)
                child_family = None
                if identity_code == 122:
                    name = ctypes.create_unicode_buffer(length.value)
                    if kernel.GetPackageFamilyName(int(self.process.handle), ctypes.byref(length), name) == 0:
                        child_family = name.value
                write_json(self.root / 'logs/core-routing.json', {
                    'child_package_family': child_family, 'package_query_code': identity_code,
                    'registered_core': os.environ.get('CODEX_WINDOWS_REGISTERED_CORE') == '1',
                    'package_family_hint': os.environ.get('CODEX_WINDOWS_SANDBOX_PACKAGE_FAMILY')})
            self.connected = True
            self.tasks = [asyncio.create_task(self._read_stdout()), asyncio.create_task(self._read_stderr())]
            self.server_info = await self.request('initialize', {
                'clientInfo': {'name': f'codex_worker_appserver_{self.settings.port}', 'title': f'Worker App Server {self.settings.port}', 'version': '0.4.0-test.1'},
                'capabilities': {'experimentalApi': True}}, timeout=75)
            await self.notify('initialized', {})
            # No thread/model task exists yet. Retain the exact trusted runtime
            # identities: packaged Codex may own both a launcher and Core process.
            self.runtime_processes = self.process.active_processes()
            if not self.runtime_processes:
                raise JobError('PROCESS_INSPECTION_FAILED', 'App Server 进程基线为空。')
            return self.server_info
        except BaseException:
            await self.close()
            raise

    async def _write(self, message):
        if not self.connected:
            raise JobError('APP_SERVER_DISCONNECTED', 'App Server 连接不可用，未自动重跑任务。', 503)
        data = json.dumps(message, ensure_ascii=False, separators=(',', ':')).encode('utf-8') + b'\n'
        if len(data) > 8 * 1024 * 1024:
            raise JobError('LIMIT_EXCEEDED', 'App Server 输入消息超过限制。')
        def send():
            remaining = memoryview(data)
            while remaining:
                n = self.process.stdin.write(remaining)
                if not n:
                    raise OSError('closed RPC stdin')
                remaining = remaining[n:]
        async with self.write_lock:
            try:
                await self._blocking(send)
            except OSError:
                self._fail('App Server stdin closed')
                raise JobError('APP_SERVER_DISCONNECTED', 'App Server 输入连接断开，任务未自动重跑。') from None

    async def request(self, method, params, *, timeout=30):
        self.sequence += 1
        request_id = self.sequence
        future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        try:
            await self._write({'id': request_id, 'method': method, 'params': params})
            try:
                return await asyncio.wait_for(future, timeout)
            except TimeoutError:
                from .common import write_json
                write_json(self.root / 'logs/rpc-last-timeout.json', {
                    'method': method, 'transport_bytes': self.transport_bytes,
                    'connected': self.connected, 'process_exit_code': self.process.poll(),
                    'active_processes': self.process.active_count(),
                    'failure': self.failure})
                raise JobError('APP_SERVER_RPC_TIMEOUT', 'App Server 请求超时，未自动重发执行请求。') from None
        finally:
            self.pending.pop(request_id, None)
            if not future.done():
                future.cancel()

    async def notify(self, method, params):
        await self._write({'method': method, 'params': params})

    def subscribe(self, thread_id):
        if thread_id in self.queues:
            raise JobError('INVALID_TRACE', 'App Server 会话被重复分配。')
        queue = asyncio.Queue(maxsize=1024)
        self.queues[thread_id] = queue
        return queue

    def unsubscribe_local(self, thread_id):
        self.queues.pop(thread_id, None)
        self.tool_threads.discard(thread_id)

    def runtime_processes_unchanged(self):
        try:
            return bool(self.runtime_processes) and self.process.active_processes() == self.runtime_processes
        except Exception:
            return False

    def adopt_project_tool_runtime(self):
        """Adopt exactly one official lazy tool host + its console, never arbitrary children."""
        try:
            before=set(self.runtime_processes or ())
            current=self.process.active_processes()
            added=set(current)-before
            if not before or not before.issubset(current) or len(added)!=2:
                return False
            details=self.process.process_details(added)
            host=next((pid for pid,d in details.items() if Path(d['image']).name=='codex-code-mode-host.exe'),None)
            console=next((pid for pid,d in details.items() if Path(d['image']).name=='conhost.exe'),None)
            if host is None or console is None or details[host]['parent']!=self.process.metadata['pid'] or details[console]['parent']!=host:
                return False
            for d in details.values():
                path=Path(d['image']).resolve()
                expected=self.trusted_tool_images.get(str(path).casefold())
                if not expected or file_digest(path)!=expected:return False
            if self.process.active_processes()!=current:return False
            self.runtime_processes=current
            return True
        except Exception:
            return False

    def _fail(self, detail):
        self.connected = False
        self.failure = self.redactor.text(detail)[:2000]
        for future in list(self.pending.values()):
            if not future.done():
                future.set_exception(JobError('APP_SERVER_DISCONNECTED', 'App Server 连接中断，任务未自动重跑。'))
        for queue in self.queues.values():
            try:
                queue.put_nowait({'method': 'transport/disconnected', 'params': {}})
            except asyncio.QueueFull:
                pass

    async def _dispatch(self, message):
        if self.root.name.startswith('appserver-protocol-') and isinstance(message, dict):
            with (self.root / 'logs/rpc-methods.jsonl').open('a', encoding='utf-8') as stream:
                stream.write(json.dumps({'id': message.get('id'), 'method': message.get('method'),
                    'has_error': 'error' in message, 'result_keys': list(message.get('result', {})) if isinstance(message.get('result'), dict) else []}) + '\n')
        if not isinstance(message, dict):
            raise ValueError('RPC message must be an object')
        if 'method' not in message and 'id' in message:
            future = self.pending.get(message['id'])
            if future and not future.done():
                if 'error' in message:
                    error = message['error']
                    future.set_exception(AppServerRpcError(error.get('code'), self.redactor.text(error.get('message', ''))))
                elif 'result' in message:
                    future.set_result(message['result'])
                else:
                    raise ValueError('RPC response missing result')
            return
        method = message.get('method')
        params = message.get('params') or {}
        if not isinstance(method, str) or not isinstance(params, dict):
            raise ValueError('Invalid RPC event')
        if 'id' in message:
            if method == 'item/tool/call' and params.get('threadId') in self.tool_threads:
                queue = self.queues.get(params.get('threadId'))
                if queue is not None:
                    queue.put_nowait(message)
                    return
            if method in ('item/commandExecution/requestApproval', 'item/fileChange/requestApproval'):
                await self._write({'id': message['id'], 'result': {'decision': 'decline'}})
            else:
                await self._write({'id': message['id'], 'error': {'code': -32601,
                    'message': 'Interactive tools, permission escalation and external token refresh are unavailable in this Worker.'}})
            # These are never forwarded to upstream users and never auto-approved.
            queue = self.queues.get(params.get('threadId'))
            if queue:
                queue.put_nowait({'method': 'worker/interactiveRequestDenied', 'params': {'threadId': params.get('threadId')}})
            return
        queue = self.queues.get(params.get('threadId'))
        if queue:
            queue.put_nowait(message)
        if self.on_event:
            self.on_event(message)

    async def _read_stdout(self):
        try:
            while True:
                line = await self._blocking(self.process.stdout.readline, MAX_EVENT_BYTES + 1)
                if not line:
                    self._fail('App Server stdout closed')
                    return
                if len(line) > MAX_EVENT_BYTES:
                    raise ValueError('App Server event exceeded transport size limit')
                self.transport_bytes += len(line)
                await self._dispatch(json.loads(line))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._fail(type(exc).__name__)

    async def _read_stderr(self):
        total = 0
        try:
            with (self.root / 'logs/app-server.stderr.log').open('ab') as output:
                while True:
                    line = await self._blocking(self.process.stderr.readline, 64 * 1024 + 1)
                    if not line:
                        return
                    total += len(line)
                    if total > self.settings.max_log_bytes or len(line) > 64 * 1024:
                        self._fail('App Server stderr limit exceeded')
                        return
                    output.write(self.redactor.text(line.decode('utf-8', errors='replace')).encode('utf-8'))
                    output.flush()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._fail(type(exc).__name__)

    async def close(self):
        self.connected = False
        self._fail('App Server closed')
        process, self.process = self.process, None
        try:
            if process is not None:
                await asyncio.to_thread(process.terminate_tree)
        finally:
            for task in self.tasks:
                task.cancel()
            if self.tasks:
                await asyncio.gather(*self.tasks, return_exceptions=True)
            self.tasks.clear()
            if process is not None:
                process.close()
            if self.pool is not None:
                self.pool.shutdown(wait=True, cancel_futures=True)
                self.pool = None
