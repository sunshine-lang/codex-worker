# Upgrade an already installed development service, from an administrator PowerShell 7 session.
$ErrorActionPreference = 'Stop'
$data = 'C:\CodexWorkerData-AppServer-8789'
$root = 'C:\CodexRemoteWorker-AppServer-8789'
$python = 'C:\CodexRemoteWorker\.runtime\python313\python.exe'
$recovery = 'CodexWorker8789-Recovery'
if (!(Test-Path -LiteralPath (Join-Path $root '.worker8789')) -or
    !(Test-Path -LiteralPath (Join-Path $data '.worker8789'))) { throw 'Expected the installed 8789 instance.' }
& (Join-Path $PSScriptRoot 'build.ps1')
# Refuse overlapping maintenance. Hold this marker across installation to block new orders.
$marker = Join-Path $data '.maintenance.json'
[IO.File]::Open($marker, 'CreateNew', 'Write', 'None').Dispose()
$installStarted = $false
$installed = $false
try {
    Disable-ScheduledTask -TaskName $recovery | Out-Null
    $launchLock = [IO.File]::Open((Join-Path $data '.launch-control.lock'), 'OpenOrCreate', 'ReadWrite', 'None')
    try {
        # This helper checks both task stores and verifies ownership of every stopped process.
        Push-Location $PSScriptRoot
        try {
            & $python -c "import sys; sys.path.insert(0, '.'); import settings8789 as w; w.assert_idle(); w.stop_owned()"
            if ($LASTEXITCODE -ne 0) { throw '8789 did not confirm idle shutdown. Check the message above.' }
        } finally { Pop-Location }
    } finally { $launchLock.Dispose() }
    $installStarted = $true
    & (Join-Path $PSScriptRoot 'install.ps1')
    $installed = $true
} finally {
    if ($installStarted -and !$installed) {
        Disable-ScheduledTask -TaskName $recovery | Out-Null
        Write-Warning 'The 8789 installation was interrupted. Correct the installation error before restarting its watchdog.'
    } else {
        Enable-ScheduledTask -TaskName $recovery | Out-Null
    }
    Remove-Item -LiteralPath $marker
}
Start-ScheduledTask -TaskName $recovery
