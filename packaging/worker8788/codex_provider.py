"""Read the user's Codex configuration; never consult switcher databases."""
import hashlib
import json
import os
import re
import time
from pathlib import Path
import tomllib
from urllib.parse import urlparse

EFFORTS = ('none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra')


def read_config(home):
    config = tomllib.loads((home/'config.toml').read_text(encoding='utf-8-sig'))
    profile = config.get('profile')
    if profile:
        if not isinstance(profile, str) or any(c in profile for c in '/\\:\x00') or profile in ('.', '..'):
            raise ValueError('Codex profile 无效。')
        path = home/(profile+'.config.toml')
        extra = tomllib.loads(path.read_text(encoding='utf-8-sig')) if path.is_file() else config.get('profiles', {}).get(profile, {})
        def merge(a, b):
            result = dict(a)
            for k, v in b.items():
                result[k] = merge(result[k], v) if isinstance(result.get(k), dict) and isinstance(v, dict) else v
            return result
        config = merge(config, extra)
    return config


def normalize_models(data):
    result = []
    for m in data:
        name = m.get('model') or m.get('slug') or m.get('id')
        levels = m.get('reasoning_efforts', m.get('supported_reasoning_levels', []))
        levels = [x.get('effort') if isinstance(x, dict) else x for x in levels]
        levels = [x for x in levels if x in EFFORTS]
        if isinstance(name, str) and re.fullmatch(r'[A-Za-z0-9._/:-]{1,200}', name) and m.get('visibility', 'list') != 'hide':
            result.append({'model': name, 'display_name': m.get('display_name', name),
                           'reasoning_efforts': levels or ['default'],
                           'default_effort': m.get('default_reasoning_level', 'default')})
    return result


def models_for(config, home, fetch=None):
    provider = config.get('model_provider', 'openai')
    entry = config.get('model_providers', {}).get(provider, {})
    catalog = config.get('model_catalog_json')
    if catalog:
        path = Path(catalog)
        if not path.is_absolute(): path = home/path
        return normalize_models(json.loads(path.read_text(encoding='utf-8-sig')).get('models', []))
    if provider == 'openai':
        path = home/'models_cache.json'
        return normalize_models(json.loads(path.read_text(encoding='utf-8-sig')).get('models', [])) if path.exists() else []
    if urlparse(entry.get('base_url', '')).hostname == 'openrouter.ai':
        if fetch is None:
            import httpx
            def fetch():
                cache=Path(r'C:\CodexWorkerData-AppServer-8788\private\openrouter-models-cache.json')
                if cache.exists() and time.time()-cache.stat().st_mtime<900:
                    return json.loads(cache.read_text(encoding='utf-8'))
                r = httpx.get('https://openrouter.ai/api/v1/models', timeout=15)
                r.raise_for_status()
                data=r.json()['data']
                cache.write_text(json.dumps(data),encoding='utf-8')
                return data
        result = []
        for m in fetch():
            # Continuous project sessions require text output and dynamic tools.
            if 'tools' not in m.get('supported_parameters', []): continue
            if 'text' not in m.get('architecture', {}).get('output_modalities', ['text']): continue
            reasoning = m.get('reasoning') or {}
            levels = reasoning.get('supported_efforts') or []
            # A generic reasoning capability does not establish exact effort levels.
            if not levels and m['id'] == config.get('model') and config.get('model_reasoning_effort'):
                levels = [config['model_reasoning_effort']]
            result.append({'model': m['id'], 'display_name': m.get('name', m['id']),
                           'reasoning_efforts': [x for x in levels if x in EFFORTS] or ['default'],
                           'default_effort': reasoning.get('default_effort', 'default')})
        return result
    model = config.get('model')
    return [{'model': model, 'display_name': model,
             'reasoning_efforts': [config.get('model_reasoning_effort') or 'default']}] if model else []


def snapshot(home, fallback=None, fetch=None):
    config = read_config(home)
    provider = config.get('model_provider', 'openai')
    entry = config.get('model_providers', {}).get(provider, {})
    if provider != 'openai' and entry.get('wire_api', 'responses') != 'responses':
        raise ValueError('当前供应商需要配置 Responses API 才能用于 Codex。')
    models = models_for(config, home, fetch)
    if not models:
        raise ValueError('当前 Codex 没有可读取的模型目录，请先在 Codex 中刷新模型。')
    names = {m['model']: m for m in models}
    model = config.get('model')
    if model and model not in names:
        raise ValueError('当前 Codex 模型不在可用目录中，或不支持整单项目所需的工具调用。')
    if not model and fallback and fallback.get('model_provider', 'openai') == provider:
        model = fallback.get('model')
    if model not in names: model = models[0]['model']
    effort = config.get('model_reasoning_effort')
    if not effort and fallback: effort = fallback.get('model_reasoning_effort')
    if effort not in names[model]['reasoning_efforts']:
        effort = names[model].get('default_effort')
        if effort not in names[model]['reasoning_efforts']:effort=names[model]['reasoning_efforts'][0]
    auth = json.loads((home/'auth.json').read_text(encoding='utf-8-sig')) if (home/'auth.json').exists() else {}
    env = {}
    for key in [entry.get('env_key'), *entry.get('env_http_headers', {}).values()]:
        if key:
            if not os.environ.get(key): raise ValueError('当前 Codex 供应商需要的环境变量未设置。')
            env[key] = os.environ[key]
    selected = {k: config[k] for k in ('model_providers',) if k in config}
    selected.update(model_provider=provider, model=model, cli_auth_credentials_store='file', thread_unload_delay_secs=0)
    if effort != 'default': selected['model_reasoning_effort'] = effort
    public = {'configured_provider': provider,
              'provider_name': entry.get('name', 'OpenAI 官方' if provider == 'openai' else provider),
              'configured_model': model, 'configured_reasoning_effort': effort,
              'available_models': models}
    # Fingerprint the routing config, not personal prompts, plugins or task content.
    fingerprint = hashlib.sha256(json.dumps({'config': selected, 'catalog': models}, sort_keys=True).encode()).hexdigest()
    public['source_signature'] = fingerprint
    return {'config': selected, 'auth': auth, 'env': env, 'public': public}


def to_toml(config):
    def value(v):
        if isinstance(v, dict): return '{' + ', '.join(json.dumps(k)+' = '+value(x) for k,x in v.items()) + '}'
        return json.dumps(v, ensure_ascii=False)
    return '\n'.join(json.dumps(k)+' = '+value(v) for k,v in config.items())+'\n'
