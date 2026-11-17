import asyncio
import importlib.util
import json
from pathlib import Path
import sys
import tomllib
from types import SimpleNamespace

import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[4]/'packaging/worker8788'))
import codex_provider as provider
import settings8788 as controller
from codex_remote_worker.app_server_runner import AppServerRunner, thread_parameters
from codex_remote_worker.subscription import require_configured_login
from codex_remote_worker.common import JobError
from test_app_server_adapter import Rpc


def home(tmp_path, custom=False):
    h=tmp_path/'home';h.mkdir()
    c={'model_provider':'custom' if custom else 'openai','model':'vendor/model' if custom else 'official','model_reasoning_effort':'high'}
    if custom:c['model_providers']={'custom':{'name':'OpenRouter','base_url':'https://openrouter.ai/api/v1','wire_api':'responses','requires_openai_auth':True}}
    (h/'config.toml').write_text(provider.to_toml(c),encoding='utf-8')
    (h/'auth.json').write_text(json.dumps({'OPENAI_API_KEY':'synthetic-private-key'}))
    (h/'models_cache.json').write_text(json.dumps({'models':[{'slug':'official','supported_reasoning_levels':[{'effort':'low'},{'effort':'high'}]}]}))
    return h


def test_official_catalog_and_no_credentials_in_public(tmp_path):
    h=home(tmp_path);s=provider.snapshot(h)
    assert s['public']['configured_provider']=='openai'
    assert s['public']['available_models'][0]['reasoning_efforts']==['low','high']
    assert 'synthetic-private-key' not in json.dumps(s['public'])
    assert tomllib.loads(provider.to_toml(s['config']))['model']=='official'


def test_custom_models_and_exact_efforts(tmp_path):
    h=home(tmp_path,True)
    data=[{'id':'vendor/model','supported_parameters':['tools','reasoning'],'reasoning':{'supported_efforts':['none','high','max']}},
          {'id':'other/model','supported_parameters':['tools']}, {'id':'no/tools','supported_parameters':[]}]
    s=provider.snapshot(h,fetch=lambda:data)
    assert s['public']['configured_provider']=='custom'
    assert [m['model'] for m in s['public']['available_models']]==['vendor/model','other/model']
    assert s['public']['available_models'][0]['reasoning_efforts']==['none','high','max']
    assert s['public']['available_models'][1]['reasoning_efforts']==['default']
    assert s['auth']['OPENAI_API_KEY']=='synthetic-private-key'


def test_profile_and_catalog_are_resolved_without_switcher(tmp_path):
    h=home(tmp_path)
    (h/'config.toml').write_text('profile="work"\n[profiles.work]\nmodel_provider="custom"\nmodel="vendor/model"\nmodel_catalog_json="catalog.json"\n[model_providers.custom]\nbase_url="https://example.test/v1"\n')
    (h/'catalog.json').write_text(json.dumps({'models':[{'slug':'vendor/model','supported_reasoning_levels':[{'effort':'low'}]}]}))
    assert provider.snapshot(h)['public']['configured_provider']=='custom'
    assert provider.snapshot(h)['public']['configured_reasoning_effort']=='low'


def test_default_effort_removes_override_and_colon_model_allowed():
    text=controller.replace_config('model="old"\nmodel_reasoning_effort="high"\n','vendor/model:free','default')
    assert 'model_reasoning_effort' not in tomllib.loads(text)


@pytest.fixture
def control(tmp_path,monkeypatch):
    h=home(tmp_path)
    monkeypatch.setattr(controller,'DATA',tmp_path)
    monkeypatch.setattr(controller,'CONFIG',h/'config.toml')
    monkeypatch.setattr(controller,'TRIAL',tmp_path/'trial.json')
    controller.TRIAL.write_text(json.dumps({'max_concurrent_jobs':1,'task_batch_concurrency':1,'project_context_enabled':True,'authentication_mode':'chatgpt'}))
    monkeypatch.setattr(controller,'SOURCE_HOME',h)
    monkeypatch.setattr(controller,'assert_idle',lambda:None)
    monkeypatch.setattr(controller,'ready',lambda: (_ for _ in ()).throw(ConnectionError()))
    monkeypatch.setattr(controller,'stop_owned',lambda:None)
    monkeypatch.setattr(controller,'command',lambda *a,**kw:SimpleNamespace(returncode=0,stdout='<Task><Settings><Enabled>true</Enabled></Settings></Task>'))
    monkeypatch.setattr(controller,'start_and_wait',lambda exp:{'configured_provider':'openai'})
    return controller


def test_offline_service_can_be_recovered(control):
    s=control.describe()
    assert control.apply('official','low',1,s['source_signature'])['changed']
    assert tomllib.loads(control.CONFIG.read_text())['model_reasoning_effort']=='low'
    assert json.loads(control.TRIAL.read_text())['authentication_mode']=='codex_auto'


def test_provider_change_after_display_rejects_before_writes(control):
    b=control.CONFIG.read_bytes()
    with pytest.raises(ValueError,match='变化'):control.apply('official','high',1,'stale-signature')
    assert control.CONFIG.read_bytes()==b


def test_restart_failure_restores_credentials_and_both_configs(control):
    before={p:p.read_bytes() for p in [control.CONFIG,control.CONFIG.parent/'auth.json',control.TRIAL]}
    calls=[]
    def start(exp):
        calls.append(exp)
        if len(calls)==1:raise ValueError('failed')
    control.start_and_wait=start
    try:
        with pytest.raises(ValueError,match='已恢复原配置'):control.apply('official','low',1)
    finally:control.start_and_wait=lambda exp:{'configured_provider':'openai'}
    assert all(p.read_bytes()==b for p,b in before.items())
    assert not (control.CONFIG.parent/'provider-env.json').exists()


def test_busy_service_does_not_mutate(control,monkeypatch):
    b=control.CONFIG.read_bytes()
    monkeypatch.setattr(control,'assert_idle',lambda: (_ for _ in ()).throw(ValueError('busy')))
    with pytest.raises(ValueError,match='busy'):control.apply('official','low',1)
    assert control.CONFIG.read_bytes()==b


def test_custom_auth_and_thread_routing(settings,tmp_path):
    settings.authentication_mode='codex_auto'
    (settings.codex_home/'auth.json').write_text('{"OPENAI_API_KEY":"test-private"}')
    assert require_configured_login(settings)['passed']
    root=tmp_path/'run';(root/'work').mkdir(parents=True)
    assert thread_parameters(settings,root)['config']['model_provider']=='fixture'


@pytest.mark.parametrize('pages,good', [([{'data':[]}],True),([{'data':['owned']}],False),
    ([{}],False),([{'data':[],'nextCursor':'page2'},{'data':['owned']}],False),
    ([{'data':['foreign'],'nextCursor':'page2'},{'data':[]}],True)])
def test_loaded_list_fallback_requires_complete_absence(settings,pages,good):
    async def exercise():
        runner=AppServerRunner(settings);rpc=Rpc();q=rpc.subscribe('owned');results=iter(pages)
        async def request(method,params,**kwargs):
            if method=='thread/unsubscribe':return {'status':'unsubscribed'}
            assert method=='thread/loaded/list'
            return next(results)
        rpc.request=request
        assert await runner._unload_thread(rpc,'owned',q,timeout=.001) is good
    asyncio.run(exercise())
