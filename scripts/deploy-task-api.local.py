"""Stopped-service, incremental deployment of the whole-task API."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import sys

WORKSPACE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WORKSPACE/'src'))
from codex_remote_worker.store import InstanceLock

ROOT = Path(r'C:\CodexRemoteWorker')
DATA = Path(r'C:\CodexWorkerData')
STATE = WORKSPACE/'build/task-api/deployment-state.json'
MODULES = ('app.py', 'translation_tasks.py', 'translation_task_api.py')


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def save(state):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding='utf-8')


def records(path, table, key):
    if not path.exists():
        return {}
    with sqlite3.connect(path.as_uri()+'?mode=ro', uri=True) as db:
        return {row[0]: hashlib.sha256(row[1].encode('utf-8')).hexdigest()
                for row in db.execute(f'SELECT {key},record FROM {table}')}


def restore(state):
    for item in reversed(state['files']):
        target = Path(item['target'])
        assert target.is_relative_to(ROOT) or target == DATA/'preflight.json'
        if item['existed']:
            shutil.copy2(item['backup'], target)
        elif target.exists():
            target.unlink()
    installed = ROOT/'bin/CodexRemoteWorker'
    assert installed.resolve().is_relative_to(ROOT.resolve()) and not installed.is_symlink() and not installed.is_junction()
    before = set(state['bin_files'])
    for path in installed.rglob('*'):
        if path.is_file() and path.relative_to(installed).as_posix() not in before:
            path.unlink()
    shutil.copytree(Path(state['backup'])/'worker-bin', installed, dirs_exist_ok=True)
    state['rolled_back'] = True
    save(state)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=('install', 'verify', 'rollback'))
    parser.add_argument('--operation-id', required=True)
    args = parser.parse_args()
    marker = json.loads((DATA/'.maintenance.json').read_text(encoding='utf-8-sig'))
    assert marker['operation_id'] == args.operation_id, 'Maintenance ownership changed'
    state = json.loads(STATE.read_text(encoding='utf-8-sig')) if args.action != 'install' else None
    if args.action == 'verify':
        for path, expected in state['protected_hashes'].items():
            assert sha(Path(path)) == expected, 'An unrelated module or configuration changed'
        for table, key in (('jobs', 'job_id'), ('tasks', 'task_id')):
            actual = records(DATA/(table+'.sqlite3'), table, key)
            assert all(actual.get(i) == value for i, value in state['history'][table].items()), 'Existing task history changed'
        for item in state['files']:
            if item['source']:
                assert sha(Path(item['target'])) == sha(Path(item['source'])), 'Installed source differs from candidate'
        assert sha(ROOT/'bin/CodexRemoteWorker/CodexRemoteWorker.exe') == state['exe_sha256']
        print(json.dumps({'verified': True, 'configuration_and_v4_preserved': True, 'existing_history_preserved': True}))
        return
    lock = InstanceLock(DATA/'.instance.lock')
    databases = []
    try:
        for table in ('jobs', 'tasks'):
            path = DATA/(table+'.sqlite3')
            if not path.exists():
                continue
            db = sqlite3.connect(path, isolation_level=None, timeout=10)
            databases.append(db)
            db.execute('BEGIN IMMEDIATE')
            states = ('queued', 'running', 'checking', 'cancelling') if table == 'jobs' else ('queued', 'running')
            placeholders = ','.join('?' for _ in states)
            assert db.execute(f'SELECT COUNT(*) FROM {table} WHERE status IN ({placeholders})', states).fetchone()[0] == 0, 'Active jobs or whole tasks; installation unchanged'
        if args.action == 'rollback':
            assert state['operation_id'] == args.operation_id
            restore(state)
            print(json.dumps({'rolled_back': True, 'backup': state['backup']}))
            return
        candidate = WORKSPACE/'dist/windows-exe/CodexRemoteWorker'
        assert (candidate/'CodexRemoteWorker.exe').is_file()
        backup = DATA/'maintenance-backups'/('task-api-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ'))
        backup.mkdir(parents=True, exist_ok=False)
        config = json.loads((ROOT/'config.local.json').read_text(encoding='utf-8-sig'))
        home = Path(config['codex_home'])
        protected = [ROOT/'config.local.json', ROOT/'desktop.settings.json', ROOT/'bin/CodexWorkerLauncher.exe',
                     ROOT/'.runtime/verify-json-runtime.py', DATA/'private/credentials.local.json', home/'config.toml', home/'auth.json']
        protected += [p for p in (ROOT/'src/codex_remote_worker').glob('*.py') if p.name not in MODULES]
        state = {'operation_id': args.operation_id, 'backup': str(backup), 'files': [], 'installed': False,
                 'protected_hashes': {str(p): sha(p) for p in protected if p.exists()},
                 'history': {table: records(DATA/(table+'.sqlite3'), table, key) for table, key in (('jobs', 'job_id'), ('tasks', 'task_id'))},
                 'exe_sha256': sha(candidate/'CodexRemoteWorker.exe'),
                 'bin_files': [p.relative_to(ROOT/'bin/CodexRemoteWorker').as_posix() for p in (ROOT/'bin/CodexRemoteWorker').rglob('*') if p.is_file()]}
        shutil.copytree(ROOT/'bin/CodexRemoteWorker', backup/'worker-bin')
        targets = [(DATA/'preflight.json', None), (ROOT/'docs/WHOLE_TASK_API.md', WORKSPACE/'docs/WHOLE_TASK_API.md')]
        for relative in ('src/codex_remote_worker', '.runtime/python313/Lib/site-packages/codex_remote_worker'):
            assert (ROOT/relative).is_dir()
            targets.extend((ROOT/relative/name, WORKSPACE/'src/codex_remote_worker'/name) for name in MODULES)
        for index, (target, source) in enumerate(targets):
            item = {'target': str(target), 'source': str(source) if source else None,
                    'backup': str(backup/f'file-{index}'), 'existed': target.exists()}
            if item['existed']:
                shutil.copy2(target, item['backup'])
            state['files'].append(item)
        save(state)
        try:
            shutil.copytree(candidate, ROOT/'bin/CodexRemoteWorker', dirs_exist_ok=True)
            for item in state['files']:
                if item['source']:
                    target = Path(item['target'])
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(item['source'], target)
            state['installed'] = True
            save(state)
        except BaseException:
            restore(state)
            raise
        print(json.dumps({'installed': True, 'contract': 'translation-task/1', 'backup': str(backup)}))
    finally:
        for db in reversed(databases):
            db.rollback()
            db.close()
        lock.close()


if __name__ == '__main__':
    main()
