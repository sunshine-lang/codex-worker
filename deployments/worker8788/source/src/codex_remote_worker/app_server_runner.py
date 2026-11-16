"""App Server adapter for the unchanged Worker job/task/delivery contracts."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import time
import tomllib

from .app_server_rpc import AppServerRpc, AppServerRpcError
from .common import JobError, Redactor, atomic_write, digest, now, write_json
from .config import read_codex_config
from .runner import CodexRunner, config_arguments, codex_config_digest
from .subscription import effective_config, require_chatgpt_login
from .task_runtime import prepare_task_runtime


def thread_parameters(settings, root):
    arguments = config_arguments(settings, root)
    overrides = {}
    for flag, item in zip(arguments[::2], arguments[1::2]):
        assert flag == '-c'
        key, value = item.split('=', 1)
        overrides[key] = tomllib.loads('value=' + value)['value']
    return {'cwd': str(root / 'work'), 'approvalPolicy': 'never', 'permissions': 'translation',
            'config': overrides, 'ephemeral': False, 'experimentalRawEvents': False,
            'allowProviderModelFallback': False}


def verify_thread(response, configured, root):
    profile = response.get('activePermissionProfile') or {}
    if profile.get('id') != 'translation' or response.get('approvalPolicy') != 'never':
        raise JobError('APP_SERVER_POLICY_MISMATCH', 'App Server 未确认原有 translation 权限配置，未启动模型。')
    if Path(response.get('cwd', '')).resolve() != (root / 'work').resolve():
        raise JobError('APP_SERVER_POLICY_MISMATCH', 'App Server 工作目录不匹配，未启动模型。')
    for config_key, response_key in (('model', 'model'), ('model_provider', 'modelProvider'), ('model_reasoning_effort', 'reasoningEffort')):
        expected = configured.get(config_key)
        if expected is not None and response.get(response_key) != expected:
            raise JobError('APP_SERVER_MODEL_MISMATCH', 'App Server 的模型、Provider 或推理设置与基线不同，未启动模型。')
    thread_id = (response.get('thread') or {}).get('id')
    if not isinstance(thread_id, str) or not thread_id:
        raise JobError('INVALID_TRACE', 'App Server 未返回有效会话编号。')
    return thread_id


class AppServerRunner(CodexRunner):
    def __init__(self, settings, *, rpc_factory=AppServerRpc):
        super().__init__(settings)
        self.rpc_factory = rpc_factory
        self.rpc = None
        self.generation = 0
        self.started = False
        self.restart_pending = False
        self.serial = asyncio.Lock()
        self.server_root = settings.data_root / 'app-server'
        self.process_record = settings.data_root / 'app-server-process.json'
        self.redactor = Redactor(settings.secret_values(), [settings.data_root, settings.codex_home])

    def baseline_ready(self):
        return super().ready()

    async def prepare(self, job, root, touch, stopped, redactor):
        return await prepare_task_runtime(self.settings, job, root, touch, stopped, redactor)

    def thread_parameters(self, root):
        return thread_parameters(self.settings, root)

    def turn_parameters(self, thread_id, prompt, root):
        return {'threadId': thread_id, 'input': [{'type': 'text', 'text': prompt.decode('utf-8'), 'text_elements': []}],
                'permissions': 'translation', 'approvalPolicy': 'never'}

    def check_item(self, item):
        pass

    def delivered(self, root, payload):
        pass

    def next_turn(self, thread_id, root, payload):
        """Optional deliberate continuation; never retries a failed/ambiguous turn."""
        return None

    async def _connect(self):
        rpc = self.rpc_factory(self.settings, self.server_root, self.redactor)
        await rpc.start()
        self.rpc = rpc
        self.generation += 1
        self.restart_pending = False
        write_json(self.process_record, {
            'process': rpc.process.metadata, 'generation': self.generation, 'started_at': now(),
            'runtime_processes': getattr(rpc, 'runtime_processes', None),
            'transport': 'private-stdio', 'model_called_during_startup': False})

    async def start(self):
        baseline = self.baseline_ready()
        if not baseline['ready']:
            raise JobError(baseline['code'], baseline['message'], 503)
        await self._connect()
        self.started = True

    def ready(self):
        value = self.baseline_ready()
        value.update(execution_backend='codex-app-server', app_server_generation=self.generation,
            max_concurrent_jobs=1, task_batch_concurrency=1)
        if value['ready'] and (not self.started or (not self.restart_pending and (not self.rpc or not self.rpc.connected))):
            value.update(ready=False, code='APP_SERVER_UNAVAILABLE', message='App Server 尚未就绪或已断开；未自动重跑任务。')
        return value

    async def close(self):
        self.started = False
        if self.rpc:
            await self.rpc.close()
            self.rpc = None

    async def _unload_thread(self, rpc, thread_id, queue, *, timeout=5):
        if queue is None:
            queue = rpc.subscribe(thread_id)
        try:
            result = await rpc.request('thread/unsubscribe', {'threadId': thread_id}, timeout=timeout)
            if result.get('status') == 'notLoaded':
                return True
            if result.get('status') not in ('unsubscribed', 'notSubscribed'):
                return False
            until = time.monotonic() + timeout
            while rpc.connected and time.monotonic() < until:
                event = await asyncio.wait_for(queue.get(), max(.001, until-time.monotonic()))
                params = event.get('params') or {}
                if params.get('threadId') != thread_id:
                    continue
                if event.get('method') == 'thread/closed':
                    return True
                if event.get('method') == 'thread/status/changed' and (params.get('status') or {}).get('type') == 'notLoaded':
                    return True
        except (JobError, TimeoutError):
            pass
        return False

    async def _finish_thread(self, thread_id, turn_id, terminal, queue):
        cleanup_started = time.monotonic()
        rpc = self.rpc
        if rpc is None:
            return {'daemon_recycled': False}
        confirmed = terminal
        if thread_id and turn_id and not confirmed and rpc.connected:
            try:
                await rpc.request('turn/interrupt', {'threadId': thread_id, 'turnId': turn_id}, timeout=4)
                until = time.monotonic() + 5
                while time.monotonic() < until and not confirmed:
                    event = await asyncio.wait_for(queue.get(), until - time.monotonic())
                    params = event.get('params') or {}
                    turn = params.get('turn') or {}
                    confirmed = event.get('method') == 'turn/completed' and turn.get('id') == turn_id
            except (JobError, TimeoutError):
                pass
        unloaded = not thread_id
        if thread_id and confirmed and rpc.connected:
            unloaded = await self._unload_thread(rpc, thread_id, queue)
        if thread_id:
            rpc.unsubscribe_local(thread_id)
        # Keep the daemon only when the thread was unloaded and no tool child remains.
        # A detached child must never keep editing files during host validation.
        if confirmed and unloaded and rpc.connected:
            context=getattr(self,'project_session',None) or getattr(self,'project_context',None)
            if context is not None and context.calls and not rpc.runtime_processes_unchanged():
                if rpc.adopt_project_tool_runtime():
                    write_json(self.process_record, {'process':rpc.process.metadata,'generation':self.generation,
                        'runtime_processes':rpc.runtime_processes,'trusted_project_tool_host':True,
                        'transport':'private-stdio','updated_at':now()})
            until = time.monotonic() + 3
            while not rpc.runtime_processes_unchanged() and time.monotonic() < until:
                await asyncio.sleep(.05)
            if rpc.runtime_processes_unchanged():
                return {'daemon_recycled': False, 'thread_unloaded': True, 'tool_children_remaining': 0,
                        'runtime_identity_verified': True, 'cleanup_seconds': round(time.monotonic()-cleanup_started, 3)}
        await rpc.close()  # Job Object termination is verified by the existing WindowsProcess.
        self.rpc = None
        self.restart_pending = True
        return {'daemon_recycled': True, 'thread_unloaded': unloaded, 'tool_children_remaining': 0,
                'cleanup_seconds': round(time.monotonic()-cleanup_started, 3)}

    async def run(self, job, root, touch, stopped, redactor):
        async with self.serial:
            ready = self.ready()
            if not ready['ready']:
                raise JobError('NOT_READY', ready.get('message', ready['code']), 503)
            if stopped():
                raise JobError('CANCEL_REQUESTED', '任务在启动前已请求停止。')
            config_sha = codex_config_digest(self.settings)
            if 'codex_config_sha256' in job and job['codex_config_sha256'] != config_sha:
                raise JobError('CODEX_CONFIG_CHANGED', '任务提交后 Codex 配置变化，未调用模型。')
            configured = (effective_config(self.settings) if self.settings.authentication_mode == 'chatgpt'
                          else read_codex_config(self.settings.codex_home))
            prompt = (root / 'input' / job['inputs']['prompt_file']['filename']).read_bytes()
            if digest(prompt) != job['prompt_sha256']:
                raise JobError('INPUT_CHANGED', 'Prompt 原件与提交记录不一致。')
            preparation = await self.prepare(job, root, touch, stopped, redactor)
            if codex_config_digest(self.settings) != config_sha:
                raise JobError('CODEX_CONFIG_CHANGED', '文件准备期间 Codex 配置变化，未调用模型。')
            if self.settings.authentication_mode == 'chatgpt':
                await asyncio.to_thread(require_chatgpt_login, self.settings, refresh=True)
            reused = not self.restart_pending
            if self.restart_pending:
                await self._connect()  # A new daemon, never a replay of an earlier model turn.
            rpc = self.rpc
            execution = {'harness': 'codex app-server', 'service_version': ready['service_version'],
                'codex_version': ready['codex_version'], 'tools': ready['tools'],
                'configuration_source': 'server_codex', 'authentication_mode': self.settings.authentication_mode,
                'codex_config_sha256': config_sha, 'configured_provider': configured.get('model_provider'),
                'configured_model': configured.get('model'), 'provider': None, 'model_id': None,
                'resolved_model_id': None, 'upstream_provider': None, 'model_routing_verified': False,
                'permission_profile': 'workspace-only-server-codex-v3-runtime-tools', 'approval_policy': 'never',
                'started_at': now(), 'prompt_sha256': digest(prompt), 'prompt_bytes': len(prompt),
                'prompt_mode': 'uploaded_verbatim', 'prompt_delivered': False, 'event_count': 0,
                'usage': None, 'thread_id': None, 'turn_id': None, 'trace_complete': True,
                'turn_completed': False, 'turn_failed': False, 'runtime_preparation': preparation,
                'app_server_generation': self.generation, 'daemon_reused': reused,
                'model_output_schema_requested': False, 'exit_code_semantics': 'turn_status_not_daemon_exit'}
            touch(execution=execution)
            thread_id = turn_id = None
            terminal = False
            queue = None
            started = time.monotonic()
            total = 0
            transport_start = rpc.transport_bytes
            final_messages = {}
            last_progress = 0.0
            trace_path = root / 'logs/trace.jsonl'
            error_path = root / 'logs/stderr.log'
            try:
                response = await rpc.request('thread/start', self.thread_parameters(root))
                thread_id = verify_thread(response, configured, root)
                queue = rpc.subscribe(thread_id)
                if getattr(self, 'project_context', None) is not None or getattr(self, 'project_session', None) is not None:
                    rpc.tool_threads.add(thread_id)
                execution.update(thread_id=thread_id, resolved_model_id=response['model'],
                    effective_provider=response['modelProvider'], effective_reasoning_effort=response.get('reasoningEffort'),
                    active_permission_profile=response['activePermissionProfile'])
                touch(process=rpc.process.metadata, execution=execution, stage='agent_running')
                if stopped():
                    raise JobError('CANCEL_REQUESTED', '任务在发送模型请求前已请求停止。')
                if codex_config_digest(self.settings) != config_sha:
                    raise JobError('CODEX_CONFIG_CHANGED', '会话创建期间 Codex 配置变化，未调用模型。')
                params = self.turn_parameters(thread_id, prompt, root)
                if params.get('environments') == []:
                    execution.update(prompt_mode='host_prepared_context',
                        permission_profile='environmentless-text-json',
                        model_output_schema_requested='outputSchema' in params,
                        submitted_context_bytes=len(params['input'][0]['text'].encode('utf-8')),
                        submitted_context_sha256=digest(params['input'][0]['text'].encode('utf-8')))
                result = await rpc.request('turn/start', params)
                turn_id = (result.get('turn') or {}).get('id')
                if not turn_id:
                    raise JobError('INVALID_TRACE', 'App Server 未返回任务轮次编号。')
                execution.update(turn_id=turn_id, turn_ids=[turn_id], prompt_delivered=True,
                    thread_start_and_submit_seconds=round(time.monotonic() - started, 3))
                touch(execution=execution)
                with trace_path.open('wb') as trace, error_path.open('wb') as errors:
                    while True:
                        if stopped():
                            raise JobError('CANCEL_REQUESTED', '任务已请求停止。')
                        if not rpc.connected:
                            raise JobError('APP_SERVER_DISCONNECTED', 'App Server 连接中断，任务未自动重跑。')
                        try:
                            event = await asyncio.wait_for(queue.get(), .15)
                        except TimeoutError:
                            continue
                        method, data = event.get('method'), event.get('params') or {}
                        if method == 'transport/disconnected':
                            raise JobError('APP_SERVER_DISCONNECTED', 'App Server 连接中断，任务未自动重跑。')
                        if data.get('threadId') != thread_id:
                            raise JobError('INVALID_TRACE', '收到不属于当前任务的会话事件。')
                        event_turn = data.get('turnId') or (data.get('turn') or {}).get('id')
                        if event_turn is not None and event_turn != turn_id:
                            raise JobError('INVALID_TRACE', '收到不属于当前任务的轮次事件。')
                        execution['event_count'] += 1
                        if rpc.transport_bytes - transport_start > self.settings.max_log_bytes:
                            execution['trace_complete'] = False
                            raise JobError('LIMIT_EXCEEDED', '执行事件超过日志限制。')
                        if method == 'item/tool/call':
                            handler = getattr(self, 'project_tool_result', None)
                            if handler is None:
                                raise JobError('UNEXPECTED_TOOL_ACTIVITY', '未配置项目查询工具。')
                            handling = asyncio.create_task(asyncio.to_thread(handler, data))
                            try:
                                result = await asyncio.shield(handling)
                            except asyncio.CancelledError:
                                await handling  # Finish the host checkpoint before releasing the lane.
                                raise
                            await rpc._write({'id': event['id'], 'result': result})
                            session = getattr(self, 'project_session', None)
                            if session is not None:
                                execution['project_session'] = session.progress()
                                execution['conversation_mode'] = 'one_project_one_thread'
                            else:
                                execution['project_context_queries'] = len(self.project_context.calls)
                            touch(execution=execution)
                            continue
                        item = data.get('item') or {}
                        self.check_item(item)
                        if method == 'item/completed' and item.get('type') == 'contextCompaction':
                            execution['context_compactions'] = execution.get('context_compactions', 0)+1
                        if method == 'item/completed' and item.get('type') == 'agentMessage':
                            text = item.get('text')
                            if isinstance(text, str):
                                final_messages[item['id']] = (item.get('phase'), text)
                        if method == 'thread/tokenUsage/updated':
                            usage = (data.get('tokenUsage') or {}).get('total') or {}
                            execution['usage'] = {'input_tokens': usage.get('inputTokens'),
                                'cached_input_tokens': usage.get('cachedInputTokens'), 'output_tokens': usage.get('outputTokens')}
                        clean = None
                        if method in ('turn/started', 'turn/completed'):
                            turn = data.get('turn') or {}
                            clean = {'type': method.replace('/', '.'), 'thread_id': thread_id,
                                'turn_id': turn.get('id'), 'status': turn.get('status')}
                        elif method in ('item/started', 'item/completed') and item.get('type') not in ('reasoning', 'userMessage', 'hookPrompt'):
                            clean = redactor.event({'type': method.replace('/', '.'), 'item': item})
                        elif method in ('error', 'worker/interactiveRequestDenied'):
                            clean = redactor.event({'type': method, 'error': data.get('error')})
                        if clean:
                            line = json.dumps(clean, ensure_ascii=False).encode('utf-8') + b'\n'
                            total += len(line)
                            if total > self.settings.max_log_bytes:
                                raise JobError('LIMIT_EXCEEDED', '执行日志超过任务限制。')
                            trace.write(line); trace.flush()
                        if time.monotonic() - last_progress > .4:
                            touch(progress={'event_count': execution['event_count'], 'last_activity_at': now()}, execution=execution)
                            last_progress = time.monotonic()
                        if method == 'worker/interactiveRequestDenied':
                            raise JobError('INTERACTIVE_REQUEST_DENIED', '模型请求了交互或额外权限，已拒绝且未自动重跑。')
                        if method == 'turn/completed':
                            terminal = True
                            turn = data['turn']
                            for item in turn.get('items', []):
                                self.check_item(item)
                                if item.get('type') == 'agentMessage' and isinstance(item.get('text'), str):
                                    final_messages[item['id']] = (item.get('phase'), item['text'])
                            if turn.get('status') != 'completed':
                                execution.update(turn_failed=True, exit_code=1)
                                detail = redactor.text(json.dumps(turn.get('error'), ensure_ascii=False))
                                errors.write(detail.encode('utf-8'))
                                raise JobError('EXECUTION_FAILED', 'Codex 执行未成功完成，请查看脱敏日志。')
                            choices = [text for phase, text in final_messages.values() if phase == 'final_answer']
                            if not choices:
                                choices = [text for phase, text in final_messages.values() if phase is None]
                            if not choices:
                                raise JobError('INCOMPLETE_TURN', '未收到最终回答，不能仅凭完成事件交付。')
                            payload = choices[-1].encode('utf-8')
                            if len(payload) > self.settings.max_output_bytes:
                                raise JobError('LIMIT_EXCEEDED', '最终回答超过大小限制。')
                            continuation = self.next_turn(thread_id, root, payload)
                            if continuation is not None:
                                atomic_write(root / 'control/draft_response.raw.json', payload)
                                if stopped():
                                    raise JobError('CANCEL_REQUESTED', '整单在最终检查前已请求停止。')
                                if codex_config_digest(self.settings) != config_sha:
                                    raise JobError('CODEX_CONFIG_CHANGED', '整单会话期间配置变化，未继续调用模型。')
                                result = await rpc.request('turn/start', continuation)
                                turn_id = (result.get('turn') or {}).get('id')
                                if not turn_id:
                                    raise JobError('INVALID_TRACE', 'App Server 未返回检查轮次编号。')
                                terminal = False
                                execution['turn_ids'].append(turn_id)
                                execution.update(turn_id=turn_id, project_session=self.project_session.progress())
                                final_messages.clear()
                                touch(execution=execution, stage='project_self_review')
                                continue
                            atomic_write(root / 'control/final_response.raw.md', payload)
                            self.delivered(root, payload)
                            session = getattr(self, 'project_session', None)
                            if session is not None:
                                execution.update(project_session=session.progress(), self_review_status='completed')
                            execution.update(turn_completed=True, exit_code=0,
                                model_turn_seconds=round(time.monotonic() - started, 3))
                            return execution
            except AppServerRpcError as exc:
                atomic_write(error_path, redactor.text(exc.detail).encode('utf-8'))
                raise
            finally:
                async def cleanup():
                    result = await self._finish_thread(thread_id, turn_id, terminal, queue)
                    execution.update(runtime_cleanup=result, ended_at=now())
                    touch(execution=execution)
                task = asyncio.create_task(cleanup())
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    await task
                    raise
