import hashlib
import json
from pathlib import Path
import subprocess
import sys


def test_build_manifest_binds_source_and_runtime_without_reading_config(tmp_path):
    source=tmp_path/'source';modules=source/'src/codex_remote_worker';modules.mkdir(parents=True)
    (modules/'__init__.py').write_text('version = "fixture"')
    (source/'packaging').mkdir();(source/'packaging/worker_entry.py').write_text('pass')
    (source/'config.local.json').write_text('not part of the build manifest')
    runtime=tmp_path/'dist/CodexRemoteWorker';runtime.mkdir(parents=True)
    executable=runtime/'CodexRemoteWorker.exe';executable.write_bytes(b'fixture executable')
    script=Path(__file__).resolve().parents[1]/'scripts/write-build-manifest.py'
    result=subprocess.run([sys.executable,str(script),'--source-root',str(source),'--runtime-root',str(runtime)],capture_output=True,text=True)
    assert result.returncode==0,result.stderr
    manifest=json.loads((runtime.parent/'concurrency-build-manifest.json').read_text())
    assert manifest['scheduler_version']=='bounded-concurrency/1'
    assert 'config.local.json' not in manifest['source_sha256']
    assert manifest['runtime_sha256']['CodexRemoteWorker.exe']==hashlib.sha256(executable.read_bytes()).hexdigest()
    assert manifest['windows_runtime_smoke_verified'] is False
