"""Stopped-service transactional deployment; no credentials or sandbox configuration writes."""
from pathlib import Path
import argparse
from datetime import datetime, timezone
import hashlib
import json
import shutil
import sqlite3
import sys
import zipfile

WORKSPACE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WORKSPACE / 'src'))
from codex_remote_worker.store import InstanceLock

ROOT = Path(r'C:\CodexRemoteWorker')
DATA = Path(r'C:\CodexWorkerData')
STATE = WORKSPACE / 'build/tm-0.4.0/upgrade-state.json'


def sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')


def restore(state):
    # Only destinations enumerated by this deployment may be removed or restored.
    if state.get('rolled_back'):
        return
    for item in reversed(state.get('changes', [])):
        target, backup = Path(item['target']), Path(item['backup'])
        permitted = target.is_relative_to(ROOT) or target == Path(r'C:\Users\Administrator\Desktop\Codex 服务启动器.exe')
        assert permitted and target != ROOT and backup.is_relative_to(Path(state['backup']))
        if item['existed'] and not backup.exists():
            continue  # The original rename never happened; do not delete an untouched original.
        if target.exists():
            if target.is_dir():
                assert target.resolve().is_relative_to(ROOT.resolve()) and not target.is_symlink()
                shutil.rmtree(target)
            else:
                target.unlink()
        if item['existed'] and backup.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(backup), str(target))
    state['rolled_back'] = True
    save(STATE, state)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--rollback', action='store_true')
    args = parser.parse_args()
    import win32file
    import win32con
    from win32com.shell import shell
    assert shell.IsUserAnAdmin(), 'Administrator deployment context required'
    state = json.loads(STATE.read_text(encoding='utf-8'))
    marker = json.loads((DATA / '.maintenance.json').read_text(encoding='utf-8-sig'))
    assert marker['operation_id'] == state['operation_id'], 'Maintenance ownership changed'
    control = win32file.CreateFile(str(DATA / '.desktop-control.lock'), win32con.GENERIC_READ | win32con.GENERIC_WRITE,
        0, None, win32con.OPEN_ALWAYS, win32con.FILE_ATTRIBUTE_NORMAL, None)
    lock = InstanceLock(DATA / '.instance.lock')
    db = sqlite3.connect(DATA / 'jobs.sqlite3', isolation_level=None, timeout=10)
    try:
        db.execute('BEGIN IMMEDIATE')
        assert db.execute("SELECT COUNT(*) FROM jobs WHERE status IN ('queued','running','checking','cancelling')").fetchone()[0] == 0, 'Active jobs; no files changed'
        if args.rollback:
            restore(state)
            print(json.dumps({'rolled_back': True, 'backup': state['backup']}))
            return
        assert not state.get('installed'), 'Already deployed'
        for path, expected in state['protected_hashes'].items():
            assert sha(Path(path)) == expected, 'Protected file changed since baseline'
        candidate = WORKSPACE / 'dist/windows-exe/CodexRemoteWorker'
        assert (candidate / 'CodexRemoteWorker.exe').is_file()
        backup = DATA / 'maintenance-backups' / ('tm-0.4.0-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ'))
        backup.mkdir(parents=True, exist_ok=False)
        state.update(backup=str(backup), changes=[])
        save(STATE, state)
        def replace(target, name, source=None, directory=False):
            target = Path(target)
            item = {'target': str(target), 'backup': str(backup / name), 'existed': target.exists()}
            state['changes'].append(item)
            save(STATE, state)
            if item['existed']:
                shutil.move(str(target), item['backup'])
            if source is not None:
                if directory:
                    shutil.copytree(source, target)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, target)
        try:
            replace(ROOT / 'bin/CodexRemoteWorker', 'CodexRemoteWorker', candidate, True)
            replace(ROOT / 'src/codex_remote_worker', 'source-package', WORKSPACE / 'src/codex_remote_worker', True)
            site = ROOT / '.runtime/python313/Lib/site-packages'
            replace(site / 'codex_remote_worker', 'python-package')
            for path in site.glob('codex_remote_worker-*.dist-info'):
                replace(path, path.name)
            wheel = WORKSPACE / 'build/tm-0.4.0/wheels/codex_remote_worker-0.4.0-py3-none-any.whl'
            metadata = site / 'codex_remote_worker-0.4.0.dist-info'
            replace(metadata, 'new-metadata-placeholder')
            with zipfile.ZipFile(wheel) as package:
                for entry in package.infolist():
                    relative = Path(entry.filename)
                    assert not relative.is_absolute() and '..' not in relative.parts
                    assert relative.parts[0] in ('codex_remote_worker', 'codex_remote_worker-0.4.0.dist-info')
                    if entry.is_dir():
                        continue
                    path = site / relative
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(package.read(entry))
            launcher = WORKSPACE / 'dist/windows-exe/CodexWorkerLauncher.exe'
            replace(ROOT / 'bin/CodexWorkerLauncher.exe', 'CodexWorkerLauncher.exe', launcher)
            replace(Path(r'C:\Users\Administrator\Desktop\Codex 服务启动器.exe'), 'desktop-launcher.exe', launcher)
            replace(ROOT / '.runtime/verify-json-runtime.py', 'verify-json-runtime.py', WORKSPACE / 'packaging/desktop-user/verify-json-runtime.py')
            replace(ROOT / 'pyproject.toml', 'pyproject.toml', WORKSPACE / 'pyproject.toml')
            for path, expected in state['protected_hashes'].items():
                if path.endswith('CodexRemoteWorker.exe'):
                    continue
                assert sha(Path(path)) == expected, 'Unrelated configuration changed'
            assert sha(candidate / 'CodexRemoteWorker.exe') == sha(ROOT / 'bin/CodexRemoteWorker/CodexRemoteWorker.exe')
            state.update(installed=True, version='0.4.0', installed_at=datetime.now(timezone.utc).isoformat(),
                installed_exe_sha256=sha(candidate / 'CodexRemoteWorker.exe'), native_checks_passed=False)
            save(STATE, state)
            save(backup / 'deployment.json', {k: state[k] for k in ('operation_id', 'backup', 'version', 'installed_at', 'installed_exe_sha256')})
            print(json.dumps({'installed': True, 'version': '0.4.0', 'backup': str(backup), 'needs_native_checks': True}))
        except BaseException:
            restore(state)
            raise
    finally:
        db.rollback()
        db.close()
        lock.close()
        control.Close()


if __name__ == '__main__':
    main()
