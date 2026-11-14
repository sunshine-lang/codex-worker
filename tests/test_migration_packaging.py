import json
import os
from pathlib import Path
import subprocess
import sys
import tomllib
import zipfile

import pytest

PROJECT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('relative', [
    'app/Data/jobs.sqlite3', 'app/Data/task.txt', 'app/data/task.txt', 'app/private/key.txt',
    'app/.codex/config.toml', 'app/credentials.local.json', 'app/install-plan.json',
    'app/bootstrap-credentials.local.json', 'app/preflight.json',
])
def test_installer_builder_rejects_machine_state(tmp_path, relative):
    stage = tmp_path / 'stage'
    path = stage / relative
    path.parent.mkdir(parents=True)
    path.write_text('synthetic data', encoding='utf-8')
    stub = tmp_path / 'stub.exe'
    stub.write_bytes(b'synthetic stub')
    output = tmp_path / 'Setup.exe'
    done = subprocess.run([sys.executable, str(PROJECT / 'scripts/build-installer-payload.py'),
                           '--stage', str(stage), '--stub', str(stub), '--output', str(output)],
                          capture_output=True, timeout=10)
    assert done.returncode != 0 and not output.exists()
    assert b'Refusing to package' in done.stderr


def test_installer_payload_excludes_bytecode_and_preserves_safe_files(tmp_path):
    stage = tmp_path / 'stage'
    app = stage / 'app'
    app.mkdir(parents=True)
    (app / 'readme.txt').write_text('synthetic distribution', encoding='utf-8')
    (app / 'cache.pyc').write_bytes(b'not distributed')
    (app / '.DS_Store').write_bytes(b'not distributed')
    stub = tmp_path / 'stub.exe'
    stub.write_bytes(b'synthetic stub')
    output = tmp_path / 'Setup.exe'
    subprocess.run([sys.executable, str(PROJECT / 'scripts/build-installer-payload.py'), '--stage', str(stage),
                    '--stub', str(stub), '--output', str(output)], check=True, capture_output=True, timeout=10)
    with zipfile.ZipFile(output) as package:
        assert package.namelist() == ['app/readme.txt']
        assert package.read('app/readme.txt') == b'synthetic distribution'


def test_installer_scripts_use_real_network_filter_filename():
    for name in ('start-worker.ps1', 'configure-install.ps1', 'uninstall.ps1'):
        text = (PROJECT / 'packaging/installer' / name).read_text(encoding='utf-8-sig')
        assert 'wfp -ne twork' not in text
        assert 'wfp-network-lockdown.py' in text


def test_migration_verifies_json_and_uses_configured_paths():
    setup = (PROJECT / 'packaging/installer/configure-install.ps1').read_text(encoding='utf-8-sig')
    start = (PROJECT / 'packaging/installer/start-worker.ps1').read_text(encoding='utf-8-sig')
    helper = (PROJECT / 'packaging/installer/verify-json-runtime.py').read_text(encoding='utf-8')
    assert '-Mode VerifyJson -CredentialsFile $bootstrapPath' in setup
    assert 'PACKAGE_CONTRACT_MISMATCH' in setup and "DisplayVersion='0.4.0'" in setup
    assert 'verify-json-runtime.py' in start and 'UseSystemPassword' not in start
    assert 'C:\\CodexRemoteWorker' not in start and 'C:\\CodexRemoteWorker' not in helper
    assert 'args.config, args.credentials_file' in helper


def test_installer_model_configuration_never_embeds_a_model_key(tmp_path):
    source = tmp_path / 'input.json'
    target = tmp_path / 'config.toml'
    source.write_text(json.dumps({'model': 'deepseek/deepseek-v4.1-flash-1017',
                                 'base_url': 'https://model.example/v1',
                                 'model_api_key': 'synthetic-secret-not-for-config'}), encoding='utf-8')
    subprocess.run([sys.executable, str(PROJECT / 'packaging/installer/write_model_config.py'),
                    str(source), str(target)], check=True, capture_output=True, timeout=10)
    text = target.read_text(encoding='utf-8')
    config = tomllib.loads(text)
    assert 'synthetic-secret-not-for-config' not in text
    assert config['model'] == 'deepseek/deepseek-v4.1-flash-1017'
    assert config['model_providers']['custom']['env_key'] == 'CODEX_WORKER_PROVIDER_API_KEY'
    assert config['model_reasoning_effort'] == 'high'


