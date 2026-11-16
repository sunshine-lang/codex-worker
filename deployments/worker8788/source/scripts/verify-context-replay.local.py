"""Offline replay of today's input coverage. Never submits a job or calls a model."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time

WORKSPACE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WORKSPACE / 'src'))
from codex_remote_worker.task_tools import READ_OUTPUT_BYTES, TaskTools

out = WORKSPACE / 'build/context-replay' / datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
out.mkdir(parents=True)
results = []
for job_id in ('job_5b28d176b55d4714a06c4028fd31dab6', 'job_485218179fc545208214799faaded6b0'):
    original = Path(r'C:\CodexWorkerData\jobs') / job_id / 'work/.worker'
    meta = json.loads((original / 'manifest.json').read_text(encoding='utf-8'))
    root = out / job_id / 'work'
    directory = root / '.worker'
    (directory / 'guides').mkdir(parents=True)
    meta.update(work_root=str(root), snapshot=str(directory / 'source.json'), source_path=str(directory / 'source.json'))
    shutil.copyfile(original / 'source.json', directory / 'source.json')
    shutil.copyfile(WORKSPACE / 'src/codex_remote_worker/task_tools.py', directory / 'task_tools.py')
    guides = {}
    for role, entry in meta['styles'].items():
        data = Path(entry['text_path']).read_bytes()
        target = directory / 'guides' / (role + '.txt')
        target.write_bytes(data)
        guides[role] = data.decode('utf-8-sig')
        entry.update(text_path=str(target), source_path=str(target), cached=True)
    manifest = directory / 'manifest.json'
    manifest.write_text(json.dumps(meta, ensure_ascii=False), encoding='utf-8')
    tools = TaskTools(manifest)
    started = time.perf_counter()
    summary = tools.prepare_context()
    preparation_seconds = time.perf_counter() - started
    index = json.loads((directory / 'context/index.json').read_text(encoding='utf-8'))
    def read_page(number):
        result = subprocess.run([sys.executable, '-X', 'utf8', str(directory / 'task_tools.py'), 'context', '--page', str(number)],
                                cwd=out, capture_output=True, timeout=15)
        assert result.returncode == 0 and len(result.stdout) <= READ_OUTPUT_BYTES, result.stderr[:100]
        assert b'Warning: truncated output' not in result.stdout
        return json.loads(result.stdout)
    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=8) as executor:
        pages = list(executor.map(read_page, range(index['page_count'])))
    batch_seconds = time.perf_counter() - started
    actual_guides, actual_rows, fragments = {}, {}, {}
    for page in pages:
        for part in page['parts']:
            if part['kind'] == 'guide':
                current = actual_guides.get(part['role'], '')
                assert part['offset'] == len(current)
                actual_guides[part['role']] = current + part['text']
            elif part['kind'] == 'source':
                assert part['row_index'] not in actual_rows
                actual_rows[part['row_index']] = part['row']
            else:
                current = fragments.get(part['row_index'], '')
                assert part['offset'] == len(current)
                fragments[part['row_index']] = current + part['text']
                if part['end_offset'] == part['total_chars']:
                    actual_rows[part['row_index']] = json.loads(fragments.pop(part['row_index']))
    assert not fragments and actual_guides == guides
    assert [actual_rows[i] for i in range(len(actual_rows))] == tools.snapshot['rows']
    # Also verify complete fallback traversal against the exact same reference input.
    offset = char_offset = 0
    rows = []
    row_text = ''
    while True:
        page = tools.source_page(offset, 20, READ_OUTPUT_BYTES, char_offset)
        rows.extend(page['rows'])
        if page.get('fragment'):
            row_text += page['fragment']['text']
            if page['next_char_offset'] == 0:
                rows.append(json.loads(row_text))
                row_text = ''
        if not page['has_more']:
            break
        offset, char_offset = page['next_offset'], page['next_char_offset']
    assert rows == tools.snapshot['rows'] and not row_text
    results.append({'job_id': job_id, 'phase': tools.snapshot['phase'], 'model_called': False,
                    'context': summary, 'largest_page_bytes': max(x['bytes'] for x in index['pages']),
                    'all_guide_text_preserved': True, 'all_model_rows_preserved': True, 'rows': len(rows),
                    'context_preparation_seconds': round(preparation_seconds, 4),
                    'parallel_helper_read_seconds': round(batch_seconds, 4),
                    'source_snapshot_sha256': hashlib.sha256((directory / 'source.json').read_bytes()).hexdigest()})
report = {'model_called': False, 'results': results, 'output_dir': str(out)}
(out / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
print(json.dumps(report, ensure_ascii=False, indent=2))
