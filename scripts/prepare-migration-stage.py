"""Stage the exact installed Worker and offline runtimes, without machine state or credentials."""
import argparse
import ast
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys


def sha(path):
    value = hashlib.sha256()
    with path.open('rb') as stream:
        while chunk := stream.read(1024 * 1024):
            value.update(chunk)
    return value.hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--installed-root', type=Path, default=Path(r'C:\CodexRemoteWorker'))
    parser.add_argument('--stage', type=Path)
    args = parser.parse_args()
    project = Path(__file__).resolve().parents[1]
    stage = (args.stage or project / 'build/migration-stage-0.3.0').resolve()
    if not stage.is_relative_to(project / 'build'):
        raise ValueError('Stage must be inside this project build directory')
    install = args.installed_root.resolve()
    config = json.loads((install / 'config.local.json').read_text(encoding='utf-8-sig'))
    app = stage / 'app'
    ignore = shutil.ignore_patterns('__pycache__', '*.pyc', '*.pyo', '.DS_Store')
    pairs = [
        (install / 'bin/CodexRemoteWorker', app / 'bin/CodexRemoteWorker'),
        (Path(config['tool_python']).parent, app / '.runtime/python313'),
        (install / '.runtime/powershell', app / '.runtime/powershell'),
        (Path(config['codex_executable']).parent, app / 'tools/codex/bin'),
        (Path(config['doc_converter']).parent.parent, app / 'tools/LibreOffice'),
        (project / 'build/installer-stage/app/licenses', app / 'licenses'),
        (project / 'build/installer-stage/dependencies', stage / 'dependencies'),
    ]
    for source, target in pairs:
        if not source.is_dir():
            raise FileNotFoundError(str(source))
        print(json.dumps({'phase': 'copy', 'component': str(target.relative_to(stage))}), flush=True)
        shutil.copytree(source, target, dirs_exist_ok=True, ignore=ignore)
    for relative in ['installer', 'scripts']:
        (app / relative).mkdir(parents=True, exist_ok=True)
    # Create a normal 0.3.0 wheel from deployed sources for the auxiliary Python interpreter.
    wheel_root = project / 'build/migration-0.3.0/wheel-source'
    package = wheel_root / 'src/codex_remote_worker'
    package.mkdir(parents=True, exist_ok=True)
    for source in (install / 'src/codex_remote_worker').glob('*.py'):
        shutil.copy2(source, package / source.name)
    shutil.copy2(install / 'pyproject.toml', wheel_root / 'pyproject.toml')
    shutil.copy2(project / 'NOTICE.md', wheel_root / 'NOTICE.md')
    wheel_dir = project / 'build/migration-0.3.0/wheels'
    wheel_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run([str(config['tool_python']), '-X', 'utf8', '-m', 'pip', 'wheel', '--no-index', '--no-deps',
                    '--no-build-isolation', '--wheel-dir', str(wheel_dir), str(wheel_root)], check=True)
    wheel = wheel_dir / 'codex_remote_worker-0.3.0-py3-none-any.whl'
    if not wheel.is_file():
        raise RuntimeError('Deployed baseline version is not the expected 0.3.0')
    subprocess.run([str(app / '.runtime/python313/python.exe'), '-X', 'utf8', '-m', 'pip', 'install',
                    '--no-index', '--no-deps', '--force-reinstall', str(wheel)], check=True)
    baseline_exe = install / 'bin/CodexRemoteWorker/CodexRemoteWorker.exe'
    helper = install / 'src/codex_remote_worker/task_tools.py'
    if sha(helper) != sha(app / 'bin/CodexRemoteWorker/_internal/codex_remote_worker/task_tools.py'):
        raise RuntimeError('Deployed helper source and frozen resource differ')
    manifest = {
        'product_id': '5ea84759-74a0-438e-bcaf-cf19803d8053', 'worker_version': '0.3.0',
        'installer_version': '1.1.0', 'variant': 'json-on-hardened-runtime', 'platform': 'windows-x64',
        'codex_version': config['expected_codex_version'], 'build_date': '2026-11-12',
        'input_contracts': ['siemens-json/1', 'table-csv/1'], 'token_creation': 'after_full_preflight',
        'baseline_worker_sha256': sha(baseline_exe), 'task_helper_sha256': sha(helper),
        'includes_credentials': False, 'includes_jobs': False, 'includes_machine_sandbox_state': False,
        'model_defaults': {'model': 'deepseek/deepseek-v4.1-flash-1017', 'reasoning_effort': 'high'},
    }
    (app / 'package-manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'phase': 'prepared', 'stage': str(stage), 'worker_version': '0.3.0',
                      'baseline_worker_sha256': manifest['baseline_worker_sha256']}), flush=True)


if __name__ == '__main__':
    main()
