"""Local 8789 service status and maintenance helpers."""
import argparse
import json
from pathlib import Path
import sqlite3

ROOT=Path(r'C:\CodexRemoteWorker-AppServer-8789')
DATA=Path(r'C:\CodexWorkerData-AppServer-8789')
TERMINAL={'completed','needs_review','failed','cancelled','interrupted'}


def assert_idle(data=DATA):
    for table in ('tasks','jobs'):
        with sqlite3.connect((data/(table+'.sqlite3')).as_uri()+'?mode=ro',uri=True) as db:
            if any(n and status not in TERMINAL for status,n in db.execute('select status,count(*) from '+table+' group by status')):
                raise ValueError('当前仍有未完成任务。请等待整单结束后再应用；本次未更改配置。')


def key():
    return json.loads((DATA/'private/credentials.local.json').read_text(encoding='utf-8-sig'))['CODEX_WORKER_SERVICE_TOKEN']


def ready():
    import httpx
    with httpx.Client(trust_env=False,timeout=5) as c:
        r=c.get('http://127.0.0.1:8789/v1/ready',headers={'Authorization':'Bearer '+key()});r.raise_for_status();return r.json()


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
        if not meta['job_object'].startswith('Local\\CodexWorkerAppServer8789-') or not reconcile_process(meta):
            raise ValueError('无法确认 8789 执行通道已退出。')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Show the active settings of Worker 8789.')
    parser.add_argument('--describe', action='store_true', help='Show live service status.')
    parser.parse_args()
    state = ready()
    print(json.dumps({k: state.get(k) for k in ('ready', 'configured_provider', 'configured_model',
                    'configured_reasoning_effort', 'active_execution_slots', 'max_concurrent_jobs')},
                    ensure_ascii=False))
