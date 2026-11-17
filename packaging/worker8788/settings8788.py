"""Local administrator-only configuration controller. Never exposes an HTTP settings endpoint."""
from pathlib import Path
import argparse
import datetime
import json
import re
import sqlite3
import subprocess
import time
import tomllib
import uuid
import os
import sys
sys.path.insert(0,str(Path(__file__).resolve().parent))
from codex_provider import snapshot, to_toml

ROOT=Path(r'C:\CodexRemoteWorker-AppServer-8788')
DATA=Path(r'C:\CodexWorkerData-AppServer-8788')
CONFIG=DATA/'private/codex-home/config.toml'
TRIAL=ROOT/'config.trial.json'
TASK='CodexWorker8788-Recovery'
SOURCE_HOME=Path(r'C:\Users\Administrator\.codex')
TERMINAL={'completed','needs_review','failed','cancelled','interrupted'}


def replace_config(text,model,effort):
    if not re.fullmatch(r'[A-Za-z0-9._/:-]{1,200}',model) or effort not in ('default','none','minimal','low','medium','high','xhigh','max','ultra'):
        raise ValueError('模型或思考等级无效。')
    header,sep,rest=text.partition('\n[')
    for name,value in [('model',model),('model_reasoning_effort',effort)]:
        pattern=r'(?m)^"?'+name+r'"?\s*=.*$'
        if name=='model_reasoning_effort' and value=='default':
            header=re.sub(pattern,'',header)
            continue
        replacement=name+' = '+json.dumps(value)
        if re.search(pattern,header):header=re.sub(pattern,replacement,header)
        else:header+='\n'+replacement
    updated=header+sep+rest
    before,after=tomllib.loads(text),tomllib.loads(updated)
    assert {k:v for k,v in before.items() if k not in ('model','model_reasoning_effort')}=={k:v for k,v in after.items() if k not in ('model','model_reasoning_effort')}
    return updated


def assert_idle(data=DATA):
    for table in ('tasks','jobs'):
        with sqlite3.connect((data/(table+'.sqlite3')).as_uri()+'?mode=ro',uri=True) as db:
            if any(n and status not in TERMINAL for status,n in db.execute('select status,count(*) from '+table+' group by status')):
                raise ValueError('当前仍有未完成任务。请等待整单结束后再应用；本次未更改配置。')


def replace_trial(text,channels):
    if type(channels) is not int or not 1<=channels<=8:
        raise ValueError('执行通道数量必须是 1～8 的整数。')
    config=json.loads(text)
    if config.get('project_context_enabled') is not True:
        raise ValueError('此设置仅适用于 8788 项目顺序翻译版本。')
    config.update(max_concurrent_jobs=channels,task_batch_concurrency=1)
    return json.dumps(config,ensure_ascii=False,indent=2)+'\n'


def ready_matches(state,expected):
    return bool(state.get('ready') and
        (state.get('configured_model'),state.get('configured_reasoning_effort'),state.get('max_concurrent_jobs'))==expected
        and state.get('task_batch_concurrency')==1 and state.get('project_context_enabled') is True
        and state.get('independent_app_server_processes')==expected[2]
        and len(state.get('app_server_generations',{}))==expected[2])


def key():
    config=json.loads(Path(r'C:\CodexRemoteWorker\config.local.json').read_text(encoding='utf-8-sig'))
    return json.loads((Path(config['data_root'])/'private/credentials.local.json').read_text(encoding='utf-8-sig'))['CODEX_WORKER_SERVICE_TOKEN']


def ready():
    import httpx
    with httpx.Client(trust_env=False,timeout=5) as c:
        r=c.get('http://127.0.0.1:8788/v1/ready',headers={'Authorization':'Bearer '+key()});r.raise_for_status();return r.json()


