"""Read-only official-login checks. Never copies, logs in, logs out, or rewrites credentials."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import threading
import time
import tomllib

from .common import JobError
from .config import load_settings, read_codex_config

_cache = {}
_lock = threading.RLock()


def selected_profile_path(settings):
    config = read_codex_config(settings.codex_home)
    name = config.get('profile')
    if name is None:
        return None
    if not isinstance(name, str) or not name or name in ('.', '..') or any(c in name for c in '/\\:\x00\r\n'):
        raise JobError('CODEX_CONFIG_INVALID', 'Codex profile 名称不合法，未修改原配置。', 503)
    path = settings.codex_home / (name + '.config.toml')
    return path if path.is_file() else None


def effective_config(settings):
    config = read_codex_config(settings.codex_home)
    profiles = config.get('profiles', {})
    if not isinstance(profiles, dict):
        raise JobError('CODEX_CONFIG_INVALID', 'Codex profiles 配置不合法，未修改原配置。', 503)
    name = config.get('profile')
    profile_path = selected_profile_path(settings)
    try:
        selected = (tomllib.loads(profile_path.read_text(encoding='utf-8-sig')) if profile_path is not None
                    else profiles.get(name, {}))
        if not isinstance(selected, dict):
            raise ValueError('invalid profile')
    except (OSError, ValueError, TypeError):
        raise JobError('CODEX_CONFIG_INVALID', '无法读取 Codex profile，未修改原配置。', 503) from None
    def merge(base, extra):
        result = dict(base)
        for key, value in extra.items():
            result[key] = merge(result[key], value) if isinstance(result.get(key), dict) and isinstance(value, dict) else value
        return result
    return merge(config, selected)


def classify_login(returncode, output):
    value = output.casefold()
    return returncode == 0 and 'chatgpt' in value and 'api key' not in value and 'api_key' not in value


def require_chatgpt_login(settings, *, refresh=False):
    config = effective_config(settings)
    if config.get('model_provider', 'openai') != 'openai':
        raise JobError('CHATGPT_PROVIDER_REQUIRED', '当前 Codex 配置不是官方提供商；请在桌面端确认使用官方订阅。Worker 不会修改个人配置。', 503)
    path = settings.codex_home / 'config.toml'
    signature = path.stat().st_mtime_ns if path.exists() else None
    key = (str(settings.codex_executable), str(settings.codex_home), signature)
    with _lock:
        if not refresh and _cache.get(key, 0) > time.monotonic():
            return {'passed': True, 'authentication_mode': 'chatgpt', 'model_called': False}
        env = {k: v for k, v in os.environ.items() if not k.upper().startswith('CODEX_')
               and k.upper() not in {'OPENAI_API_KEY', 'OPENROUTER_API_KEY', 'OPENAI_BASE_URL'}}
        env['CODEX_HOME'] = str(settings.codex_home)
        try:
            result = subprocess.run([str(settings.codex_executable), 'login', 'status'],
                                    cwd=settings.codex_home, env=env, stdin=subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15,
                                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            # The CLI may print API-key information. Never emit or persist its raw output.
            output = (result.stdout + result.stderr).decode('utf-8', errors='replace')
            good = classify_login(result.returncode, output)
        except (OSError, subprocess.TimeoutExpired):
            raise JobError('CHATGPT_LOGIN_CHECK_FAILED', '无法验证桌面用户的官方登录；请用同一 Windows 用户打开 Codex 后重试。', 503) from None
        if not good:
            _cache.pop(key, None)
            raise JobError('CHATGPT_LOGIN_REQUIRED', '未检测到可复用的官方订阅登录。请先用当前 Windows 用户在 Codex 桌面端登录，再重试；不会要求 API Key。', 503)
        _cache[key] = time.monotonic() + 15
        return {'passed': True, 'authentication_mode': 'chatgpt', 'model_called': False}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--credentials-file', type=Path, required=True)
    args = parser.parse_args()
    try:
        settings = load_settings(args.config, args.credentials_file)
        result = require_chatgpt_login(settings, refresh=True)
        config = effective_config(settings)
        mode = config.get('windows', {}).get('sandbox')
        if mode is None:
            mode = 'elevated' if (settings.codex_home / '.sandbox/setup_marker.json').is_file() else 'mxc'
        if mode not in ('mxc', 'elevated'):
            raise JobError('SANDBOX_MODE_UNSUPPORTED', '桌面端沙箱模式与本版 Worker 不兼容；未改动原有配置。', 503)
        result['windows_sandbox'] = mode
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except JobError as error:
        print(json.dumps({'passed': False, **error.public()}, ensure_ascii=False))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())


def require_configured_login(settings, *, refresh=False):
    config = effective_config(settings)
    if settings.authentication_mode != 'codex_auto' or config.get('model_provider', 'openai') == 'openai':
        return require_chatgpt_login(settings, refresh=refresh)
    provider = config.get('model_providers', {}).get(config.get('model_provider'), {})
    if not provider or provider.get('wire_api', 'responses') != 'responses':
        raise JobError('PROVIDER_CONFIG_INVALID', '当前供应商需要有效的 Responses API 配置。', 503)
    auth_path = settings.codex_home / 'auth.json'
    try:
        auth = json.loads(auth_path.read_text(encoding='utf-8')) if auth_path.is_file() else {}
    except (OSError, ValueError):
        raise JobError('PROVIDER_AUTH_INVALID', '无法读取当前供应商认证。', 503) from None
    env_key = provider.get('env_key')
    if env_key:
        good = env_key in settings.provider_env
    else:
        good = bool(auth.get('OPENAI_API_KEY') or provider.get('experimental_bearer_token')
                    or provider.get('http_headers', {}).get('Authorization'))
    if not good:
        raise JobError('PROVIDER_AUTH_REQUIRED', '当前供应商缺少 API 认证，请先在 Codex 中配置。', 503)
    return {'passed': True, 'authentication_mode': 'codex_auto', 'model_called': False}
