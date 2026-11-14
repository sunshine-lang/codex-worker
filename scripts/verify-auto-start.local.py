"""Exercise the same launcher path as a double-click, without model calls."""
import argparse
import datetime as dt
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import time
import urllib.error
import urllib.request

parser = argparse.ArgumentParser()
parser.add_argument('--reopen-only', action='store_true')
parser.add_argument('--background-recovery', action='store_true')
args = parser.parse_args()
project = Path(__file__).resolve().parents[1]
reports = project / 'build' / 'auto-start'
reports.mkdir(exist_ok=True, parents=True)
root = Path(r'C:\CodexRemoteWorker')
launcher = root / 'bin' / 'CodexWorkerLauncher.exe'
config = json.loads((root / 'config.local.json').read_text(encoding='utf-8-sig'))
data = Path(config['data_root'])
token = json.loads((data / 'private' / 'credentials.local.json').read_text(encoding='utf-8-sig'))['CODEX_WORKER_SERVICE_TOKEN']
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
status_path = root / 'desktop-status.json'


def read(path):
    return json.loads(path.read_text(encoding='utf-8-sig'))


def ready():
    req = urllib.request.Request(f'http://127.0.0.1:{config["port"]}/v1/ready',
                                 headers={'Authorization': 'Bearer ' + token})
    try:
        response = opener.open(req, timeout=20)
    except urllib.error.HTTPError as error:
        response = error
    except OSError:
        return False
    with response:
        value = json.load(response)
        return response.status == 200 and value.get('ready') is True


def job_count():
    with sqlite3.connect((data / 'jobs.sqlite3').as_uri() + '?mode=ro', uri=True) as db:
        assert db.execute("select count(*) from jobs where status in ('queued','running','checking','cancelling')").fetchone()[0] == 0
        return db.execute('select count(*) from jobs').fetchone()[0]


def ensure(name):
    path = reports / (name + '.json')
    start = time.monotonic()
    result = subprocess.run([str(launcher), '--root', str(root), '--ensure-to', str(path)],
                            timeout=240, creationflags=subprocess.CREATE_NO_WINDOW)
    value = read(path)
    assert result.returncode == 0 and value.get('state') == 'ready', value
    assert value.get('operation') == 'ensure' and value.get('operation_id'), value
    assert ready()
    return {'case': name, 'pid': value['process_id'], 'code': value['code'],
            'operation_id': value['operation_id'], 'seconds': round(time.monotonic() - start, 2)}


def corrupt_receipt():
    path = data / 'preflight.json'
    original = path.read_bytes()
    changed = json.loads(original)
    changed['sandbox_state']['accounts'][0]['password_set_at'] -= 600
    payload = json.dumps(changed).encode()
    temporary = path.with_suffix('.auto-test.tmp')
    temporary.write_bytes(payload)
    temporary.replace(path)
    return path, original, payload


def restore_if_still_synthetic(path, original, payload):
    if path.read_bytes() == payload:
        temporary = path.with_suffix('.auto-restore.tmp')
        temporary.write_bytes(original)
        temporary.replace(path)


before = job_count()
cases = []
if args.reopen_only:
    old = read(status_path)
    subprocess.run([str(Path(r'C:\Users\Administrator\Desktop\Codex 服务启动器.exe'))],
                   timeout=10, creationflags=subprocess.CREATE_NO_WINDOW, check=True)
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        current = read(status_path)
        if current.get('operation_id') != old.get('operation_id') and current.get('state') == 'ready':
            assert current['process_id'] == old['process_id'], (old, current)
            cases.append({'case': 'repeat_double_click_existing_tray', 'pid': current['process_id'],
                          'code': current['code'], 'operation_id': current['operation_id']})
            break
        time.sleep(1)
    else:
        raise AssertionError('Second double-click did not perform a new successful check')
