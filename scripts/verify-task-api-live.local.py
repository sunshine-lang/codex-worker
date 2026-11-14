"""Live whole-task API smoke: one all-TM order and one tiny mixed order."""
from datetime import datetime, timezone
import copy
import json
from pathlib import Path
import sqlite3
import sys
import time
import uuid

import httpx

sys.stdout.reconfigure(encoding='utf-8')
ROOT = Path(r'C:\CodexRemoteWorker')
DATA = Path(r'C:\CodexWorkerData')
WORKSPACE = Path(__file__).resolve().parents[1]
config = json.loads((ROOT/'config.local.json').read_text(encoding='utf-8-sig'))
credentials = json.loads((DATA/'private/credentials.local.json').read_text(encoding='utf-8-sig'))
base = 'http://127.0.0.1:'+str(config['port'])
headers = {'Authorization': 'Bearer '+credentials['CODEX_WORKER_SERVICE_TOKEN']}


def record(identifier, locked):
    return {'segment_id': identifier, 'source_language': 'en', 'target_language': 'zh',
            'source_text': 'Close the window.', 'target_text': None, 'context': 'Application button.',
            'tb_list': [{'source_text': 'window', 'target_text': '窗口'}],
            'tm_list': [{'source_text': 'Close the window.', 'target_text': '关闭窗口。', 'match_rate': 100}] if locked else [],
            'business': {'code': '00001', 'flag': False, 'optional': None}}


def files(value):
    return {'source_file': ('order.json', json.dumps(value, ensure_ascii=False).encode('utf-8'), 'application/json'),
            'prompt_file': ('prompt.md', 'Translate the English UI text into concise Simplified Chinese using the supplied terminology and guides.'.encode(), 'text/plain'),
            'project_style': ('project.md', 'UI translation. Preserve identifiers and punctuation. Translate window as 窗口.'.encode('utf-8'), 'text/plain'),
            'general_style': ('general.md', 'Use natural, concise Simplified Chinese. Do not add explanations.'.encode(), 'text/plain')}


def poll(client, task_id):
    deadline = time.monotonic()+180
    while time.monotonic() < deadline:
        response = client.get('/v1/tasks/'+task_id)
        response.raise_for_status()
        value = response.json()
        if value['status'] in ('completed', 'failed'):
            assert value['status'] == 'completed', value.get('error')
            return value
        time.sleep(1)
    raise RuntimeError('Live task still pending; task was preserved and not automatically retried')


def submit(client, value, key):
    response = client.post('/v1/tasks', files=files(value), data={'options': '{"timeout_seconds":120}'},
                           headers={'Idempotency-Key': key})
    response.raise_for_status()
    return response


def compare(original, result):
    assert set(original) == set(result)
    assert {k:v for k,v in original.items() if k!='segments'} == {k:v for k,v in result.items() if k!='segments'}
    assert len(original['segments']) == len(result['segments'])
    for before, after in zip(original['segments'], result['segments']):
        assert {k:v for k,v in before.items() if k!='target_text'} == {k:v for k,v in after.items() if k!='target_text'}
        assert isinstance(after['target_text'], str) and after['target_text'].strip()
        if before['tm_list']:
            assert after['target_text'] == before['tm_list'][0]['target_text']


