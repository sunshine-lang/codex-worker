import importlib.util
import json
from pathlib import Path
import sqlite3
import tomllib
from types import SimpleNamespace

import pytest


@pytest.fixture
def controller(tmp_path,monkeypatch):
    path=Path(__file__).resolve().parents[4]/'packaging/worker8788/settings8788.py'
    spec=importlib.util.spec_from_file_location('settings_test',path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    module.DATA=tmp_path
    module.CONFIG=tmp_path/'config.toml'
    module.TRIAL=tmp_path/'config.trial.json'
    module.CONFIG.write_text('model="before"\nmodel_reasoning_effort="high"\nmodel_provider="openai"\n[windows]\nsandbox="elevated"\n')
    module.TRIAL.write_text(json.dumps({'max_concurrent_jobs':8,'task_batch_concurrency':1,
        'project_context_enabled':True,'port':8788,'codex_home':'unchanged-home'}))
    module.ready=lambda:{'available_models':[{'model':m,'reasoning_efforts':['medium','high']} for m in ('before','after')],
        'ready':True,'configured_model':'before','configured_reasoning_effort':'high','max_concurrent_jobs':8,
        'task_batch_concurrency':1,'project_context_enabled':True,'independent_app_server_processes':8,
        'app_server_generations':{str(i):1 for i in range(8)}}
    module.assert_idle=lambda:None
    module.command=lambda *a,**k:SimpleNamespace(returncode=0,stdout='<Task><Settings><Enabled>true</Enabled></Settings></Task>')
    module.stop_owned=lambda:None
    return module


def test_config_preserves_provider_and_permissions(controller):
    before=controller.CONFIG.read_text()
    after=tomllib.loads(controller.replace_config(before,'after','medium'))
    assert after['model']=='after' and after['model_reasoning_effort']=='medium'
    assert after['model_provider']=='openai' and after['windows']=={'sandbox':'elevated'}
    with pytest.raises(ValueError):controller.replace_config(before,'bad"\nmodel_provider="evil','high')


def test_busy_rejects_without_config_change(controller):
    before=controller.CONFIG.read_bytes()
    def busy():raise ValueError('unfinished')
    controller.assert_idle=busy
    with pytest.raises(ValueError,match='unfinished'):controller.apply('after','medium')
    assert controller.CONFIG.read_bytes()==before
    assert not (controller.DATA/'.maintenance.json').exists()


def test_failed_restart_rolls_back_original(controller):
    before=controller.CONFIG.read_bytes();attempts=[]
    def start(expected):
        attempts.append(expected)
        if len(attempts)==1:raise ValueError('new model failed')
        return {'ready':True}
    controller.start_and_wait=start
    with pytest.raises(ValueError,match='已恢复原配置'):controller.apply('after','medium')
    assert attempts==[('after','medium',8),('before','high',8)]
    assert controller.CONFIG.read_bytes()==before
    assert not (controller.DATA/'.maintenance.json').exists()


def test_successful_model_update(controller):
    controller.start_and_wait=lambda expected:{'ready':True}
    result=controller.apply('after','medium')
    assert result['changed'] and tomllib.loads(controller.CONFIG.read_text())['model']=='after'


def test_unavailable_effort_rejected_before_restart(controller):
    before=controller.CONFIG.read_bytes()
    with pytest.raises(ValueError,match='可用列表'):controller.apply('after','max')
    assert controller.CONFIG.read_bytes()==before


@pytest.mark.parametrize('count',[1,4,8])
def test_channels_preserve_sequential_project_and_model(controller,count):
    model_before=controller.CONFIG.read_bytes()
    controller.start_and_wait=lambda expected:{'ready':True}
    controller.apply('before','high',count)
    config=json.loads(controller.TRIAL.read_text())
    assert config['max_concurrent_jobs']==count and config['task_batch_concurrency']==1
    assert config['project_context_enabled'] is True and config['codex_home']=='unchanged-home'
    assert controller.CONFIG.read_bytes()==model_before


@pytest.mark.parametrize('count',[0,9,True,2.5,'4'])
def test_invalid_channels_do_not_mutate(controller,count):
    before=controller.TRIAL.read_bytes()
    with pytest.raises(ValueError,match='1～8'):controller.apply('before','high',count)
    assert controller.TRIAL.read_bytes()==before


def test_busy_channels_do_not_restart_or_mutate(controller):
    before=controller.TRIAL.read_bytes()
    def busy():raise ValueError('unfinished')
    controller.assert_idle=busy
    controller.stop_owned=lambda:pytest.fail('must not stop a busy service')
    with pytest.raises(ValueError,match='unfinished'):controller.apply('before','high',1)
    assert controller.TRIAL.read_bytes()==before


def test_channels_rollback_restores_both_files(controller):
    model=controller.CONFIG.read_bytes();trial=controller.TRIAL.read_bytes();attempts=[]
    def start(expected):
        attempts.append(expected)
        if len(attempts)==1:raise ValueError('startup failed')
        return {'ready':True}
    controller.start_and_wait=start
    with pytest.raises(ValueError,match='已恢复原配置'):controller.apply('after','medium',1)
    assert controller.CONFIG.read_bytes()==model and controller.TRIAL.read_bytes()==trial
    assert attempts==[('after','medium',1),('before','high',8)]


def test_ready_checks_real_capacity_and_project_order(controller):
    state=controller.ready();assert controller.ready_matches(state,('before','high',8))
    for key,value in [('task_batch_concurrency',8),('independent_app_server_processes',4),
                      ('app_server_generations',{}),('project_context_enabled',False)]:
        assert not controller.ready_matches({**state,key:value},('before','high',8))


def test_unchanged_settings_do_not_restart(controller):
    controller.stop_owned=lambda:pytest.fail('unchanged settings should not restart')
    assert controller.apply('before','high',8)['changed'] is False
