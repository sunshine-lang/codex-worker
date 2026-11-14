"""Run real PowerShell/ZIP operations with account management mocked; never create users."""
import base64
import hashlib
import json
import os
from pathlib import Path
import subprocess
import zipfile

import pytest


PROJECT = Path(__file__).resolve().parents[1]
SCRIPT = PROJECT / "scripts/Configure-CodexWorkerUser.ps1"
POWERSHELL = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
pytestmark = pytest.mark.skipif(os.name != "nt" or not POWERSHELL.is_file(), reason="Windows PowerShell required")


def quote(value):
    return "'" + str(value).replace("'", "''") + "'"


def run_ps(body):
    code = "[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false); . " + quote(SCRIPT) + ";\n" + body
    encoded = base64.b64encode(code.encode("utf-16-le")).decode("ascii")
    result = subprocess.run([str(POWERSHELL), "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
        stdin=subprocess.DEVNULL, capture_output=True, encoding="utf-8-sig", timeout=30,
        env={**os.environ, "PSModulePath": str(POWERSHELL.parent / "Modules")})
    assert result.returncode == 0, result.stderr
    return result.stdout


@pytest.fixture
def packet(tmp_path):
    path = tmp_path / "fixture.zip"
    names = ["CodexWorker.exe", "portable.py", "worker-settings.json", ".runtime/python313/python.exe",
        "bin/CodexRemoteWorker/CodexRemoteWorker.exe", "internal/verify-json-runtime.py", "package-manifest.json"]
    with zipfile.ZipFile(path, "w") as archive:
        for name in names:
            archive.writestr("CodexWorker-Portable/" + name, "fixture only; never executed")
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


def test_whatif_never_requests_password_changes_account_or_creates_folder(tmp_path):
    target = tmp_path / "preview" / "Worker"
    output = run_ps("""
function Assert-WorkerSetupEnvironment { throw 'must not inspect elevation in preview' }
function New-LocalUser { throw 'must not create a user in preview' }
function Read-WorkerAccountPassword { throw 'must not prompt in preview' }
Invoke-CodexWorkerUserSetup -UserName 'FixtureWorker' -InstallRoot """ + quote(target) + " -AllowRemoteDesktop -WhatIf")
    assert "FixtureWorker" in output and not target.parent.exists()


def test_wrong_package_hash_rejected_before_account_management(tmp_path, packet):
    path, _ = packet
    target = tmp_path / "wrong-hash" / "Worker"
    result = run_ps("""
function Assert-WorkerSetupEnvironment {}
function Get-LocalUser { throw 'must not inspect or create accounts before package verification' }
try {
 Invoke-CodexWorkerUserSetup -PackageZip """ + quote(path) + " -ExpectedSha256 " + quote("0" * 64) + " -InstallRoot " + quote(target) + """
 throw 'unexpected success'
} catch { if ($_.Exception.Message -notmatch 'SHA-256') { throw }; 'EXPECTED_REJECTION' }
""")
    assert "EXPECTED_REJECTION" in result and not target.parent.exists()


@pytest.mark.parametrize("extra", ["../escape", "CodexWorker-Portable/../../escape", "CodexWorker-Portable/.. /escape",
    "CodexWorker-Portable/file.txt:stream", "CodexWorker-Portable/NUL.txt", "CodexWorker-Portable/portable.py"])
def test_archive_unsafe_or_duplicate_windows_paths_rejected(tmp_path, packet, extra):
    path, _ = packet
    with zipfile.ZipFile(path, "a") as archive:
        archive.writestr(extra, "untrusted fixture")
    code = """
Add-Type -AssemblyName System.IO.Compression.FileSystem
$archive=[IO.Compression.ZipFile]::OpenRead(""" + quote(path) + ")\n" + """
try {
 try { $null=Assert-WorkerArchiveEntries $archive """ + quote(tmp_path / "extract") + """; throw 'unexpected success' }
 catch { if ($_.Exception.Message -eq 'unexpected success') { throw }; 'EXPECTED_REJECTION' }
} finally { $archive.Dispose() }
"""
    assert "EXPECTED_REJECTION" in run_ps(code)
    assert not (tmp_path / "extract").exists()


@pytest.mark.parametrize("state", ["builtin", "administrator", "disabled"])
def test_existing_privileged_or_disabled_account_is_not_repurposed(state):
    rid = "500" if state == "builtin" else "1007"
    enabled = "$false" if state == "disabled" else "$true"
    members = "return @($script:fixtureAccount)" if state == "administrator" else "return @()"
    code = "$script:fixtureAccount=[pscustomobject]@{SID=[Security.Principal.SecurityIdentifier]::new('S-1-5-21-111-222-333-" + rid + "');Enabled=" + enabled + "}\n"
    code += "function Get-WorkerGroupMembers { " + members + " }\n"
    code += "try { Assert-StandardWorkerAccount $script:fixtureAccount; throw 'unexpected success' } catch { if($_.Exception.Message -eq 'unexpected success'){throw};'EXPECTED_REJECTION' }"
    assert "EXPECTED_REJECTION" in run_ps(code)


def test_directory_acl_limits_modify_to_the_worker_user(tmp_path):
    code = """
function Set-Acl { param($LiteralPath,$AclObject); $script:capturedAcl=$AclObject }
Set-WorkerDirectoryAcl -Path """ + quote(tmp_path) + """ -UserSid 'S-1-5-21-111-222-333-1007'
$rules=$script:capturedAcl.GetAccessRules($true,$true,[Security.Principal.SecurityIdentifier])
@($rules | ForEach-Object { [pscustomobject]@{sid=$_.IdentityReference.Value;rights=$_.FileSystemRights.ToString()} }) | ConvertTo-Json -Compress
"""
    rules = json.loads(run_ps(code))
    assert {x["sid"] for x in rules} == {"S-1-5-18", "S-1-5-32-544", "S-1-5-21-111-222-333-1007"}
    assert next(x for x in rules if x["sid"].endswith("-1007"))["rights"] == "Modify, Synchronize"


def test_real_zip_deployment_and_repeat_run_preserve_existing_data(tmp_path, packet):
    path, checksum = packet
    root = tmp_path / "配置 & setup" / "FixtureWorker"
    code = """
function Assert-WorkerSetupEnvironment {}
function Initialize-WorkerParent { param($Path); $null=[IO.Directory]::CreateDirectory($Path) }
function Set-WorkerDirectoryAcl { param($Path,$UserSid,[switch]$ParentOnly) }
function Write-WorkerDesktopShortcut { param($Root,$AccountName); return (Join-Path $Root 'fixture.lnk') }
$script:fixtureAccount=$null
$script:created=0
$script:groups=@{}
function Get-LocalUser { param($Name,$ErrorAction); return $script:fixtureAccount }
function New-LocalUser {
 param($Name,$Password,$FullName,$Description,$ErrorAction)
 $script:created++
 $script:fixtureAccount=[pscustomobject]@{SID=[Security.Principal.SecurityIdentifier]::new('S-1-5-21-111-222-333-1007');Enabled=$true}
 return $script:fixtureAccount
}
function Get-WorkerGroupMembers { param($GroupSid); if($script:groups.ContainsKey($GroupSid)){return @($script:groups[$GroupSid])};return @() }
function Add-LocalGroupMember { param($SID,$Member,$ErrorAction); $script:groups[$SID.Value]=@($Member) }
function Read-WorkerAccountPassword { throw 'must not ask for an existing account password' }
$password=ConvertTo-SecureString 'Synthetic-only-Password1!' -AsPlainText -Force
"""
    args = " -UserName FixtureWorker -PackageZip " + quote(path) + " -ExpectedSha256 " + quote(checksum) + " -InstallRoot " + quote(root) + " -AllowRemoteDesktop"
    code += "Invoke-CodexWorkerUserSetup" + args + " -Password $password\n"
    code += "$null=[IO.Directory]::CreateDirectory(" + quote(root / "Data") + ")\n"
    code += "[IO.File]::WriteAllText(" + quote(root / "Data/keep.txt") + ",'existing task data')\n"
    code += "Invoke-CodexWorkerUserSetup" + args + "\n"
    code += "if($script:created -ne 1){throw 'account recreated'}\n"
    code += "if($script:groups.Count -ne 2 -or -not $script:groups.ContainsKey('S-1-5-32-545') -or -not $script:groups.ContainsKey('S-1-5-32-555')){throw 'unexpected group changes'}\n'WORKFLOW_PASSED'"
    assert "WORKFLOW_PASSED" in run_ps(code)
    assert (root / "Data/keep.txt").read_text() == "existing task data"
    assert (root / "CodexWorker.exe").read_text() == "fixture only; never executed"
    state = json.loads((root / ".codex-worker-user-setup.json").read_text(encoding="utf-8-sig"))
    assert state["user_sid"].endswith("-1007") and state["codex_login_checked"] is False
    assert state["sandbox_checked"] is False


def test_existing_unrelated_directory_is_preserved(tmp_path, packet):
    path, checksum = packet
    root = tmp_path / "unrelated"
    root.mkdir()
    (root / "keep.txt").write_text("untouched")
    code = """
function Assert-WorkerSetupEnvironment {}
function Get-LocalUser { return [pscustomobject]@{SID=[Security.Principal.SecurityIdentifier]::new('S-1-5-21-111-222-333-1007');Enabled=$true} }
function Get-WorkerGroupMembers { return @() }
function New-LocalUser { throw 'must not change accounts' }
try {
 Invoke-CodexWorkerUserSetup -UserName FixtureWorker -PackageZip """ + quote(path) + " -ExpectedSha256 " + quote(checksum) + " -InstallRoot " + quote(root) + """
 throw 'unexpected success'
} catch { if($_.Exception.Message -notmatch '目标目录已存在'){throw};'EXPECTED_REJECTION' }
"""
    assert "EXPECTED_REJECTION" in run_ps(code)
    assert (root / "keep.txt").read_text() == "untouched"


def test_desktop_shortcut_points_to_portable_exe_without_elevation(tmp_path):
    root = tmp_path / "Worker 中文"
    root.mkdir()
    (root / "CodexWorker.exe").write_text("fixture only; never executed")
    output = run_ps("$path=Write-WorkerDesktopShortcut -Root " + quote(root) + " -AccountName FixtureWorker -DesktopDirectory " + quote(tmp_path) + "; [pscustomobject]@{target=[CodexWorkerUserSetup.Shortcuts]::Target($path);cwd=[CodexWorkerUserSetup.Shortcuts]::Directory($path)}|ConvertTo-Json -Compress")
    link = json.loads(output)
    assert link["target"] == str(root / "CodexWorker.exe") and link["cwd"] == str(root)
    data = (tmp_path / "Codex Worker - FixtureWorker.lnk").read_bytes()
    # Shell link flags bit 0x2000 requests elevation; the setup must not set it.
    assert not int.from_bytes(data[20:24], "little") & 0x2000
