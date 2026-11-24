# Use the host's existing registered Codex runtime. Run in an elevated PowerShell 7.
param([ValidateRange(1,8)][int]$Slots = 5, [string]$LangflowAddress, [string]$BaseUrl)
$ErrorActionPreference = 'Stop'
$repo = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
$snapshot = Join-Path $repo 'deployments\worker8788'
$port = 8787
# These are the stable installation and task directories, including for an in-place upgrade.
$root = 'C:\CodexRemoteWorker-AppServer-8790'
$data = 'C:\CodexWorkerData-AppServer-8790'
$python = 'C:\CodexRemoteWorker\.runtime\python313\python.exe'
$mark = '.materials-worker'
if (Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue) {
    throw "Port $port is already occupied. Check the owning process before installing the materials Worker."
}
$instanceLock = Join-Path $data '.materials.lock'
if (Test-Path -LiteralPath $instanceLock) {
    try {
        $handle = [IO.File]::Open($instanceLock,'Open','ReadWrite','None')
        $handle.Dispose()
    } catch [IO.IOException] {
        throw 'The materials data directory is in use. Drain and stop the existing materials instance before updating.'
    }
}
$existing = @('C:\CodexRemoteWorker-AppServer-8789','C:\CodexRemoteWorker-AppServer-8788') |
    Where-Object { Test-Path -LiteralPath "$_\config.trial.json" } | Select-Object -First 1
if (!$existing -or !(Test-Path -LiteralPath $python)) {
    throw 'Expected a configured 8789 or 8788 Windows Worker installation and its bundled Python.'
}
# The 8788 trial file contains overrides; use its original loader to resolve the actual model and paths.
$readConfig = @'
import json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from codex_remote_worker.app_server_service import trial_settings
settings = trial_settings(Path(sys.argv[2]))
print(json.dumps(settings.model_dump(mode="json", exclude={"service_token", "provider_env"})))
'@
$publicConfig = & $python -X utf8 -c $readConfig "$snapshot\source\src" "$existing\config.trial.json"
if ($LASTEXITCODE) { throw "Could not read the existing Worker configuration in $existing." }
$cfg = $publicConfig | ConvertFrom-Json -AsHashtable
$existingData = $cfg.data_root
$office = @($env:MATERIALS_SOFFICE, "$env:ProgramFiles\LibreOffice\program\soffice.com",
    "$env:ProgramFiles\LibreOffice\program\soffice.exe") | Where-Object { $_ -and (Test-Path -LiteralPath $_ -PathType Leaf) } | Select-Object -First 1
if (!$office) { throw 'Install LibreOffice or set MATERIALS_SOFFICE to soffice.com first.' }
function Initialize-MaterialsDirectory([string]$Path, [string]$AclSource) {
    if (Test-Path -LiteralPath $Path) {
        if ((Get-Item -LiteralPath $Path).Attributes -band [IO.FileAttributes]::ReparsePoint -or
            !(Test-Path -LiteralPath (Join-Path $Path $mark))) {
            throw "Unrecognized directory: $Path"
        }
    } else {
        New-Item -ItemType Directory -Path $Path | Out-Null
        Set-Acl -LiteralPath $Path -AclObject (Get-Acl -LiteralPath $AclSource)
        New-Item -ItemType File -Path (Join-Path $Path $mark) | Out-Null
    }
}
Initialize-MaterialsDirectory $root $existing
Initialize-MaterialsDirectory $data $existingData
$private = New-Item -Force -ItemType Directory -Path "$data\private"
$credential = Join-Path $private.FullName 'credentials.local.json'
if (!(Test-Path -LiteralPath $credential)) {
    $bytes = [Security.Cryptography.RandomNumberGenerator]::GetBytes(48)
    @{CODEX_WORKER_SERVICE_TOKEN=[Convert]::ToBase64String($bytes)} |
        ConvertTo-Json | Set-Content -LiteralPath $credential -Encoding utf8NoBOM
}
$cfg.protected_read_roots = @($cfg.protected_read_roots | Where-Object {$_}) + @($existingData,$root)
$cfg.port = $port
$cfg.bind_host = '0.0.0.0'
$cfg.data_root = $data
$cfg.max_concurrent_jobs = $Slots
$cfg.task_batch_concurrency = 1
$cfg | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath "$root\config.trial.json" -Encoding utf8NoBOM
"$existing\config.trial.json" | Set-Content -LiteralPath "$root\model-instance.path" -Encoding utf8NoBOM
$office | Set-Content -LiteralPath "$root\office.path" -Encoding utf8NoBOM

