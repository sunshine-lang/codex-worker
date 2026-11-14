"""Idle, stopped-service file transaction; caller owns the desktop control lock."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import sys

WORKSPACE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WORKSPACE / 'src'))
from codex_remote_worker.store import InstanceLock

ROOT = Path(r'C:\CodexRemoteWorker')
DATA = Path(r'C:\CodexWorkerData')
STATE = WORKSPACE / 'build/bounded-context/deployment-state.json'


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def save(state):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding='utf-8')


def history(db):
    return hashlib.sha256(json.dumps(db.execute('select job_id,record from jobs order by job_id').fetchall(),
                                    ensure_ascii=False).encode('utf-8')).hexdigest()


def restore(state):
    for item in reversed(state['files']):
        target, backup = Path(item['target']), Path(item['backup'])
        assert target.is_relative_to(ROOT) or target == DATA / 'preflight.json'
        if item['existed']:
            shutil.copy2(backup, target)
        elif target.exists():
            target.unlink()
    installed = ROOT / 'bin/CodexRemoteWorker'
    assert installed.resolve().is_relative_to(ROOT.resolve())
    before = set(state['bin_files'])
    for path in installed.rglob('*'):
        if path.is_file() and path.relative_to(installed).as_posix() not in before:
            path.unlink()
    shutil.copytree(Path(state['backup']) / 'worker-bin', installed, dirs_exist_ok=True)
    state['rolled_back'] = True
    save(state)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=('install', 'rollback', 'verify'))
    parser.add_argument('--operation-id', required=True)
    args = parser.parse_args()
    marker = json.loads((DATA / '.maintenance.json').read_text(encoding='utf-8-sig'))
    assert marker['operation_id'] == args.operation_id, 'Maintenance ownership changed'
    state = json.loads(STATE.read_text(encoding='utf-8')) if args.action != 'install' else None
    if args.action == 'verify':
        for path, expected in state['protected_hashes'].items():
            assert sha(Path(path)) == expected, 'Protected configuration changed'
        with sqlite3.connect((DATA / 'jobs.sqlite3').as_uri()+'?mode=ro', uri=True) as db:
            assert history(db) == state['history_sha256'], 'Job history changed'
        for item in state['files']:
            if item.get('source'):
                assert sha(Path(item['target'])) == sha(Path(item['source'])), 'Installed module mismatch'
        assert sha(ROOT / 'bin/CodexRemoteWorker/CodexRemoteWorker.exe') == state['exe_sha256']
        print(json.dumps({'verified': True, 'protected_configuration_preserved': True, 'job_history_preserved': True}))
        return
    lock = InstanceLock(DATA / '.instance.lock')
    db = sqlite3.connect(DATA / 'jobs.sqlite3', isolation_level=None, timeout=10)
    try:
        db.execute('BEGIN IMMEDIATE')
        assert db.execute("select count(*) from jobs where status in ('queued','running','checking','cancelling')").fetchone()[0] == 0, 'Active jobs; installation unchanged'
        if args.action == 'rollback':
            assert state['operation_id'] == args.operation_id
            restore(state)
            print(json.dumps({'rolled_back': True, 'backup': state['backup']}))
            return
        candidate = WORKSPACE / 'dist/windows-exe/CodexRemoteWorker'
        assert (candidate / 'CodexRemoteWorker.exe').is_file()
        backup = DATA / 'maintenance-backups' / ('bounded-context-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ'))
        backup.mkdir(parents=True, exist_ok=False)
        config = json.loads((ROOT / 'config.local.json').read_text(encoding='utf-8-sig'))
        home = Path(config['codex_home'])
        protected = [ROOT/'config.local.json', ROOT/'desktop.settings.json', ROOT/'bin/CodexWorkerLauncher.exe',
                     DATA/'private/credentials.local.json', home/'config.toml', home/'auth.json']
        state = {'operation_id': args.operation_id, 'backup': str(backup), 'files': [],
                 'protected_hashes': {str(p): sha(p) for p in protected if p.exists()},
                 'history_sha256': history(db), 'installed': False,
                 'exe_sha256': sha(candidate/'CodexRemoteWorker.exe'),
                 'bin_files': [p.relative_to(ROOT/'bin/CodexRemoteWorker').as_posix() for p in (ROOT/'bin/CodexRemoteWorker').rglob('*') if p.is_file()]}
        shutil.copytree(ROOT/'bin/CodexRemoteWorker', backup/'worker-bin')
        targets = [(DATA/'preflight.json', None),
                   (ROOT/'.runtime/verify-json-runtime.py', WORKSPACE/'packaging/desktop-user/verify-json-runtime.py')]
        for relative in ('src/codex_remote_worker', '.runtime/python313/Lib/site-packages/codex_remote_worker'):
            if (ROOT/relative).is_dir():
                targets.extend((ROOT/relative/name, WORKSPACE/'src/codex_remote_worker'/name)
                               for name in ('runner.py', 'task_runtime.py', 'task_tools.py'))
        for number, (target, source) in enumerate(targets):
            item = {'target': str(target), 'backup': str(backup/f'file-{number}'), 'existed': target.exists(),
                    'source': str(source) if source else None}
            if item['existed']:
                shutil.copy2(target, item['backup'])
            state['files'].append(item)
        save(state)
        try:
            shutil.copytree(candidate, ROOT/'bin/CodexRemoteWorker', dirs_exist_ok=True)
            for item in state['files']:
                if item['source']:
                    shutil.copy2(item['source'], item['target'])
            state['installed'] = True
            save(state)
        except BaseException:
            restore(state)
            raise
        print(json.dumps({'installed': True, 'backup': str(backup), 'runtime': 'utf8-absolute-tools-v4-bounded-context'}))
    finally:
        db.rollback()
        db.close()
        lock.close()


if __name__ == '__main__':
    main()
