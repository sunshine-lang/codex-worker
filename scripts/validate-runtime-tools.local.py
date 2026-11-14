"""Service-account native validation. No jobs are submitted and no model is called."""
import argparse
import asyncio
import json
from pathlib import Path
import shutil
import sqlite3
import sys
import time
import uuid

parser = argparse.ArgumentParser()
parser.add_argument('--source-root', required=True, type=Path)
args = parser.parse_args()
sys.path.insert(0, str(args.source_root / 'src'))
from codex_remote_worker.common import Redactor, file_digest, write_json
from codex_remote_worker.config import load_settings
from codex_remote_worker.doctor import verify
from codex_remote_worker.qa import read_source
from codex_remote_worker.task_runtime import prepare_task_runtime

install = Path(r'C:\CodexRemoteWorker')
settings = load_settings(install / 'config.local.json', Path(r'C:\CodexWorkerData\private\credentials.local.json'))
with sqlite3.connect((settings.data_root / 'jobs.sqlite3').as_uri() + '?mode=ro', uri=True) as db:
    active = db.execute("select count(*) from jobs where status in ('queued','running','checking','cancelling')").fetchone()[0]
    if active:
        raise SystemExit('Active tasks; native validation deferred')
    reference = json.loads(db.execute('select record from jobs order by created_at desc limit 1').fetchone()[0])


async def main():
    native = await verify(settings, live=True)
    report = {'native_probe': native, 'preparations': [], 'model_called': False}
    for index in range(2):
        root = settings.data_root / 'runtime-validation' / ('probe-' + uuid.uuid4().hex)
        for folder in ('input', 'work', 'control', 'logs'):
            (root / folder).mkdir(parents=True)
        original = settings.data_root / 'jobs' / reference['job_id'] / 'input'
        for entry in reference['inputs'].values():
            for folder in ('input', 'work'):
                shutil.copyfile(original / entry['filename'], root / folder / entry['filename'])
        source = root / 'input' / reference['inputs']['source_file']['filename']
        write_json(root / 'control' / 'source.json', read_source(source))
        fixture = {**reference, 'job_id': 'validation_' + uuid.uuid4().hex}
        started = time.monotonic()
        result = await prepare_task_runtime(settings, fixture, root, lambda **kw: None,
                                            lambda: False, Redactor(settings.secret_values()))
        result['seconds'] = round(time.monotonic() - started, 3)
        result['round'] = index + 1
        assert result['model_called'] is False
        for role, entry in fixture['inputs'].items():
            assert file_digest(root / 'work' / entry['filename']) == entry['sha256'], role
        report['preparations'].append(result)
    assert all(report['preparations'][1]['guide_cache_hits'].values()), report
    write_json(settings.data_root / 'runtime-validation' / 'validation-report.json', report)
    print(json.dumps(report, ensure_ascii=False), flush=True)


asyncio.run(main())
