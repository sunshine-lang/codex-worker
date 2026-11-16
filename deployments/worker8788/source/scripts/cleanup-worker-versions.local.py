"""Remove explicitly scoped inactive Worker packages; preserve the live installation and source."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import time

sys.stdout.reconfigure(encoding='utf-8')
WORKSPACE = Path(r'C:\Users\Administrator\Documents\codex-remote-worker')
INSTALL = Path(r'C:\CodexRemoteWorker')
DATA = Path(r'C:\CodexWorkerData')
DESKTOP = Path(r'C:\Users\Administrator\Desktop')
REPORT = WORKSPACE / 'build/disk-cleanup-20261113'
PLAN = REPORT / 'plan.json'
EXACT = [DESKTOP/'CodexRemoteWorker-0.3.0-2026-11-12',
         DESKTOP/'CodexWorker_免安装迁移_0.4.0_20261112',
         DESKTOP/'CodexWorker_免安装迁移_20261112',
         INSTALL/'candidate-desktop-subscription-test', INSTALL/'candidate-runtime-tools']
OLD_BACKUPS = ['json-0.3.0-20261111T135916Z', 'sandbox-fix-20261111T085724Z',
               'sandbox-fix-20261111T092140Z', 'sandbox-fix-20261111T131612Z',
               'sandbox-fix-20261111T132120Z', 'tm-0.4.0-20261112T073423Z',
               'bounded-context-20261113T060231Z']
EXACT += [DATA/'maintenance-backups'/name for name in OLD_BACKUPS]
PRESERVE_BUILD = {'bounded-context', 'context-replay', 'bounded-context-build.log', REPORT.name}


def guard(path):
    path = path.absolute()
    scoped = path in EXACT or (path.parent == WORKSPACE/'build' and path.name not in PRESERVE_BUILD) or (
        path.parent == WORKSPACE/'dist' and path.name != 'windows-exe')
    assert scoped, f'Unapproved cleanup path: {path}'
    assert path.resolve() == path, f'Redirected cleanup path: {path}'
    for part in [path, *path.parents]:
        assert not part.is_symlink() and not part.is_junction(), f'Reparse-point ancestor: {part}'
    return path


def measure(root):
    total = count = 0
    pending = [root]
    while pending:
        path = pending.pop()
        info = path.lstat()
        if getattr(info, 'st_file_attributes', 0) & 0x400 or path.is_symlink():
            continue
        if stat.S_ISDIR(info.st_mode):
            pending.extend(path.iterdir())
        else:
            total += info.st_size
            count += 1
    return total, count


def sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def protected_hashes():
    directories = [INSTALL/'bin', INSTALL/'src', INSTALL/'scripts',
                   INSTALL/'.runtime/python313/Lib/site-packages/codex_remote_worker',
                   *[WORKSPACE/name for name in ('src', 'tests', 'docs', 'packaging', 'scripts', 'integrations', '.git')]]
    paths = []
    for directory in directories:
        paths.extend(p for p in directory.rglob('*') if p.is_file() and '__pycache__' not in p.parts)
    config = json.loads((INSTALL/'config.local.json').read_text(encoding='utf-8-sig'))
    paths += [INSTALL/'config.local.json', INSTALL/'desktop.settings.json', INSTALL/'.runtime/start-worker.local.ps1',
              INSTALL/'.runtime/verify-json-runtime.py', DATA/'private/credentials.local.json',
              Path(config['codex_home'])/'config.toml', Path(config['codex_home'])/'auth.json',
              Path(config['codex_executable']), Path(config['tool_python']), Path(config['doc_converter']),
              WORKSPACE/'dist/windows-exe/CodexRemoteWorker/CodexRemoteWorker.exe']
    return {str(p): sha(p) for p in paths if p.exists()}


def processes():
    code = 'Get-CimInstance Win32_Process | Select-Object ProcessId,ParentProcessId,ExecutablePath,CommandLine | ConvertTo-Json -Compress'
    result = subprocess.run([str(INSTALL/'.runtime/powershell/pwsh.exe'), '-NoLogo', '-NoProfile', '-NonInteractive', '-Command', code],
                            capture_output=True, text=True, encoding='utf-8-sig', timeout=20, check=True)
    values = json.loads(result.stdout)
    return values if isinstance(values, list) else [values]


def referenced(path, values):
    own = {os.getpid(), os.getppid()}
    matches = []
    token = str(path).lower().replace('/', '\\')
    for value in values:
        if value['ProcessId'] in own:
            continue
        executable = (value.get('ExecutablePath') or '').lower().replace('/', '\\')
        command = (value.get('CommandLine') or '').lower().replace('/', '\\')
        if executable == token or executable.startswith(token+'\\') or token in command:
            matches.append({'pid': value['ProcessId'], 'executable': value.get('ExecutablePath')})
    return matches


def retry_remove(function, path):
    try:
        function(path)
    except PermissionError:
        os.chmod(path, stat.S_IREAD | stat.S_IWRITE)
        function(path)


def erase(path):
    """Never descend through junctions/symlinks; no recycle-bin copy or replacement backup."""
    info = os.lstat(path)
    directory = bool(getattr(info, 'st_file_attributes', 0) & 0x10)
    if getattr(info, 'st_file_attributes', 0) & 0x400:
        retry_remove(os.rmdir if directory else os.unlink, path)
    elif stat.S_ISDIR(info.st_mode):
        with os.scandir(path) as entries:
            children = [entry.path for entry in entries]
        for child in children:
            erase(child)
        retry_remove(os.rmdir, path)
    else:
        retry_remove(os.unlink, path)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    REPORT.mkdir(parents=True, exist_ok=True)
    if not args.apply:
        candidates = [p for p in EXACT if p.exists()]
        candidates += [p for p in (WORKSPACE/'build').iterdir() if p.name not in PRESERVE_BUILD]
        candidates += [p for p in (WORKSPACE/'dist').iterdir() if p.name != 'windows-exe']
        values = processes()
        targets, skipped = [], []
        for candidate in candidates:
            path = guard(candidate)
            matches = referenced(path, values)
            if matches:
                skipped.append({'path': str(path), 'active_references': matches})
                continue
            size, files = measure(path)
            targets.append({'path': str(path), 'bytes': size, 'files': files})
        plan = {'created_at': datetime.now(timezone.utc).isoformat(), 'free_before': shutil.disk_usage('C:\\').free,
                'targets': targets, 'skipped': skipped, 'protected_hashes': protected_hashes(),
                'estimated_bytes': sum(t['bytes'] for t in targets)}
        PLAN.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps({'plan': str(PLAN), 'target_count': len(targets), 'estimated_gib': round(plan['estimated_bytes']/1024**3, 2),
                          'targets': targets, 'skipped': skipped}, ensure_ascii=False), flush=True)
        return
    plan = json.loads(PLAN.read_text(encoding='utf-8'))
    current = protected_hashes()
    assert current == plan['protected_hashes'], 'Current installation/source changed since planning; no cleanup performed'
    # Validate every final absolute target before starting any recursive deletion.
    targets = [guard(Path(item['path'])) for item in plan['targets']]
    assert len(set(targets)) == len(targets)
    values = processes()
    for path in targets:
        assert not referenced(path, values), f'Cleanup path became active: {path}'
    result = {'started_at': datetime.now(timezone.utc).isoformat(), 'free_before': shutil.disk_usage('C:\\').free,
              'deleted': [], 'failed': [], 'protected_content_unchanged': False}
    started = time.monotonic()
    for item, path in zip(plan['targets'], targets):
        try:
            guard(path)
            if path.exists():
                erase('\\\\?\\'+str(path))
            assert not path.exists()
            result['deleted'].append(item)
            print(json.dumps({'deleted': str(path), 'estimated_gib': round(item['bytes']/1024**3, 3)}, ensure_ascii=False), flush=True)
        except OSError as error:
            result['failed'].append({'path': str(path), 'error_type': type(error).__name__, 'winerror': getattr(error, 'winerror', None)})
        (REPORT/'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    result['protected_content_unchanged'] = protected_hashes() == plan['protected_hashes']
    result.update(free_after=shutil.disk_usage('C:\\').free, elapsed_seconds=round(time.monotonic()-started, 2),
                  finished_at=datetime.now(timezone.utc).isoformat())
    result['freed_bytes'] = result['free_after']-result['free_before']
    (REPORT/'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'deleted_count': len(result['deleted']), 'failed': result['failed'],
                      'freed_gib': round(result['freed_bytes']/1024**3, 2), 'free_after_gib': round(result['free_after']/1024**3, 2),
                      'protected_content_unchanged': result['protected_content_unchanged'], 'report': str(REPORT/'result.json')}, ensure_ascii=False), flush=True)
    assert result['protected_content_unchanged'], 'Protected installation/source changed; inspect report'
    if result['failed']:
        raise SystemExit(2)


if __name__ == '__main__':
    main()
