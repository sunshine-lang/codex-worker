from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import platform
import re
from functools import lru_cache

from . import __version__
from .common import JobError, canonical, digest, file_digest, now
from .config import read_codex_config
from .windows import WindowsProcess, reconcile_process
from .sandbox_state import capture_sandbox_state, same_sandbox_state
from .task_runtime import prepare_task_runtime, runtime_instructions
from . import task_tools
from .subscription import require_chatgpt_login, effective_config, selected_profile_path


POLICY_VERSION = 'workspace-only-server-codex-v3-runtime-tools'


def send_prompt(pipe, payload):
    try:
        remaining = memoryview(payload)
        while remaining:
            count = pipe.write(remaining)
            if not count:
                raise OSError('stdin closed')
            remaining = remaining[count:]
    except OSError as exc:
        raise JobError('INPUT_DELIVERY_FAILED', 'Prompt 未完整发送，已停止任务。') from exc
    finally:
        pipe.close()


def runtime_environment(settings, root: Path):
    allowed = {'systemroot', 'windir', 'comspec', 'pathext', 'userprofile', 'appdata', 'localappdata',
               'programfiles', 'programfiles(x86)', 'programdata', 'username', 'userdomain',
               'http_proxy', 'https_proxy', 'all_proxy', 'no_proxy', 'ssl_cert_file', 'codex_ca_certificate'}
    env = {key: value for key, value in os.environ.items() if key.lower() in allowed}
    temp = root / 'work' / '.task_tmp'
    temp.mkdir(exist_ok=True)
    system = Path(os.environ.get('SystemRoot', r'C:\Windows'))
    env['PATH'] = os.environ.get('PATH') or os.pathsep.join(map(str, [settings.tool_python.parent, settings.doc_converter.parent,
        settings.codex_executable.parent, system / 'System32', system / 'System32' / 'WindowsPowerShell' / 'v1.0']))
    if settings.authentication_mode != 'chatgpt':
        env.update({name: value.get_secret_value() for name, value in settings.provider_env.items()})
    env.update(CODEX_HOME=str(settings.codex_home), TEMP=str(temp), TMP=str(temp), TMPDIR=str(temp),
               PYTHONUTF8='1', PYTHONIOENCODING='utf-8')
    return env


