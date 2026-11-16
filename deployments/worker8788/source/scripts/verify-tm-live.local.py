"""Activate the validated deployment and submit one explicit synthetic zero-model job."""
from pathlib import Path
import hashlib
import json
import time
from urllib.request import Request, ProxyHandler, build_opener
import uuid

ROOT = Path(__file__).resolve().parents[1]
DATA = Path(r'C:\CodexWorkerData')
state_path = ROOT / 'build/tm-0.4.0/upgrade-state.json'
state = json.loads(state_path.read_text(encoding='utf-8'))
native = json.loads((DATA / 'tm04-native-verification.json').read_text(encoding='utf-8-sig'))
assert native['passed'] and native['sandbox']['passed'] and 'siemens-json/2' in native['json']['input_contracts']
for path, expected in state['protected_hashes'].items():
    if not path.endswith('CodexRemoteWorker.exe'):
        assert hashlib.sha256(Path(path).read_bytes()).hexdigest() == expected
marker = DATA / '.maintenance.json'
assert json.loads(marker.read_text(encoding='utf-8-sig'))['operation_id'] == state['operation_id']
marker.unlink()
state['native_checks_passed'] = True
state['maintenance_released_at'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
state_path.write_text(json.dumps(state, indent=2), encoding='utf-8')
config = json.loads(Path(r'C:\CodexRemoteWorker\config.local.json').read_text(encoding='utf-8-sig'))
token = json.loads((DATA / 'private/credentials.local.json').read_text(encoding='utf-8-sig'))['CODEX_WORKER_SERVICE_TOKEN']
base = 'http://127.0.0.1:' + str(config['port'])
opener = build_opener(ProxyHandler({}))


def request(path, data=None, headers=None):
    req = Request(base + path, data=data, headers={'Authorization': 'Bearer ' + token, **(headers or {})})
    with opener.open(req, timeout=15) as response:
        return response.read()


ready = json.loads(request('/v1/ready'))
assert ready['ready'] and ready['service_version'] == '0.4.0'
assert set(('siemens-json/2', 'siemens-json/1', 'table-csv/1')).issubset(ready['input_contracts'])
record = {'segment_id': 'upgrade-check', 'source_language': 'en', 'target_language': 'zh-CN',
    'source_text': 'Press Start.', 'target_text': '按启动键。', 'context': None, 'tb_list': [],
    'tm_list': [{'source_text': 'Press Start.', 'target_text': '按启动键。', 'match_rate': 100}]}
selection = {'tm_index': 0, 'match_rate': 100, 'target_text': '按启动键。', 'multiple_targets': False, 'source_exact': True}
packet = {'contract': 'siemens-json/2', 'phase': 'translate', 'project_name': '__worker_upgrade_tm04_check__',
    'request_id': state['operation_id'], 'rows': [], 'tm_reuse': [{'row_id': '0', 'record': record,
    'selection': selection, 'source_units': [], 'memory': {}, 'term_decisions': []}]}
boundary = 'tm04-' + uuid.uuid4().hex
parts = []
for role, name, content in (('source_file', 'tm04-check.json', json.dumps(packet, ensure_ascii=False).encode('utf-8')),
    ('prompt_file', 'tm04-prompt.md', b'Pure TM check; no model call.'),
    ('project_style', 'tm04-project.md', b'Fixed synthetic project style.'),
    ('general_style', 'tm04-general.md', b'Fixed synthetic general style.')):
    parts.append(('--' + boundary + '\r\nContent-Disposition: form-data; name="' + role + '"; filename="' + name +
        '"\r\nContent-Type: application/octet-stream\r\n\r\n').encode() + content + b'\r\n')
parts.append(('--' + boundary + '\r\nContent-Disposition: form-data; name="options"\r\n\r\n{}\r\n--' + boundary + '--\r\n').encode())
start = time.monotonic()
accepted = json.loads(request('/v1/jobs', b''.join(parts), {'Content-Type': 'multipart/form-data; boundary=' + boundary,
    'Idempotency-Key': str(uuid.uuid4())}))
job_id = accepted['job_id']
deadline = time.monotonic() + 30
while time.monotonic() < deadline:
    job = json.loads(request('/v1/jobs/' + job_id))
    if job['status'] in ('completed', 'needs_review', 'failed', 'cancelled', 'interrupted'):
        break
    time.sleep(.1)
assert job['status'] == 'completed' and job['archive_error'] is None, job.get('error')
assert job['model_rows'] == 0 and job['tm_reused_rows'] == 1
archive = (DATA / job['project_archive']).resolve()
assert archive.is_relative_to(DATA / 'projects')
for artifact in job['artifacts']:
    content = request(artifact['download_path'])
    assert hashlib.sha256(content).hexdigest() == artifact['sha256']
    assert content == (archive / 'artifacts' / artifact['filename']).read_bytes()
    if artifact['kind'] == 'run':
        run = json.loads(content)
        assert run['execution'] == {'harness': 'tm-reuse', 'model_called': False, 'exit_code': 0, 'thread_id': None}
    if artifact['kind'] == 'translation':
        assert json.loads(content) == {'rows': []}
assert json.loads((archive / 'batch-candidates.json').read_text(encoding='utf-8'))['segments'] == [record]
report = {'ready': True, 'service_version': ready['service_version'], 'input_contracts': ready['input_contracts'],
    'pure_tm_job_id': job_id, 'pure_tm_status': job['status'], 'model_called': False, 'archive_verified': True,
    'artifact_downloads_verified': True, 'duration_seconds': round(time.monotonic() - start, 3),
    'project_archive': job['project_archive'], 'model_credentials_and_config_preserved': True}
(ROOT / 'build/tm-0.4.0/live-verification.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
print(json.dumps(report, ensure_ascii=False))
