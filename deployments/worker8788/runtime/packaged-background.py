from pathlib import Path
import argparse
import ctypes
from ctypes import wintypes
import json
import os
import sys

ROOT=Path(__file__).resolve().parent
DATA=Path(r'C:\CodexWorkerData-AppServer-8788')
p=argparse.ArgumentParser()
p.add_argument('mode',choices=('identity-probe','protocol-probe','protocol-probe-no-environment','doctor','serve'))
p.add_argument('--operation-id',required=True)
a=p.parse_args()
sys.stdout=(DATA/(a.mode+'.stdout.log')).open('w',encoding='utf-8',buffering=1)
sys.stderr=(DATA/(a.mode+'.stderr.log')).open('w',encoding='utf-8',buffering=1)
sys.path.insert(0,str(ROOT/'src'))
from codex_remote_worker.common import write_json,now
import win32api,win32process
write_json(DATA/(a.mode+'.process.json'),{'operation_id':a.operation_id,'ProcessId':os.getpid(),
    'created_at':win32process.GetProcessTimes(win32api.GetCurrentProcess())['CreationTime'].timestamp(),
    'Executable':sys.executable,'Entrypoint':str(Path(__file__).resolve()),'Mode':a.mode,'StartedAt':now(),
    'StandardOutput':str(DATA/(a.mode+'.stdout.log')),'StandardError':str(DATA/(a.mode+'.stderr.log'))})
code=1
try:
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    n=wintypes.UINT(0)
    result=kernel.GetCurrentPackageFamilyName(ctypes.byref(n),None)
    family=None
    if result==122:
        b=ctypes.create_unicode_buffer(n.value)
        if kernel.GetCurrentPackageFamilyName(ctypes.byref(n),b)==0:
            family=b.value
    if a.mode=='identity-probe':
        import win32security,win32con
        token=win32security.OpenProcessToken(win32api.GetCurrentProcess(),win32con.TOKEN_QUERY)
        groups=win32security.GetTokenInformation(token,win32security.TokenGroups)
        interactive=[attrs for sid,attrs in groups if win32security.ConvertSidToStringSid(sid)=='S-1-5-4']
        session=win32security.GetTokenInformation(token,win32security.TokenSessionId)
        statistics=win32security.GetTokenInformation(token,win32security.TokenStatistics)
        try: logon_type=win32security.LsaGetLogonSessionData(statistics['AuthenticationId']).get('LogonType')
        except Exception: logon_type=None
        token.Close()
        result={'interactive_group_attributes':interactive,'session_id':session,'logon_type':logon_type,'passed':family=='OpenAI.Codex_2p2nqsd0c76g0' and not bool(ctypes.windll.shell32.IsUserAnAdmin()),
            'package_family':family,'administrator':bool(ctypes.windll.shell32.IsUserAnAdmin()),'model_called':False}
        print(json.dumps(result),flush=True)
        code=0 if result['passed'] else 1
    elif family!='OpenAI.Codex_2p2nqsd0c76g0':
        print(json.dumps({'error':{'code':'PACKAGE_IDENTITY_REQUIRED'}}),file=sys.stderr,flush=True)
    else:
        # Official activation supplies OS package identity, but not the parent
        # shell's environment. Select registered-Core routing only after verifying it.
        os.environ['CODEX_WINDOWS_REGISTERED_CORE']='1'
        os.environ['CODEX_WINDOWS_SANDBOX_PACKAGE_FAMILY']=family
        from codex_remote_worker.app_server_service import main
        sys.argv=[str(ROOT/'worker-app-server.py'),'protocol-probe' if a.mode=='protocol-probe-no-environment' else a.mode,'--config',str(ROOT/'config.trial.json')]
        if a.mode=='protocol-probe-no-environment':
            sys.argv.append('--without-environment')
        code=main()
except BaseException as exc:
    print(json.dumps({'error':{'code':'BACKGROUND_EXCEPTION','exception_type':type(exc).__name__}}),file=sys.stderr,flush=True)
finally:
    write_json(DATA/(a.mode+'.exit.json'),{'operation_id':a.operation_id,'exit_code':code,'ended_at':now()})
raise SystemExit(code)