def stop_owned():
    import win32api,win32event,win32process
    record=json.loads((DATA/'serve.process.json').read_text(encoding='utf-8'))
    if record['Entrypoint']!=str(ROOT/'packaged-background.py') or record['Mode']!='serve':
        raise ValueError('服务进程归属不匹配，未停止进程。')
    try:handle=win32api.OpenProcess(0x1000|1|0x100000,False,record['ProcessId'])
    except win32api.error as exc:
        if exc.winerror!=87:raise
        handle=None
    if handle is not None:
        try:
            if abs(win32process.GetProcessTimes(handle)['CreationTime'].timestamp()-record['created_at'])<=.001:
                win32api.TerminateProcess(handle,0)
                if win32event.WaitForSingleObject(handle,10000)!=0:raise ValueError('服务进程未退出。')
            # A stale receipt does not authorize stopping a different process.
        finally:handle.Close()
    import sys
    sys.path.insert(0,str(ROOT/'src'))
    from codex_remote_worker.windows import reconcile_process
    for p in (DATA/'app-server').glob('slot-*/process.json'):
        meta=json.loads(p.read_text(encoding='utf-8'))['process']
        if not meta['job_object'].startswith('Local\\CodexWorkerAppServer8788-') or not reconcile_process(meta):
            raise ValueError('无法确认 8788 执行通道已退出。')


def command(args,timeout=200):
    return subprocess.run(args,capture_output=True,text=True,encoding='utf-8',errors='replace',
        timeout=timeout,creationflags=subprocess.CREATE_NO_WINDOW)


def start_and_wait(expected):
    runtime=json.loads((ROOT/'desktop/runtime.json').read_text(encoding='utf-8-sig'))
    result=command([runtime['pwsh'],'-NoProfile','-NonInteractive','-File',str(ROOT/'desktop/start-worker.ps1'),'-Mode','Serve'])
    if result.returncode:raise ValueError('服务启动失败，请检查启动日志。')
    until=time.monotonic()+90
    while time.monotonic()<until:
        try:
            state=ready()
            if ready_matches(state,expected):return state
        except Exception:pass
        time.sleep(1)
    raise ValueError('新配置未通过就绪检查。')


def current_source():
    fallback=tomllib.loads(CONFIG.read_text(encoding='utf-8-sig')) if CONFIG.exists() else {}
    return snapshot(SOURCE_HOME,fallback)


def describe():
    source=current_source()
    trial=json.loads(TRIAL.read_text(encoding='utf-8-sig'))
    return {**source['public'],'max_concurrent_jobs':trial['max_concurrent_jobs']}