@pytest.mark.skipif(os.name != 'nt', reason='PowerShell command construction test is Windows-only')
@pytest.mark.parametrize('mode', ['Doctor', 'Probe', 'VerifyJson', 'Serve'])
def test_installer_start_modes_are_portable_and_hidden(tmp_path, mode):
    powershell = PROJECT / 'build/migration-stage-0.3.0/app/.runtime/powershell/pwsh.exe'
    if not powershell.is_file():
        pytest.skip('staged bundled PowerShell is not available')
    root = tmp_path / '中文 portable root'
    runtime = root / '.runtime'
    runtime.mkdir(parents=True)
    script = runtime / 'start-worker.local.ps1'
    script.write_bytes((PROJECT / 'packaging/installer/start-worker.ps1').read_bytes())
    data = root / 'Data'
    private = data / 'private'
    private.mkdir(parents=True)
    worker = root / 'bin/CodexRemoteWorker/CodexRemoteWorker.exe'
    worker.parent.mkdir(parents=True)
    worker.write_bytes(b'fixture only; never executed')
    config = {'data_root': str(data), 'codex_home': str(root / 'fixture-profile/.codex'),
              'tool_python': str(runtime / 'python313/python.exe'),
              'doc_converter': str(root / 'tools/LibreOffice/program/soffice.exe'),
              'codex_executable': str(root / 'tools/codex/bin/codex.exe'), 'port': 19887}
    (root / 'config.local.json').write_text(json.dumps(config), encoding='utf-8')
    (private / 'service-account.local.json').write_text(json.dumps({
        'username': 'SYNTHETIC\\FixtureWorker', 'password': 'Fixture-only-Aa1!'}), encoding='utf-8')
    calls = root / 'calls.json'
    def quote(path):
        return "'" + str(path).replace("'", "''") + "'"
    mock = r"""
$ErrorActionPreference='Stop'
$global:launches=[Collections.Generic.List[object]]::new()
function Start-Process {
 param($FilePath,$ArgumentList,$Credential,[switch]$LoadUserProfile,$Environment,$WorkingDirectory,$WindowStyle,$RedirectStandardOutput,$RedirectStandardError,[switch]$PassThru)
 if ($WindowStyle -ne 'Hidden') { throw 'Helper was not hidden' }
 if ($ArgumentList -notmatch '"--config"' -or $ArgumentList -notmatch '"--credentials-file"') { throw 'Arguments not quoted' }
 $global:launches.Add([pscustomobject]@{executable=$FilePath;arguments=$ArgumentList;cwd=$WorkingDirectory;window_style=$WindowStyle})
 [IO.File]::WriteAllText($RedirectStandardOutput,'{"passed":true,"ready":true}')
 [IO.File]::WriteAllText($RedirectStandardError,'')
 return [pscustomobject]@{Id=2147483000;StartTime=[DateTime]::UtcNow;ExitCode=0}
}
function Get-Process { param($Id,$ErrorAction); return $null }
"""
    command = mock + '\n& ' + quote(script) + ' -Mode ' + mode
    if mode == 'Serve':
        command += ' -VerifiedStartup'
    command += '\n$global:launches.ToArray() | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath ' + quote(calls) + ' -Encoding utf8'
    done = subprocess.run([str(powershell), '-NoLogo', '-NoProfile', '-NonInteractive', '-Command', command],
                          capture_output=True, timeout=25)
    assert done.returncode == 0, done.stderr.decode('utf-8', errors='replace')
    recorded = json.loads(calls.read_text(encoding='utf-8-sig'))
    if isinstance(recorded, dict):
        recorded = [recorded]
    assert recorded and all(item['window_style'] == 'Hidden' for item in recorded)
    assert all(item['cwd'] == str(root) for item in recorded)
    assert all(str(root) in item['arguments'] for item in recorded)
    if mode == 'VerifyJson':
        assert 'verify-json-runtime.py' in recorded[-1]['arguments']
    if mode == 'Serve':
        assert len(recorded) == 2 and '"serve"' in recorded[-1]['arguments']
