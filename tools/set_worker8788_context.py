"""Set the idle 8788 service's Codex context budget, preserving all other settings."""
import hashlib
import json
from pathlib import Path
import re
import sys
import tomllib
import uuid
from xml.etree import ElementTree as ET

ROOT=Path(r'C:\CodexRemoteWorker-AppServer-8788')
DATA=Path(r'C:\CodexWorkerData-AppServer-8788')
OUT=Path(__file__).resolve().parents[1]/'build/worker8788-context'
OUT.mkdir(parents=True,exist_ok=True)
sys.path.insert(0,str(ROOT/'desktop'))
import settings8788 as service
sys.path.insert(0,str(ROOT/'src'))
from codex_remote_worker.common import atomic_write

changes={'model_context_window':872000,'model_auto_compact_token_limit':784800}
assert changes['model_auto_compact_token_limit']*10==changes['model_context_window']*9
service.assert_idle()
initial=service.ready()
assert initial['ready'] and initial['active_execution_slots']==0
assert initial['configured_provider']=='openai'
assert initial['configured_model']=='gpt-6.1-sol' and initial['configured_reasoning_effort']=='medium'
expected=(initial['configured_model'],initial['configured_reasoning_effort'],initial['max_concurrent_jobs'])
original=service.CONFIG.read_bytes()
old=tomllib.loads(original.decode('utf-8-sig'))
assert not old.get('profile'), 'An active profile requires a profile-specific edit'
assert old.get('model_auto_compact_token_limit_scope','total')=='total'
catalog=json.loads((service.CONFIG.parent/'models_cache.json').read_text(encoding='utf-8'))
model=next(m for m in catalog['models'] if m.get('slug')==expected[0])
assert model['max_context_window']>=changes['model_context_window']
header,separator,rest=original.decode('utf-8-sig').partition('\n[')
for key,value in changes.items():
    pattern=r'(?m)^"?'+re.escape(key)+r'"?\s*=.*$'
    replacement=key+' = '+str(value)
    if re.search(pattern,header):header=re.sub(pattern,replacement,header)
    else:header=header.rstrip()+'\n'+replacement+'\n'
replacement=header+separator+rest
new=tomllib.loads(replacement)
assert all(new[k]==v for k,v in changes.items())
assert {k:v for k,v in old.items() if k not in changes}=={k:v for k,v in new.items() if k not in changes}
protected=[ROOT/'config.trial.json',Path(r'C:\Users\Administrator\.codex\config.toml'),
           Path(r'C:\CodexRemoteWorker\config.local.json')]
fingerprints={str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in protected}
query=service.command(['schtasks','/Query','/TN',service.TASK,'/XML'])
assert query.returncode==0
enabled=ET.fromstring(query.stdout).find('.//{*}Settings/{*}Enabled')
was_enabled=enabled is None or enabled.text!='false'
operation=uuid.uuid4().hex
marker=DATA/'.maintenance.json'
with marker.open('x',encoding='utf-8') as stream:
    json.dump({'operation':operation,'reason':'Set 8788 context to 872000 and auto-compaction to 784800'},stream)

def release():
    if marker.exists() and json.loads(marker.read_text(encoding='utf-8')).get('operation')==operation:
        marker.unlink()

stopped=False
try:
    service.assert_idle()
    backup=DATA/'maintenance-backups'/('context-872000-'+operation)
    backup.mkdir(parents=True)
    (backup/'config.toml').write_bytes(original)
    if was_enabled:
        assert service.command(['schtasks','/Change','/TN',service.TASK,'/DISABLE']).returncode==0
    service.assert_idle()
    service.stop_owned();stopped=True
    atomic_write(service.CONFIG,replacement.encode('utf-8'))
    release()
    state=service.start_and_wait(expected)
    current=tomllib.loads(service.CONFIG.read_text(encoding='utf-8'))
    assert all(current[k]==v for k,v in changes.items())
    assert state['configured_provider']=='openai' and state['project_controller_version']=='ledger/2'
    assert fingerprints=={str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in protected}
    report={**changes,'compaction_percent_of_configured_window':90,'compaction_scope':'total',
        'model':state['configured_model'],'reasoning_effort':state['configured_reasoning_effort'],
        'provider':state['configured_provider'],'ready':state['ready'],
        'active_execution_slots':state['active_execution_slots'],'controller':state['project_controller_version'],
        'config_path':str(service.CONFIG),'backup':str(backup),'other_configurations_unchanged':True}
    (OUT/'context-872000-verification.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    print(json.dumps(report),flush=True)
except BaseException:
    if stopped:
        service.assert_idle()
        service.stop_owned()
        atomic_write(service.CONFIG,original)
        release()
        service.start_and_wait(expected)
    raise
finally:
    release()
    if was_enabled:
        assert service.command(['schtasks','/Change','/TN',service.TASK,'/ENABLE']).returncode==0