def apply(model,effort,channels=None,source_signature=None):
    from xml.etree import ElementTree as ET
    source=current_source()
    if source_signature is not None and source_signature!=source['public']['source_signature']:
        raise ValueError('Codex 供应商或模型目录已变化，请刷新后重新应用。')
    try:state=ready()
    except Exception:state={}
    allowed={m['model']:m['reasoning_efforts'] for m in source['public']['available_models']}
    if model not in allowed or effort not in allowed[model]:
        raise ValueError('所选模型或思考等级不在当前账户可用列表中。')
    original_bytes=CONFIG.read_bytes();original=original_bytes.decode('utf-8-sig');old=tomllib.loads(original)
    trial_bytes=TRIAL.read_bytes();trial_text=trial_bytes.decode('utf-8-sig');old_trial=json.loads(trial_text)
    if channels is None:channels=old_trial['max_concurrent_jobs']
    trial_replacement=replace_trial(trial_text,channels)
    expected=(model,effort,channels)
    previous=(old.get('model'),old.get('model_reasoning_effort','default'),old_trial['max_concurrent_jobs'])
    provider=source['public']['configured_provider']
    replacement=replace_config(to_toml(source['config']),model,effort)
    replacement+='\n[windows]\nsandbox="elevated"\n'
    secret_files={CONFIG.parent/'auth.json':json.dumps(source['auth']),
                  CONFIG.parent/'provider-env.json':json.dumps(source['env']),
                  CONFIG.parent/'worker-models.json':json.dumps(source['public'],ensure_ascii=False)}
    if (tomllib.loads(original)==tomllib.loads(replacement) and previous==expected and old_trial.get('authentication_mode')=='codex_auto'
        and all(p.exists() and p.read_text(encoding='utf-8')==v for p,v in secret_files.items())
        and ready_matches(state,expected) and state.get('configured_provider')==provider):
        return {'changed':False,'message':'所选配置已生效。'}
    marker=DATA/'.maintenance.json';operation=uuid.uuid4().hex
    with marker.open('x',encoding='utf-8') as f:json.dump({'operation':operation,'reason':'desktop worker settings'},f)
    disabled=False;changed=False;secret_original={p:p.read_bytes() if p.exists() else None for p in secret_files}
    def release():
        if marker.exists() and json.loads(marker.read_text())['operation']==operation:marker.unlink()
    try:
        assert_idle()
        query=command(['schtasks','/Query','/TN',TASK,'/XML'],30)
        if query.returncode:raise ValueError('无法读取自动恢复任务状态。')
        enabled=ET.fromstring(query.stdout).find('.//{*}Settings/{*}Enabled')
        if enabled is None or enabled.text!='false':
            if command(['schtasks','/Change','/TN',TASK,'/DISABLE'],30).returncode:raise ValueError('无法暂停自动恢复任务。')
            disabled=True
        backup=DATA/'maintenance-backups'/('settings-'+datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S')+'-'+operation[:8])
        backup.mkdir(parents=True);(backup/'config.toml').write_bytes(original_bytes)
        (backup/'config.trial.json').write_bytes(trial_bytes)
        for p,v in secret_original.items():
            if v is not None:(backup/p.name).write_bytes(v)
        assert_idle();stop_owned();changed=True
        CONFIG.write_text(replacement,encoding='utf-8')
        for p,v in secret_files.items():p.write_text(v,encoding='utf-8')
        updated_trial=json.loads(trial_replacement);updated_trial['authentication_mode']='codex_auto'
        TRIAL.write_text(json.dumps(updated_trial,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        release()
        state=start_and_wait(expected)
        if state.get('configured_provider')!=provider:raise ValueError('启动后的供应商与当前 Codex 不一致。')
        return {'changed':True,'message':f'配置已生效：全局 {channels} 路，同一整单顺序处理；8788 已重启并就绪。',
                'model':model,'effort':effort,'channels':channels,'task_batch_concurrency':1}
    except Exception:
        if changed:
            try:
                stop_owned();CONFIG.write_bytes(original_bytes);TRIAL.write_bytes(trial_bytes)
                for p,v in secret_original.items():
                    if v is None:p.unlink(missing_ok=True)
                    else:p.write_bytes(v)
                release()
                start_and_wait(previous)
            except Exception:
                raise ValueError('新配置启动失败，自动恢复未完成，请检查日志。') from None
            raise ValueError('新配置未通过验证，已恢复原配置并启动服务。') from None
        raise
    finally:
        release()
        if disabled:command(['schtasks','/Change','/TN',TASK,'/ENABLE'],30)


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--model');parser.add_argument('--effort')
    parser.add_argument('--describe',action='store_true');parser.add_argument('--source-signature')
    parser.add_argument('--channels',type=int)
    args=parser.parse_args()
    try:
        if args.describe:result=describe()
        else:result=apply(args.model,args.effort,args.channels,args.source_signature)
        print(json.dumps({'ok':True,**result},ensure_ascii=False))
    except Exception as exc:
        # Never print credentials, configuration contents or unfiltered OS exceptions.
        message=str(exc) if isinstance(exc,ValueError) else '设置未应用，请检查服务和维护状态。'
        print(json.dumps({'ok':False,'message':message},ensure_ascii=False))
        raise SystemExit(1)
