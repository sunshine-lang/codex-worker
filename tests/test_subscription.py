import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from codex_remote_worker.common import JobError
from codex_remote_worker.runner import config_arguments, runtime_environment
from codex_remote_worker.subscription import classify_login, require_chatgpt_login, _cache


@pytest.mark.parametrize('code,text,good', [(0,'Logged in using ChatGPT',True),
    (0,'Logged in using an API key: synthetic-private-key',False),
    (1,'Not logged in using ChatGPT',False), (0,'Unknown authentication',False)])
def test_login_classification(code,text,good):
    assert classify_login(code,text) is good


def prepare(settings):
    (settings.codex_home / 'config.toml').write_text('model_provider="openai"\nmodel="official-user-selection"\n',encoding='utf-8')
    settings.authentication_mode='chatgpt'
    _cache.clear()


def test_existing_login_is_checked_without_copying_or_logging_in(settings,monkeypatch,tmp_path):
    prepare(settings)
    original=(settings.codex_home/'config.toml').read_bytes()
    calls=[]
    def status(command,**kwargs):
        calls.append((command,kwargs))
        return SimpleNamespace(returncode=0,stdout=b'Logged in using ChatGPT',stderr=b'')
    monkeypatch.setattr(subprocess,'run',status)
    monkeypatch.setenv('OPENAI_API_KEY','synthetic-api-key')
    monkeypatch.setenv('CODEX_ACCESS_TOKEN','synthetic-access-token')
    result=require_chatgpt_login(settings,refresh=True)
    assert result['passed'] and not result['model_called']
    assert calls[0][0]==[str(settings.codex_executable),'login','status']
    assert calls[0][1]['env']['CODEX_HOME']==str(settings.codex_home)
    assert 'OPENAI_API_KEY' not in calls[0][1]['env'] and 'CODEX_ACCESS_TOKEN' not in calls[0][1]['env']
    assert (settings.codex_home/'config.toml').read_bytes()==original
    require_chatgpt_login(settings)
    assert len(calls)==1
    require_chatgpt_login(settings,refresh=True)
    assert len(calls)==2


def test_custom_provider_is_not_silently_used_for_subscription(settings,monkeypatch):
    settings.authentication_mode='chatgpt'
    monkeypatch.setattr(subprocess,'run',lambda *a,**k:pytest.fail('Must reject custom provider before running CLI'))
    with pytest.raises(JobError) as error:require_chatgpt_login(settings,refresh=True)
    assert error.value.code=='CHATGPT_PROVIDER_REQUIRED'


@pytest.mark.parametrize('output',[b'Logged in using an API key: never-expose-this-key',b'Not logged in'])
def test_wrong_authentication_is_rejected_without_secret_output(settings,monkeypatch,output):
    prepare(settings)
    monkeypatch.setattr(subprocess,'run',lambda *a,**k:SimpleNamespace(returncode=0,stdout=output,stderr=b''))
    with pytest.raises(JobError) as error:require_chatgpt_login(settings,refresh=True)
    assert error.value.code=='CHATGPT_LOGIN_REQUIRED'
    assert 'never-expose' not in str(error.value.public())


def test_subscription_errors_are_redacted(settings,monkeypatch):
    prepare(settings)
    def fail(*args,**kwargs):raise subprocess.TimeoutExpired('synthetic-secret-command',15,output=b'private-token')
    monkeypatch.setattr(subprocess,'run',fail)
    with pytest.raises(JobError) as error:require_chatgpt_login(settings,refresh=True)
    assert error.value.code=='CHATGPT_LOGIN_CHECK_FAILED' and 'private-token' not in str(error.value.public())


def test_desktop_mode_keeps_model_selection_and_denies_personal_profile(settings,tmp_path):
    prepare(settings)
    settings.protected_read_roots=[tmp_path/'personal-profile']
    root=tmp_path/'job';(root/'work').mkdir(parents=True)
    values=config_arguments(settings,root)
    filesystem=next(v for v in values if v.startswith('permissions.translation.filesystem='))
    assert json.dumps(str(settings.protected_read_roots[0]))+'="deny"' in filesystem
    assert json.dumps(str(root/'work'))+'="write"' in filesystem
    assert not any(v.startswith(('model=','model_reasoning_effort=')) for v in values)
    assert 'model_provider="openai"' in values
    assert 'FIXTURE_PROVIDER_KEY' not in runtime_environment(settings,root)


