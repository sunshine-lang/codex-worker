"""Isolated port-8788 entrypoint. Reuses baseline credentials in memory only."""
from __future__ import annotations

import argparse
import asyncio
from contextlib import asynccontextmanager
import hashlib
import json
from pathlib import Path
import sys
import uuid

from .app import create_app
from .app_server_rpc import AppServerRpc, AppServerRpcError
from .app_server_runner import AppServerRunner, thread_parameters, verify_thread
from .common import JobError, Redactor, write_json
from .config import load_settings, read_codex_config
from .doctor import verify
from .sandbox_state import capture_sandbox_state, same_sandbox_state


def trial_settings(path):
    overrides = json.loads(path.read_text(encoding='utf-8-sig'))
    required = {'baseline_root', 'data_root', 'port', 'bind_host', 'codex_executable', 'expected_codex_version'}
    if not required <= set(overrides) or set(overrides) - required - {'max_concurrent_jobs', 'task_batch_concurrency', 'authentication_mode', 'codex_home', 'project_context_enabled'}:
        raise ValueError('Unknown trial configuration fields')
    project_context = overrides.get('project_context_enabled', False)
    if type(project_context) is not bool:
        raise ValueError('project_context_enabled must be boolean')
    jobs = overrides.get('max_concurrent_jobs', 1)
    batches = overrides.get('task_batch_concurrency', 1)
    if type(jobs) is not int or type(batches) is not int or not 1 <= batches <= jobs <= 8:
        raise ValueError('8788 concurrency must be bounded to one through eight')
    original = Path(overrides['baseline_root']).resolve(strict=True)
    base_path = original / 'config.local.json'
    config = json.loads(base_path.read_text(encoding='utf-8-sig'))
    original_data = Path(config['data_root']).resolve(strict=True)
    settings = load_settings(base_path, original_data / 'private/credentials.local.json')
    data = Path(overrides['data_root'])
    if not data.is_absolute() or data.resolve().is_relative_to(original_data) or original_data.is_relative_to(data.resolve()):
        raise ValueError('Trial data must be independent from original data')
    if overrides['port'] != 8788 or overrides['bind_host'] not in ('127.0.0.1', '0.0.0.0'):
        raise ValueError('This trial entrypoint is only for port 8788')
    executable = Path(overrides['codex_executable']).resolve(strict=True)
    if executable.suffix.lower() != '.exe':
        raise ValueError('Native Codex executable required')
    authentication = overrides.get('authentication_mode', settings.authentication_mode)
    if authentication not in ('server_codex', 'chatgpt'):
        raise ValueError('Unknown authentication mode')
    home = settings.codex_home
    if authentication == 'chatgpt':
        home = Path(overrides.get('codex_home', ''))
        if not home.is_absolute() or home.resolve() != (data.resolve() / 'private/codex-home').resolve():
            raise ValueError('8788 subscription requires its own private Codex home')
        home = home.resolve(strict=True)
    elif 'codex_home' in overrides:
        raise ValueError('Codex home override requires subscription mode')
    settings = settings.model_copy(update={'data_root': data.resolve(), 'port': 8788,
        'authentication_mode': authentication, 'codex_home': home,
        'provider_env': {} if authentication == 'chatgpt' else settings.provider_env,
        'codex_executable': executable, 'expected_codex_version': overrides['expected_codex_version'],
        'tool_read_roots': list(dict.fromkeys([*settings.tool_read_roots, executable.parent])),
        'bind_host': overrides['bind_host'],
        'max_concurrent_jobs': jobs, 'task_batch_concurrency': 1 if project_context else batches,
        'project_context_enabled': project_context,
        'protected_read_roots': list(dict.fromkeys([*settings.protected_read_roots, original_data, settings.codex_home]))})
    return settings


def build_app(settings):
    from .app_server_pool import ParallelTextAppServerRunner
    runner = ParallelTextAppServerRunner(settings)
    app = create_app(settings, runner=runner)
    baseline_lifespan = app.router.lifespan_context
    @asynccontextmanager
    async def lifespan(app):
        try:
            await runner.start()
            async with baseline_lifespan(app):
                yield
        finally:
            await runner.close()
    app.router.lifespan_context = lifespan
    return app


async def protocol_probe(settings, *, without_environment=False):
    root = settings.data_root / 'preflight' / ('appserver-protocol-' + uuid.uuid4().hex)
    for name in ('work', 'control', 'logs'):
        (root / name).mkdir(parents=True, exist_ok=True)
    initial = capture_sandbox_state(settings)
    redactor = Redactor(settings.secret_values(), [settings.data_root, settings.codex_home])
    rpc = AppServerRpc(settings, root, redactor)
    try:
        info = await rpc.start()
        parameters = thread_parameters(settings, root)
        if without_environment:
            parameters['environments'] = []
            parameters['runtimeWorkspaceRoots'] = []
            parameters['config'].update({'features.shell_tool': False, 'features.plugins': False,
                'features.apps': False, 'web_search': 'disabled'})
        response = await rpc.request('thread/start', parameters)
        configured = read_codex_config(settings.codex_home)
        thread_id = verify_thread(response, configured, root)
        await rpc.request('thread/unsubscribe', {'threadId': thread_id})
        final = capture_sandbox_state(settings)
        if not same_sandbox_state(initial, final):
            raise JobError('SANDBOX_STATE_CHANGED', '协议探测期间共享沙箱账户状态变化，未启动测试服务。')
        result = {'passed': True, 'transport': 'private-stdio', 'model_called': False,
            'model_turns_started': 0, 'windows_platform_confirmed': info.get('platformFamily') == 'windows',
            'effective_model': response.get('model'), 'effective_provider': response.get('modelProvider'),
            'effective_reasoning_effort': response.get('reasoningEffort'),
            'active_permission_profile': response.get('activePermissionProfile'),
            'approval_policy': response.get('approvalPolicy'), 'shared_sandbox_state_unchanged': True,
            'new_thread_per_job': True, 'original_http_contract_unchanged': True,
            'environmentless_probe_only': without_environment, 'deployed_service_uses_environmentless_mode': False}
        write_json(settings.data_root / ('app-server-no-environment-protocol-verification.json' if without_environment else 'app-server-protocol-verification.json'), result)
        return result
    except AppServerRpcError as exc:
        write_json(settings.data_root / 'app-server-protocol-error.json', {'code': exc.rpc_code, 'detail': redactor.text(exc.detail)})
        raise
    finally:
        await rpc.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=('serve', 'doctor', 'protocol-probe'))
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--without-environment', action='store_true')
    args = parser.parse_args()
    try:
        settings = trial_settings(args.config)
        if args.mode == 'doctor':
            result = asyncio.run(verify(settings))
            print(json.dumps(result, ensure_ascii=False))
            return 0 if result.get('passed') else 1
        if args.mode == 'protocol-probe':
            print(json.dumps(asyncio.run(protocol_probe(settings, without_environment=args.without_environment)), ensure_ascii=False))
            return 0
        import uvicorn
        uvicorn.run(build_app(settings), host=settings.bind_host, port=settings.port, workers=1,
                    access_log=False, timeout_graceful_shutdown=35)
        return 0
    except JobError as exc:
        print(json.dumps({'error': exc.public()}, ensure_ascii=False), file=sys.stderr)
        return 1
    except Exception as exc:
        print(json.dumps({'error': {'code': 'TRIAL_START_FAILED', 'exception_type': type(exc).__name__}}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
