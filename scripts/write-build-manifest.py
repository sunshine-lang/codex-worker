"""Record the exact source and frozen runtime; never reads credentials or task data."""
import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

BASE_COMMIT = '207e1b42be51c0da4786c11ec70dc4320bbdc168'


def sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-root',type=Path,required=True)
    parser.add_argument('--runtime-root',type=Path,required=True)
    args=parser.parse_args();source=args.source_root.resolve();runtime=args.runtime_root.resolve()
    executable=runtime/'CodexRemoteWorker.exe'
    if not executable.is_file():raise SystemExit('Built CodexRemoteWorker.exe was not found.')
    modules=sorted((source/'src/codex_remote_worker').glob('*.py'))
    if not modules:raise SystemExit('Application source modules were not found.')
    paths=modules+[source/'packaging/worker_entry.py']
    report={'base_commit':BASE_COMMIT,'scheduler_version':'bounded-concurrency/1',
            'built_at':datetime.now(timezone.utc).isoformat(),
            'source_sha256':{str(p.relative_to(source)).replace('\\','/'):sha(p) for p in paths},
            'runtime_sha256':{str(p.relative_to(runtime)).replace('\\','/'):sha(p) for p in sorted(runtime.rglob('*')) if p.is_file()},
            'windows_runtime_smoke_verified':False}
    path=runtime.parent/'concurrency-build-manifest.json'
    path.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(path)


if __name__=='__main__':main()