def config_arguments(settings, root):
    env = runtime_environment(settings, root)
    private_env = {name.upper() for name in settings.provider_env} | {
        'CODEX_HOME', 'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'NO_PROXY'}
    tools_env = {key: value for key, value in env.items() if key.upper() not in private_env}
    system = Path(os.environ.get('SystemRoot', r'C:\Windows'))
    tools_env['PATH'] = os.pathsep.join(map(str, [settings.tool_python.parent, settings.doc_converter.parent,
        settings.codex_executable.parent, system / 'System32', system / 'System32' / 'WindowsPowerShell' / 'v1.0']))
    profile = root / 'work' / '.task_profile'
    roaming, local = profile / 'AppData' / 'Roaming', profile / 'AppData' / 'Local'
    roaming.mkdir(parents=True, exist_ok=True)
    local.mkdir(parents=True, exist_ok=True)
    tools_env.update(USERPROFILE=str(profile), APPDATA=str(roaming), LOCALAPPDATA=str(local))
    tools_env.update(WORKER_TASK_ROOT=str(root / 'work'), WORKER_PYTHON=str(settings.tool_python),
                     WORKER_HELPER=str(root / 'work' / '.worker' / 'task_tools.py'))
    # The elevated Windows sandbox requires :root read access; sensitive roots stay explicitly denied.
    filesystem = {':root': 'read', ':minimal': 'read', str(settings.data_root): 'deny',
                  ':tmpdir': 'deny', ':slash_tmp': 'deny', str(root / 'work'): 'write'}
    for path in settings.protected_read_roots:
        filesystem[str(path)] = 'deny'
    for path in settings.tool_read_roots:
        filesystem[str(path)] = 'read'
    filesystem[str(settings.codex_home)] = 'deny'
    # Never combine this profile with --sandbox: legacy flags override named permissions.
    overrides = {
        'default_permissions': 'translation',
        'permissions.translation.extends': ':workspace',
        'permissions.translation.filesystem': filesystem,
        'permissions.translation.network.enabled': False,
        'windows.sandbox': settings.windows_sandbox,
        'shell_environment_policy.inherit': 'none',
        'shell_environment_policy.ignore_default_excludes': False,
        'shell_environment_policy.experimental_use_profile': False,
        'features.shell_snapshot': False,
        'shell_environment_policy.set': tools_env,
        'developer_instructions': '\n\n'.join(filter(None, [
            read_codex_config(settings.codex_home).get('developer_instructions', ''),
            runtime_instructions(settings, root)])),
    }
    if settings.authentication_mode == 'chatgpt':
        personal = effective_config(settings)
        # A translation worker must not inherit the user's personal connected tools.
        overrides.update({'model_provider': 'openai', 'features.apps': False, 'apps._default.enabled': False,
                          'plugins._default.enabled': False})
        for section in ('mcp_servers', 'apps', 'plugins'):
            entries = personal.get(section, {})
            if isinstance(entries, dict):
                for name, entry in entries.items():
                    if isinstance(entry, dict):
                        overrides[section + '.' + json.dumps(name) + '.enabled'] = False
    result = []
    for key, value in overrides.items():
        result += ['-c', key + '=' + toml_value(value)]
    return result


def toml_value(value):
    if isinstance(value, dict):
        return '{' + ','.join(json.dumps(k) + '=' + toml_value(v) for k, v in value.items()) + '}'
    return json.dumps(value, ensure_ascii=False)


def build_command(settings, job, root):
    if {'model_id', 'reasoning_effort', 'web_search'} & job['options'].keys():
        raise JobError('LEGACY_TASK', '该任务来自旧版模型参数协议，请使用新版 Langflow 节点重新提交。')
    command = [str(settings.codex_executable), '-a', 'never', *config_arguments(settings, root)]
    return command + ['exec', '--json', '--color', 'never', '--skip-git-repo-check', '--cd', str(root / 'work'),
                      '--output-last-message', str(root / 'control' / 'final_response.raw.md'), '-']


def codex_config_digest(settings):
    path = settings.codex_home / 'config.toml'
    root_digest = file_digest(path) if path.exists() else None
    if settings.authentication_mode == 'chatgpt':
        profile = selected_profile_path(settings)
        if profile is not None:
            return digest(canonical({'config': root_digest, 'profile': file_digest(profile)}))
    return root_digest


@lru_cache(maxsize=64)
def _binary_digest(path, signature):
    value = file_digest(Path(path))
    if _binary_signature(Path(path)) != signature:
        raise ValueError('Runtime executable changed while hashing')
    return value


def _binary_signature(path):
    stat = path.stat()
    return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)


def environment_fingerprint(settings):
    account = os.environ.get('USERDOMAIN', '') + '\\' + os.environ.get('USERNAME', '')
    if os.name == 'nt':
        import win32api
        import win32con
        import win32security
        token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
        try:
            sid, _ = win32security.GetTokenInformation(token, win32security.TokenUser)
            account = win32security.ConvertSidToStringSid(sid)
        finally:
            token.Close()
    values = {'policy': POLICY_VERSION, 'os': platform.platform(),
              'account': account,
              'settings': settings.model_dump(mode='json', exclude={'service_token', 'provider_env'}),
              'codex_config_sha256': codex_config_digest(settings)}
    for key in ['codex_executable', 'tool_python', 'doc_converter']:
        path = getattr(settings, key)
        values[key + '_sha256'] = _binary_digest(str(path), _binary_signature(path))
    helper = Path(task_tools.__file__)
    values['task_tools_sha256'] = _binary_digest(str(helper), _binary_signature(helper))
    return digest(canonical(values))