elif args.background_recovery:
    old = read(status_path)
    path, original, payload = corrupt_receipt()
    try:
        assert not ready()
        deadline = time.monotonic() + 150
        while time.monotonic() < deadline:
            current = read(status_path)
            if current.get('code') == 'READY_REPAIRED' and current.get('operation_id') != old.get('operation_id') and ready():
                assert current['process_id'] != old['process_id']
                cases.append({'case': 'tray_automatic_background_recovery', 'pid': current['process_id'],
                              'code': current['code'], 'operation_id': current['operation_id']})
                break
            time.sleep(2)
        else:
            raise AssertionError('Tray failed to recover the idle service automatically')
    finally:
        restore_if_still_synthetic(path, original, payload)
else:
    cases.append(ensure('cold_start'))
    first_pid = cases[-1]['pid']
    receipt = (data / 'preflight.json').read_bytes()
    for index in range(1, 4):
        result = ensure('healthy_repeat_' + str(index))
        assert result['pid'] == first_pid, result
        assert (data / 'preflight.json').read_bytes() == receipt, 'Live probe rewrote receipt'
        probe = read(data / 'worker.local.probe.stdout.log')
        assert probe['passed'] and probe['probe_only'] and probe['model_called'] is False
        cases.append(result)
    parallel = []
    for index in range(2):
        path = reports / f'concurrent_{index}.json'
        process = subprocess.Popen([str(launcher), '--root', str(root), '--ensure-to', str(path)],
                                   creationflags=subprocess.CREATE_NO_WINDOW)
        parallel.append((process, path))
    for process, path in parallel:
        assert process.wait(timeout=150) == 0
        value = read(path)
        assert value['state'] == 'ready' and value['process_id'] == first_pid
        cases.append({'case': path.stem, 'pid': value['process_id'], 'code': value['code'],
                      'operation_id': value['operation_id']})
    assert len({case['operation_id'] for case in cases}) == len(cases), cases
    path, original, payload = corrupt_receipt()
    try:
        assert not ready()
        result = ensure('automatic_stale_repair')
        assert result['code'] == 'READY_REPAIRED' and result['pid'] != first_pid, result
        cases.append(result)
    finally:
        restore_if_still_synthetic(path, original, payload)
    # A separate fixture proves running jobs are preserved without touching production jobs.
    fixture = reports / ('active-fixture-' + dt.datetime.now().strftime('%H%M%S'))
    (fixture / 'scripts').mkdir(parents=True)
    fixture_data = fixture / 'data'
    (fixture_data / 'private').mkdir(parents=True)
    (fixture_data / 'private' / 'credentials.local.json').write_text(json.dumps({'CODEX_WORKER_SERVICE_TOKEN': 'fixture-token'}))
    (fixture / 'config.local.json').write_text(json.dumps({'data_root': str(fixture_data), 'tool_python': config['tool_python'], 'port': 1}))
    shutil.copyfile(root / 'scripts' / 'desktop-ensure.ps1', fixture / 'scripts' / 'desktop-ensure.ps1')
    with sqlite3.connect(fixture_data / 'jobs.sqlite3') as db:
        db.execute('create table jobs(status text)')
        db.execute('insert into jobs values(?)', ('running',))
    result = subprocess.run([str(root / '.runtime' / 'powershell' / 'pwsh.exe'), '-NoLogo', '-NoProfile',
                             '-NonInteractive', '-File', str(fixture / 'scripts' / 'desktop-ensure.ps1')],
                            timeout=20, creationflags=subprocess.CREATE_NO_WINDOW)
    value = read(fixture / 'desktop-status.json')
    assert result.returncode == 1 and value['code'] == 'ACTIVE_JOBS', value
    cases.append({'case': 'running_job_preserved', 'code': value['code']})
assert job_count() == before, 'Verification created a translation job'
assert ready()
name = 'reopen' if args.reopen_only else ('background' if args.background_recovery else 'pipeline')
report = {'checked_at': dt.datetime.now(dt.timezone.utc).isoformat(), 'cases': cases, 'jobs_created': 0, 'model_called': False}
(reports / (name + '-verification.json')).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
print(json.dumps(report, ensure_ascii=False))
