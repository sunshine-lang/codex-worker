"""Local post-deployment checks; the synthetic submit cannot become a job."""
import json
from pathlib import Path
import sqlite3
import subprocess
import urllib.error
import urllib.request
import uuid


root = Path(r'C:\CodexRemoteWorker')
config = json.loads((root / 'config.local.json').read_text(encoding='utf-8-sig'))
data_root = Path(config['data_root'])
credentials = json.loads((data_root / 'private' / 'credentials.local.json').read_text(encoding='utf-8-sig'))
token = credentials['CODEX_WORKER_SERVICE_TOKEN']
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
base = f'http://127.0.0.1:{config["port"]}'


def request(path, body=None, content_type=None):
    headers = {'Authorization': 'Bearer ' + token}
    if content_type:
        headers.update({'Content-Type': content_type, 'Idempotency-Key': str(uuid.uuid4())})
    req = urllib.request.Request(base + path, body, headers)
    try:
        response = opener.open(req, timeout=15)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        return response.status, json.load(response)


def count_jobs():
    with sqlite3.connect((data_root / 'jobs.sqlite3').as_uri() + '?mode=ro', uri=True) as db:
        return db.execute('select count(*) from jobs').fetchone()[0]


def launcher_probe(name):
    report = Path(__file__).resolve().parents[1] / 'build' / 'sandbox-fix' / (name + '.json')
    result = subprocess.run([str(root / 'bin' / 'CodexWorkerLauncher.exe'),
                             '--root', str(root), '--probe-to', str(report)],
                            timeout=25, creationflags=subprocess.CREATE_NO_WINDOW)
    value = json.loads(report.read_text(encoding='utf-8-sig'))
    return result.returncode, value['ready'], value['code']


receipt_path = data_root / 'preflight.json'
original = receipt_path.read_bytes()
receipt = json.loads(original)
before = count_jobs()
results = {}
status, value = request('/v1/ready')
assert status == 200 and value['ready'] is True, (status, value)
results['ready_before'] = {'http': status, 'ready': value['ready']}
try:
    # Simulate a changed receipt, without touching any account or password.
    receipt['sandbox_state']['accounts'][0]['password_set_at'] -= 600
    changed = receipt_path.with_suffix('.verification.tmp')
    changed.write_text(json.dumps(receipt), encoding='utf-8')
    changed.replace(receipt_path)
    status, value = request('/v1/ready')
    assert status == 503 and value['code'] == 'SANDBOX_STATE_CHANGED', (status, value)
    results['stale_state_rejected'] = {'http': status, 'code': value['code']}
    result, ready, code = launcher_probe('launcher-stale')
    assert result == 1 and ready is False and code == 'SANDBOX_STATE_CHANGED', (result, ready, code)
    results['launcher_stale'] = {'exit': result, 'ready': ready, 'code': code}
    boundary = 'WorkerRegression' + uuid.uuid4().hex
    parts = []
    # Intentionally invalid source table also prevents job creation if the gate regresses.
    for role, filename, text in [
        ('source_file', 'invalid.csv', 'no_valid_header\n'),
        ('prompt_file', 'prompt.md', 'Synthetic readiness rejection check.'),
        ('project_style', 'project.md', 'fixture'),
        ('general_style', 'general.md', 'fixture')]:
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{role}"; filename="{filename}"\r\nContent-Type: text/plain\r\n\r\n{text}\r\n')
    parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="options"\r\n\r\n{{"timeout_seconds":60}}\r\n--{boundary}--\r\n')
    status, value = request('/v1/jobs', ''.join(parts).encode(), 'multipart/form-data; boundary=' + boundary)
    assert status == 503 and value['error']['code'] == 'NOT_READY', (status, value)
    results['submit_rejected_before_execution'] = {'http': status, 'code': value['error']['code']}
finally:
    restore = receipt_path.with_suffix('.restore.tmp')
    restore.write_bytes(original)
    restore.replace(receipt_path)
status, value = request('/v1/ready')
assert status == 200 and value['ready'] is True, (status, value)
results['ready_restored'] = {'http': status, 'ready': value['ready']}
result, ready, code = launcher_probe('launcher-ready')
assert result == 0 and ready is True, (result, ready, code)
results['launcher_ready'] = {'exit': result, 'ready': ready, 'code': code}
after = count_jobs()
assert before == after, (before, after)
results['jobs_created'] = after - before
report = Path(__file__).resolve().parents[1] / 'build' / 'sandbox-fix' / 'verification.json'
report.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding='utf-8')
print(json.dumps(results, ensure_ascii=False))