def main():
    with sqlite3.connect((DATA/'jobs.sqlite3').as_uri()+'?mode=ro', uri=True) as db:
        assert db.execute("select count(*) from jobs where status in ('queued','running','checking','cancelling')").fetchone()[0] == 0, 'Existing jobs are active; live smoke deferred'
    run = uuid.uuid4().hex[:10]
    report = {'started_at': datetime.now(timezone.utc).isoformat(), 'checks': [], 'tasks': []}
    with httpx.Client(base_url=base, headers=headers, trust_env=False, timeout=20) as client:
        assert httpx.get(base+'/v1/tasks/capabilities', trust_env=False, timeout=10).status_code == 401
        cap = client.get('/v1/tasks/capabilities')
        cap.raise_for_status()
        assert cap.json()['contract'] == 'translation-task/1' and cap.json()['semantic_review'] is False
        report['checks'] += ['existing_bearer_auth', 'whole_task_capability']
        pure = {'project_name': '__task_api_tm_probe__'+run, 'segments': [record('tm-0001', True)], 'business': {'keep': None}}
        key = str(uuid.uuid4())
        first = submit(client, pure, key)
        assert first.status_code == 202
        duplicate = submit(client, pure, key)
        assert duplicate.status_code == 200 and duplicate.json()['reused_submission']
        assert first.json()['task_id'] == duplicate.json()['task_id']
        task = poll(client, first.json()['task_id'])
        assert task['progress']['total_batches'] == 0 and task['review_status'] == 'not_started'
        response = client.get(task['result_path'])
        response.raise_for_status()
        compare(pure, response.json())
        report['tasks'].append({'task_id': task['task_id'], 'kind': 'all_tm', 'model_called': False, 'status': task['status']})
        report['checks'] += ['all_tm_zero_batches', 'idempotent_retry', 'tm_target_exact_preservation']
        mixed = {'project_name': '__task_api_model_probe__'+run,
                 'segments': [record('tm-0001', True), record('model-0002', False)], 'business': {'keep': None}}
        mixed_started = time.monotonic()
        accepted = submit(client, mixed, str(uuid.uuid4()))
        assert accepted.status_code == 202
        assert accepted.json()['progress']['tm_reused_segments'] == 1
        assert accepted.json()['progress']['model_required_segments'] == 1
        task = poll(client, accepted.json()['task_id'])
        response = client.get(task['result_path'])
        response.raise_for_status()
        compare(mixed, response.json())
        task_root = DATA/'tasks'/task['task_id']
        with sqlite3.connect((DATA/'tasks.sqlite3').as_uri()+'?mode=ro', uri=True) as db:
            stored = json.loads(db.execute('select record from tasks where task_id=?', (task['task_id'],)).fetchone()[0])
        assert len(stored['batches']) == 1
        job_id = stored['batches'][0]['job_id']
        legacy = client.get('/v1/jobs/'+job_id)
        legacy.raise_for_status()
        assert legacy.json()['model_rows'] == 1
        with sqlite3.connect((DATA/'jobs.sqlite3').as_uri()+'?mode=ro', uri=True) as db:
            job = json.loads(db.execute('select record from jobs where job_id=?', (job_id,)).fetchone()[0])
        execution = job['execution']
        assert execution['exit_code'] == 0 and execution['turn_completed'] is True and execution['trace_complete'] is True
        assert execution['runtime_preparation']['runtime_version'] == 'utf8-absolute-tools-v4-bounded-context'
        packet = json.loads((DATA/'jobs'/job_id/'control/source.json').read_text(encoding='utf-8'))
        assert [r['row_id'] for r in packet['rows']] == ['1'] and packet['tm_reuse'] == []
        assert task['review_status'] == 'not_started'
        report['tasks'].append({'task_id': task['task_id'], 'kind': 'mixed', 'worker_job_id': job_id,
                                'model_called': True, 'model_rows': 1, 'tm_rows': 1, 'status': task['status'],
                                'seconds': round(time.monotonic()-mixed_started, 3),
                                'configured_model': execution['configured_model'],
                                'runtime': execution['runtime_preparation']['runtime_version']})
        report['checks'] += ['real_model_child_job', 'locked_tm_excluded_from_model', 'v4_context_runtime',
                             'legacy_job_endpoint', 'original_business_fields_preserved', 'candidate_result_download']
        assert client.get('/v1/ready').json()['ready'] is True
    report.update(passed=True, completed_at=datetime.now(timezone.utc).isoformat())
    destination = WORKSPACE/'build/task-api/live-validation.json'
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
