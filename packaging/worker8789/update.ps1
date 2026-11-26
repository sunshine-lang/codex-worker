# Upgrade only an installed 8789 development worker.
$ErrorActionPreference = 'Stop'
$data = 'C:\CodexWorkerData-AppServer-8789'
$root = 'C:\CodexRemoteWorker-AppServer-8789'
$python = 'C:\CodexRemoteWorker\.runtime\python313\python.exe'
$recovery = 'CodexWorker8789-Recovery'
$config = Get-Content -Raw -LiteralPath (Join-Path $root 'config.trial.json') | ConvertFrom-Json
if ($config.port -ne 8789 -or $config.data_root -ne $data -or
    $config.codex_home -ne (Join-Path $data 'private\codex-home')) { throw 'Unexpected 8789 configuration.' }

function Switch-DevPackage([string]$Incoming, [string]$Previous) {
    $marker = Join-Path $data '.maintenance.json'
    $package = Join-Path $root 'src\codex_remote_worker'
    # CreateNew serializes updaters and blocks new tasks. Recheck both stores under the launch lock.
    [IO.File]::Open($marker,'CreateNew','Write','None').Dispose()
    try {
        Disable-ScheduledTask -TaskName $recovery | Out-Null
        $lock = [IO.File]::Open((Join-Path $data '.launch-control.lock'),'OpenOrCreate','ReadWrite','None')
        try {
            Push-Location $PSScriptRoot
            try {
                & $python -B -c "import settings8789 as w; w.assert_idle(); w.stop_owned()"
                if ($LASTEXITCODE -ne 0) { throw 'No idle 8789 shutdown confirmation; current runtime retained.' }
            } finally { Pop-Location }
            if (Get-NetTCPConnection -LocalPort 8789 -State Listen -ErrorAction SilentlyContinue) { throw '8789 still has a listener.' }
            [IO.Directory]::Move($package,$Previous)
            try { [IO.Directory]::Move($Incoming,$package) }
            catch { [IO.Directory]::Move($Previous,$package); throw }
        } finally { $lock.Dispose() }
    } finally {
        Remove-Item -LiteralPath $marker
        Enable-ScheduledTask -TaskName $recovery | Out-Null
        Start-ScheduledTask -TaskName $recovery
    }
}

$stage = & (Join-Path $PSScriptRoot 'stage-and-swap.ps1') -Python $python
if (!(Test-Path -LiteralPath (Join-Path $stage 'worker_materials.py'))) { throw 'Missing staged 8789 package.' }
$id = [guid]::NewGuid().ToString('N')
$backup = Join-Path $root "src\previous-8789-$id"
try { Switch-DevPackage $stage $backup }
finally { if (Test-Path -LiteralPath $stage) { Remove-Item -LiteralPath $stage -Force -Recurse } }
$verified = $false
try {
    & $python -B (Join-Path $PSScriptRoot 'order_materials\verify_installed.py')
    $verified = $LASTEXITCODE -eq 0
} catch { Write-Warning $_ }
if (!$verified) {
    Write-Warning "8789 startup verification failed. Restoring previous runtime if idle: $backup"
    Switch-DevPackage $backup (Join-Path $root "src\failed-8789-$id")
    throw 'Previous 8789 runtime restored; do not publish the new development Flow.'
}
Write-Host "8789 update verified. Previous runtime: $backup"
