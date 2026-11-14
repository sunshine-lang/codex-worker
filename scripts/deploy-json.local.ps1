$ErrorActionPreference = 'Stop'
$sourceRoot = Split-Path $PSScriptRoot -Parent
$installRoot = 'C:\CodexRemoteWorker'
$dataRoot = 'C:\CodexWorkerData'
$python = Join-Path $installRoot '.runtime\python313\python.exe'
$powerShell = Join-Path $installRoot '.runtime\powershell\pwsh.exe'
$buildRoot = Join-Path $sourceRoot 'dist\windows-exe\CodexRemoteWorker'
$installedBin = Join-Path $installRoot 'bin\CodexRemoteWorker'
$backup = Join-Path $dataRoot ('maintenance-backups\json-0.3.0-' + [DateTimeOffset]::UtcNow.ToString('yyyyMMddTHHmmssZ'))
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
if (-not ([Security.Principal.WindowsPrincipal]::new($identity)).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) { throw 'Administrator deployment session required.' }
if (-not (Test-Path -LiteralPath (Join-Path $buildRoot 'CodexRemoteWorker.exe'))) { throw 'Candidate EXE missing.' }
$controlLock = [IO.File]::Open((Join-Path $dataRoot '.desktop-control.lock'),'OpenOrCreate','ReadWrite','None')
$maintenancePath = Join-Path $dataRoot '.maintenance.json'
$operationId = [Guid]::NewGuid().ToString('N')
$owned = $false
$changed = $false
$files = [Collections.Generic.List[object]]::new()
try {
    if (Test-Path -LiteralPath $maintenancePath) { throw 'Another maintenance operation is active.' }
    @{reason='json_protocol_deployment';operation_id=$operationId} | ConvertTo-Json -Compress | Set-Content -LiteralPath $maintenancePath -Encoding utf8
    $owned = $true
    $count = & $python -c 'import sqlite3; c=sqlite3.connect("file:C:/CodexWorkerData/jobs.sqlite3?mode=ro",uri=True); print(c.execute("select count(*) from jobs where status in (?,?,?,?)",("queued","running","checking","cancelling")).fetchone()[0])'
    if ($LASTEXITCODE -ne 0 -or [string]$count -notmatch '^\d+$' -or [int]$count -ne 0) { throw 'Tasks are active or unreadable; no Worker files changed.' }
    New-Item -ItemType Directory -Path $backup -Force | Out-Null
    Copy-Item -LiteralPath $installedBin -Destination (Join-Path $backup 'CodexRemoteWorker') -Recurse
    foreach($name in @('__init__.py','common.py','files.py','qa.py','app.py','json_delivery.py','task_runtime.py','task_tools.py')) {
        foreach($relative in @('src\codex_remote_worker','.runtime\python313\Lib\site-packages\codex_remote_worker')) {
            $directory = Join-Path $installRoot $relative
            if (-not (Test-Path -LiteralPath $directory)) { continue }
            $target = Join-Path $directory $name
            $saved = Join-Path $backup ($relative + '\' + $name)
            $exists = Test-Path -LiteralPath $target
            if ($exists) {
                New-Item -ItemType Directory -Path (Split-Path $saved -Parent) -Force | Out-Null
                Copy-Item -LiteralPath $target -Destination $saved
            }
            $files.Add([pscustomobject]@{Source=(Join-Path $sourceRoot ('src\codex_remote_worker\' + $name));Target=$target;Backup=$saved;Existed=$exists})
        }
    }
    $pyproject = Join-Path $installRoot 'pyproject.toml'
    Copy-Item -LiteralPath $pyproject -Destination (Join-Path $backup 'pyproject.toml')
    $files.Add([pscustomobject]@{Source=(Join-Path $sourceRoot 'pyproject.toml');Target=$pyproject;Backup=(Join-Path $backup 'pyproject.toml');Existed=$true})
    $unchanged = @((Join-Path $installRoot 'config.local.json'),(Join-Path $installRoot 'bin\CodexWorkerLauncher.exe'),'C:\Users\Administrator\Desktop\Codex 服务启动器.exe',(Join-Path $dataRoot 'private\credentials.local.json'))
    $beforeHashes = @{}
    foreach($path in $unchanged) { $beforeHashes[$path]=(Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash }
    $safeStop = @"
import sqlite3, subprocess, sys
with sqlite3.connect(sys.argv[1], isolation_level=None, timeout=10) as db:
    db.execute('BEGIN IMMEDIATE')
    try:
        states=('queued','running','checking','cancelling')
        if db.execute('select count(*) from jobs where status in (?,?,?,?)',states).fetchone()[0]:
            raise RuntimeError('A task became active; Worker was not stopped')
        subprocess.run([sys.argv[2],'-NoLogo','-NoProfile','-NonInteractive','-WindowStyle','Hidden','-File',sys.argv[3]],check=True)
    finally:
        db.rollback()
print('WORKER_STOPPED_UNDER_SCHEDULER_LOCK')
"@
    & $python -X utf8 -c $safeStop (Join-Path $dataRoot 'jobs.sqlite3') $powerShell (Join-Path $installRoot 'scripts\desktop-stop.ps1')
    if ($LASTEXITCODE -ne 0) { throw 'Worker did not stop safely; installation unchanged.' }
    $changed = $true
    foreach($entry in $files) { Copy-Item -LiteralPath $entry.Source -Destination $entry.Target -Force }
    foreach($file in Get-ChildItem -LiteralPath $buildRoot -Force) { Copy-Item -LiteralPath $file.FullName -Destination $installedBin -Recurse -Force }
    $hashes = Get-FileHash -LiteralPath (Join-Path $buildRoot 'CodexRemoteWorker.exe'),(Join-Path $installedBin 'CodexRemoteWorker.exe') -Algorithm SHA256
    if ($hashes[0].Hash -ne $hashes[1].Hash) { throw 'Installed EXE hash mismatch.' }
    foreach($path in $unchanged) {
        if ((Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash -ne $beforeHashes[$path]) { throw 'An unrelated protected file changed.' }
    }
    [ordered]@{deployed_at=[DateTimeOffset]::UtcNow.ToString('o');version='0.3.0';variant='json-on-hardened-runtime';backup=$backup;exe_sha256=$hashes[1].Hash;launcher_and_credentials_preserved=$true;model_called=$false} | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $backup 'deployment.json') -Encoding utf8
    [pscustomobject]@{Deployed=$true;Version='0.3.0';Backup=$backup;LauncherPreserved=$true;NeedsStart=$true} | ConvertTo-Json -Compress
} catch {
    if ($changed) {
        foreach($entry in $files) {
            if ($entry.Existed) { Copy-Item -LiteralPath $entry.Backup -Destination $entry.Target -Force }
            elseif (Test-Path -LiteralPath $entry.Target) { Remove-Item -LiteralPath $entry.Target -Force }
        }
        foreach($file in Get-ChildItem -LiteralPath (Join-Path $backup 'CodexRemoteWorker') -Force) { Copy-Item -LiteralPath $file.FullName -Destination $installedBin -Recurse -Force }
        Write-Warning 'Candidate copy failed; previous Worker files were restored.'
    }
    throw
} finally {
    if ($owned -and (Test-Path -LiteralPath $maintenancePath)) {
        $marker = Get-Content -LiteralPath $maintenancePath -Raw | ConvertFrom-Json
        if ($marker.operation_id -eq $operationId) { Remove-Item -LiteralPath $maintenancePath -Force }
    }
    $controlLock.Dispose()
}
