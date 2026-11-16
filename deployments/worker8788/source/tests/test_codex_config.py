import asyncio
import json
from pathlib import Path

import pytest

from codex_remote_worker.common import JobError, Redactor
from codex_remote_worker.config import load_settings, read_codex_config
from codex_remote_worker.runner import CodexRunner, build_command, codex_config_digest, environment_fingerprint


def write_settings(settings, tmp_path):
    path = tmp_path / 'config.local.json'
    values = settings.model_dump(mode='json', exclude={'service_token', 'provider_env'})
    values['models'] = {'obsolete/model': {'efforts': [], 'web_search': False}}
    path.write_text(json.dumps(values))
    return path


def test_existing_codex_config_and_environment_supply_provider_credentials(settings, tmp_path, monkeypatch):
    path = write_settings(settings, tmp_path)
    private = settings.data_root / 'private'
    private.mkdir(parents=True)
    credentials = private / 'credentials.local.json'
    credentials.write_text(json.dumps({'CODEX_WORKER_SERVICE_TOKEN': settings.service_token.get_secret_value(),
                                       'FIXTURE_PROVIDER_KEY': 'obsolete-worker-value'}))
    monkeypatch.setenv('FIXTURE_PROVIDER_KEY', 'from-service-account-env')
    monkeypatch.setenv('UNRELATED_SECRET', 'must-not-inherit')
    actual = load_settings(path, credentials)
    assert actual.codex_home == settings.codex_home
    assert actual.provider_env['FIXTURE_PROVIDER_KEY'].get_secret_value() == 'from-service-account-env'
    assert 'UNRELATED_SECRET' not in actual.provider_env
    assert 'CODEX_WORKER_SERVICE_TOKEN' not in actual.provider_env
    assert not hasattr(actual, 'models')


def test_codex_auth_file_does_not_require_a_separate_provider_key(settings, tmp_path, monkeypatch):
    for key in ['OPENAI_API_KEY', 'OPENROUTER_API_KEY', 'FIXTURE_PROVIDER_KEY']:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv('CODEX_WORKER_SERVICE_TOKEN', settings.service_token.get_secret_value())
    (settings.codex_home / 'config.toml').write_text('model = "existing-server-model"\n')
    (settings.codex_home / 'auth.json').write_text(json.dumps({'OPENAI_API_KEY': 'fixture-auth-file-secret'}))
    actual = load_settings(write_settings(settings, tmp_path))
    assert not actual.provider_env
    assert 'fixture-auth-file-secret' in actual.secret_values()
    assert 'fixture-auth-file-secret' not in Redactor(actual.secret_values()).text('Error fixture-auth-file-secret')


def test_invalid_config_errors_do_not_echo_secrets(settings):
    (settings.codex_home / 'config.toml').write_text('model = "secret-value-without-closing-quote')
    with pytest.raises(JobError) as error:
        read_codex_config(settings.codex_home)
    assert error.value.code == 'CODEX_CONFIG_INVALID'
    assert 'secret-value' not in str(error.value)


def test_config_change_invalidates_preflight_and_queued_job(settings, tmp_path, monkeypatch):
    for path in [settings.codex_executable, settings.doc_converter]:
        path.write_bytes(b'executable fixture')
    before = environment_fingerprint(settings)
    saved = codex_config_digest(settings)
    (settings.codex_home / 'config.toml').write_text('model = "changed-model"\n')
    assert environment_fingerprint(settings) != before
    runner = CodexRunner(settings)
    monkeypatch.setattr(runner, 'ready', lambda: {'ready': True})
    with pytest.raises(JobError) as error:
        asyncio.run(runner.run({'codex_config_sha256': saved}, tmp_path, lambda **kw: None, lambda: False, Redactor()))
    assert error.value.code == 'CODEX_CONFIG_CHANGED'


def test_old_queued_jobs_are_not_silently_switched_to_server_default(settings, tmp_path):
    with pytest.raises(JobError) as error:
        build_command(settings, {'options': {'model_id': 'old/explicit-model'}}, tmp_path)
    assert error.value.code == 'LEGACY_TASK'