$vendor = New-Item -Force -ItemType Directory -Path "$root\vendor"
$readExtras = "import sys,tomllib; print(*tomllib.load(open(sys.argv[1],'rb'))['project']['optional-dependencies']['materials'],sep=chr(10))"
$requirements = @(& $python -X utf8 -c $readExtras "$snapshot\source\pyproject.toml")
if ($LASTEXITCODE -or !$requirements.Count) { throw 'Unable to read materials dependencies.' }
if (Get-Command uv -ErrorAction SilentlyContinue) {
    & uv pip install --python $python --target $vendor.FullName @requirements
} elseif (Get-Command py -ErrorAction SilentlyContinue) {
    & py -3 -m pip install --only-binary=:all: --platform win_amd64 --implementation cp --python-version 3.13 --abi cp313 --upgrade --target $vendor.FullName @requirements
} else {
    & $python -m pip install --only-binary=:all: --upgrade --target $vendor.FullName @requirements
}
if ($LASTEXITCODE) { throw 'Dependency install failed. Install uv or a Python with pip and retry.' }
# pip can move user-private staging ACLs into vendor on Windows.
& icacls.exe $vendor.FullName /inheritance:e /T /Q | Out-Null
if ($LASTEXITCODE) { throw 'Unable to apply dependency directory inheritance for the service account.' }
$package = New-Item -Force -ItemType Directory -Path "$root\src\codex_remote_worker"
Get-ChildItem -LiteralPath "$snapshot\source\src\codex_remote_worker" -Filter '*.py' -File |
    Copy-Item -Destination $package.FullName -Force
$desktop = New-Item -Force -ItemType Directory -Path "$root\desktop"
# Reuse the registered-package activation and service-account launcher used by 8789.
$launch = [IO.File]::ReadAllText("$snapshot\runtime\launch-packaged-user.ps1").Replace('C:\CodexRemoteWorker-AppServer-8788',$root)
[IO.File]::WriteAllText("$root\launch-packaged-user.ps1",$launch)
$start = [IO.File]::ReadAllText("$repo\packaging\worker8789\start-worker.ps1")
$start = $start.Replace('C:\CodexRemoteWorker-AppServer-8789',$root).Replace('C:\CodexWorkerData-AppServer-8789',$data).Replace('8789',[string]$port)
[IO.File]::WriteAllText("$desktop\start-worker.ps1",$start)
Copy-Item -LiteralPath "$PSScriptRoot\packaged-background.py" -Destination "$root\packaged-background.py" -Force
$ruleName = "Codex-Materials-$port"
$rules = @(Get-NetFirewallRule -Name @('Codex-Materials-8790', $ruleName) -ErrorAction SilentlyContinue)
if ($rules.Count) {
    foreach ($rule in $rules) {
        $rule | Get-NetFirewallPortFilter | Set-NetFirewallPortFilter -Protocol TCP -LocalPort $port | Out-Null
        if ($LangflowAddress) {
            Set-NetFirewallRule -Name $rule.Name -RemoteAddress $LangflowAddress | Out-Null
        }
    }
} elseif ($LangflowAddress) {
    New-NetFirewallRule -Name $ruleName -DisplayName $ruleName -Direction Inbound -Action Allow -Protocol TCP -LocalPort $port -RemoteAddress $LangflowAddress | Out-Null
}
& "$PSScriptRoot\install-desktop.ps1" -BaseUrl $BaseUrl
Write-Output "Installed independent materials Worker at $root. Open Worker8787.exe on the desktop to start it."