def test_server_mode_retains_existing_provider_configuration(settings,tmp_path):
    root=tmp_path/'job';(root/'work').mkdir(parents=True)
    assert settings.authentication_mode=='server_codex'
    assert runtime_environment(settings,root)['FIXTURE_PROVIDER_KEY']=='fixture-provider-key'


def test_desktop_installer_has_no_model_fields_or_privileged_system_setup():
    directory=Path(__file__).resolve().parents[1]/'packaging/desktop-user'
    source=(directory/'SetupWizard.cs').read_text(encoding='utf-8')
    assert 'model_api_key' not in source and 'model_name' not in source and 'model_base_url' not in source
    for name in ['configure-install.ps1','start-worker.ps1','desktop-ensure.ps1','desktop-repair.ps1','desktop-stop.ps1','uninstall.ps1']:
        value=(directory/name).read_text(encoding='utf-8-sig')
        for forbidden in ['New-LocalUser','Register-ScheduledTask','New-NetFirewallRule','wfp-network-lockdown.py','service-account.local.json','Initialize-Sandbox']:
            assert forbidden not in value,(name,forbidden)
    manifest=(directory/'setup.manifest').read_text(encoding='utf-8')
    assert 'asInvoker' in manifest and 'requireAdministrator' not in manifest
    assert "bind_host='127.0.0.1'" in (directory/'configure-install.ps1').read_text(encoding='utf-8-sig')


def test_personal_integrations_are_disabled_only_for_worker_process(settings,tmp_path):
    prepare(settings)
    path=settings.codex_home/'config.toml'
    path.write_text(path.read_text(encoding='utf-8')+'\n[mcp_servers."personal.files"]\ncommand="private-tool"\n[apps.personal_mail]\nenabled=true\n[plugins."personal@local"]\nenabled=true\n',encoding='utf-8')
    original=path.read_bytes()
    root=tmp_path/'job';(root/'work').mkdir(parents=True)
    values=config_arguments(settings,root)
    assert 'features.apps=false' in values
    assert 'mcp_servers."personal.files".enabled=false' in values
    assert 'apps."personal_mail".enabled=false' in values
    assert 'plugins."personal@local".enabled=false' in values
    assert path.read_bytes()==original


def test_selected_profile_file_is_read_and_fingerprinted_without_modification(settings,monkeypatch):
    from codex_remote_worker.subscription import effective_config
    from codex_remote_worker.runner import codex_config_digest
    prepare(settings)
    path=settings.codex_home/'config.toml'
    path.write_text(path.read_text(encoding='utf-8')+'profile="official"\n',encoding='utf-8')
    profile=settings.codex_home/'official.config.toml'
    profile.write_text('model="chosen-official-model"\nmodel_provider="openai"\n',encoding='utf-8')
    before=profile.read_bytes()
    assert effective_config(settings)['model']=='chosen-official-model'
    first=codex_config_digest(settings)
    profile.write_text('model="another-official-model"\nmodel_provider="openai"\n',encoding='utf-8')
    assert codex_config_digest(settings)!=first
    assert path.read_text(encoding='utf-8').endswith('profile="official"\n')


def test_profile_file_cannot_silently_select_a_third_party_provider(settings,monkeypatch):
    prepare(settings)
    path=settings.codex_home/'config.toml'
    path.write_text(path.read_text(encoding='utf-8')+'profile="private"\n',encoding='utf-8')
    (settings.codex_home/'private.config.toml').write_text('model_provider="custom"\n',encoding='utf-8')
    monkeypatch.setattr(subprocess,'run',lambda *a,**k:pytest.fail('Do not call a provider'))
    with pytest.raises(JobError) as error:require_chatgpt_login(settings,refresh=True)
    assert error.value.code=='CHATGPT_PROVIDER_REQUIRED'