class CodexRunner:
    def __init__(self, settings):
        self.settings = settings

    def ready(self):
        if os.name != 'nt':
            return {'ready': False, 'code': 'WINDOWS_REQUIRED', 'message': '正式执行需使用 Windows；本机仅可做模拟测试。'}
        try:
            if (self.settings.data_root / '.maintenance.json').exists():
                return {'ready': False, 'code': 'MAINTENANCE', 'message': '正在维护服务，请稍后重试。'}
            import win32job  # noqa: F401
            from win32com.shell import shell
            if shell.IsUserAnAdmin():
                return {'ready': False, 'code': 'EXCESSIVE_PRIVILEGE', 'message': '请使用非管理员的专用运行账户。'}
            if not self.settings.codex_home.is_dir():
                return {'ready': False, 'code': 'CODEX_HOME_MISSING', 'message': '请将 codex_home 指向服务账户已有的 Codex 配置目录。'}
            read_codex_config(self.settings.codex_home)
            for path in [self.settings.codex_executable, self.settings.tool_python, self.settings.doc_converter]:
                if not path.is_file() or path.suffix.lower() != '.exe':
                    raise ValueError('native executable required')
            for path in self.settings.tool_read_roots:
                if not path.is_dir() or path.is_relative_to(self.settings.data_root):
                    raise ValueError('invalid tool root')
            receipt = json.loads((self.settings.data_root / 'preflight.json').read_text(encoding='utf-8'))
            if receipt.get('fingerprint') != environment_fingerprint(self.settings) or not receipt.get('passed'):
                raise ValueError('receipt mismatch')
            if self.settings.windows_sandbox == 'elevated':
                current = capture_sandbox_state(self.settings)
                if not same_sandbox_state(receipt.get('sandbox_state'), current):
                    return {'ready': False, 'code': 'SANDBOX_STATE_CHANGED',
                            'message': 'Windows 沙箱账户已被其他 Codex 实例重置；请在服务启动器中点击“修复沙箱”。'}
            if self.settings.authentication_mode == 'chatgpt':
                require_chatgpt_login(self.settings)
            return {'ready': True, 'code': 'READY', 'codex_version': receipt['codex_version'],
                    'tools': receipt['tools'], 'sandbox_verified_at': receipt['verified_at'],
                    'upstream_model_verified': False, 'configuration_source': 'server_codex', 'service_version': __version__}
        except JobError as exc:
            return {'ready': False, **exc.public()}
        except (OSError, ValueError, KeyError, ImportError):
            return {'ready': False, 'code': 'PREFLIGHT_REQUIRED',
                    'message': '请使用正式服务账户运行 doctor --verify-sandbox；配置改变后需重新自检。'}

    def recover(self, job):
        return reconcile_process(job.get('process'))

    async def run(self, job, root, touch, stopped, redactor):
        ready = self.ready()
        if not ready['ready']:
            raise JobError('NOT_READY', ready['message'], 503)
        if stopped():
            raise JobError('CANCEL_REQUESTED', '任务在进程启动前已请求停止。')
        config_sha = codex_config_digest(self.settings)
        if 'codex_config_sha256' in job and job['codex_config_sha256'] != config_sha:
            raise JobError('CODEX_CONFIG_CHANGED', '任务提交后 Codex 配置已变化，请确认配置后重新提交。')
        configured = (effective_config(self.settings) if self.settings.authentication_mode == 'chatgpt'
                      else read_codex_config(self.settings.codex_home))
        prompt = (root / 'input' / job['inputs']['prompt_file']['filename']).read_bytes()
        if digest(prompt) != job['prompt_sha256']:
            raise JobError('INPUT_CHANGED', 'Prompt 原件与提交记录不一致。')
        preparation = await prepare_task_runtime(self.settings, job, root, touch, stopped, redactor)
        if codex_config_digest(self.settings) != config_sha:
            raise JobError('CODEX_CONFIG_CHANGED', '文件准备期间 Codex 配置发生变化，未启动模型。')
        if self.settings.authentication_mode == 'chatgpt':
            await asyncio.to_thread(require_chatgpt_login, self.settings, refresh=True)
            if codex_config_digest(self.settings) != config_sha:
                raise JobError('CODEX_CONFIG_CHANGED', '账号验证期间 Codex 配置变化，未调用模型。')
        # The first model request must know the completed context-page count.
        command = build_command(self.settings, job, root)
        execution = {'harness': 'codex exec', 'service_version': __version__,
                     'codex_version': ready['codex_version'], 'tools': ready['tools'],
                     'configuration_source': 'server_codex', 'authentication_mode': self.settings.authentication_mode,
                     'codex_config_sha256': config_sha,
                     'configured_provider': configured.get('model_provider'), 'configured_model': configured.get('model'),
                     'provider': None, 'model_id': None,
                     'resolved_model_id': None, 'upstream_provider': None, 'model_routing_verified': False,
                     'permission_profile': POLICY_VERSION, 'approval_policy': 'never',
                     'started_at': now(), 'prompt_sha256': digest(prompt), 'prompt_bytes': len(prompt),
                     'prompt_mode': 'uploaded_verbatim', 'prompt_delivered': False, 'event_count': 0, 'usage': None,
                     'thread_id': None, 'trace_complete': True, 'turn_completed': False, 'turn_failed': False,
                     'runtime_preparation': preparation}
        touch(execution=execution)
        def registered(metadata):
            touch(process=metadata)
            if stopped():
                raise JobError('CANCEL_REQUESTED', '任务在进程恢复前已请求停止。')
        process = WindowsProcess(command, root / 'work', runtime_environment(self.settings, root),
            'Local\\CodexRemoteWorker-' + job['job_id'], registered)
        touch(stage='agent_running')
        byte_count = 0
        # Pipe reads can block for the whole model call. Keep termination and readiness off this pool.
        pipe_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix='codex-pipe')
        loop = asyncio.get_running_loop()

        async def read_pipe(pipe, filename, events):
            nonlocal byte_count
            with (root / 'logs' / filename).open('wb') as stream:
                while line := await loop.run_in_executor(pipe_pool, pipe.readline, 1024 * 1024 + 1):
                    if len(line) > 1024 * 1024:
                        execution['trace_complete'] = False
                        raise JobError('LIMIT_EXCEEDED', '单条运行事件超过限制。')
                    byte_count += len(line)
                    if byte_count > self.settings.max_log_bytes:
                        execution['trace_complete'] = False
                        raise JobError('LIMIT_EXCEEDED', '执行日志超过任务限制，记录不完整。')
                    if events:
                        try:
                            raw = json.loads(line)
                            if not isinstance(raw, dict) or not isinstance(raw.get('type'), str):
                                raise ValueError('invalid event shape')
                            event = redactor.event(raw)
                        except (UnicodeError, ValueError):
                            execution['trace_complete'] = False
                            event = {'type': 'runner.invalid_event', 'bytes': len(line)}
                        if event is None:
                            continue
                        execution['event_count'] += 1
                        if event.get('type') == 'thread.started':
                            execution['thread_id'] = event.get('thread_id')
                        if event.get('type') == 'turn.completed':
                            execution['usage'] = event.get('usage')
                            execution['turn_completed'] = True
                        if event.get('type') == 'turn.failed':
                            execution['turn_failed'] = True
                        line = json.dumps(event, ensure_ascii=False).encode('utf-8') + b'\n'
                        touch(progress={'event_count': execution['event_count'], 'last_activity_at': now()}, execution=execution)
                    else:
                        line = redactor.text(line.decode('utf-8', errors='replace')).encode('utf-8')
                    stream.write(line)
                    stream.flush()

        readers = [asyncio.create_task(read_pipe(process.stdout, 'trace.jsonl', True)),
                   asyncio.create_task(read_pipe(process.stderr, 'stderr.log', False))]
        try:
            await asyncio.to_thread(send_prompt, process.stdin, prompt)
            execution['prompt_delivered'] = True
            while process.poll() is None:
                if stopped():
                    raise JobError('CANCEL_REQUESTED', '任务已请求停止。')
                for reader in readers:
                    if reader.done() and reader.exception():
                        raise reader.exception()
                await asyncio.sleep(.1)
            execution['exit_code'] = process.poll()
            # Detached tools must not keep editing files while host validation runs.
            await asyncio.to_thread(process.terminate_tree)
            await asyncio.gather(*readers)
            if execution['exit_code'] != 0:
                raise JobError('EXECUTION_FAILED', 'Codex 进程退出失败，请查看脱敏日志。')
            if not execution['trace_complete']:
                raise JobError('INVALID_TRACE', '执行事件不完整，请查看日志。')
            if not execution['turn_completed'] or execution['turn_failed']:
                raise JobError('INCOMPLETE_TURN', '未收到成功的任务完成事件；不能仅凭退出码判断完成。')
            return execution
        finally:
            try:
                cleanup = asyncio.create_task(asyncio.to_thread(process.terminate_tree))
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    await cleanup
                    raise
            finally:
                execution['ended_at'] = now()
                touch(execution=execution)
                for reader in readers:
                    reader.cancel()
                await asyncio.gather(*readers, return_exceptions=True)
                try:
                    process.close()
                finally:
                    pipe_pool.shutdown(wait=True, cancel_futures=True)

