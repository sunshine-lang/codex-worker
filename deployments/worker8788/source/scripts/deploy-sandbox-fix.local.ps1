$ErrorActionPreference = 'Stop'
$sourceRoot = Split-Path $PSScriptRoot -Parent
$installRoot = 'C:\CodexRemoteWorker'
$dataRoot = 'C:\CodexWorkerData'
$powerShell = Join-Path $installRoot '.runtime\powershell\pwsh.exe'
$python = Join-Path $installRoot '.runtime\python313\python.exe'
$desktopLauncher = 'C:\Users\Administrator\Desktop\Codex 服务启动器.exe'
$stamp = [DateTimeOffset]::UtcNow.ToString('yyyyMMddTHHmmssZ')
$backup = Join-Path $dataRoot ('maintenance-backups\sandbox-fix-' + $stamp)
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
if (-not ([Security.Principal.WindowsPrincipal]::new($identity)).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) { throw 'Administrator deployment session required.' }
$controlLock = [IO.File]::Open((Join-Path $dataRoot '.desktop-control.lock'),'OpenOrCreate','ReadWrite','None')
$maintenancePath = Join-Path $dataRoot '.maintenance.json'
$maintenanceId = [Guid]::NewGuid().ToString('N')
$maintenanceOwned = $false
try {
if (Test-Path -LiteralPath $maintenancePath) { throw 'Another maintenance operation is active.' }
@{reason='runtime_deployment';operation_id=$maintenanceId} | ConvertTo-Json -Compress | Set-Content -LiteralPath $maintenancePath -Encoding utf8
$maintenanceOwned = $true
$count = & $python -c 'import sqlite3; c=sqlite3.connect("file:C:/CodexWorkerData/jobs.sqlite3?mode=ro",uri=True); print(c.execute("select count(*) from jobs where status in (?,?,?,?)",("queued","running","checking","cancelling")).fetchone()[0])'
if ($LASTEXITCODE -ne 0 -or [string]$count -notmatch '^\d+$' -or [int]$count -ne 0) { throw 'Active jobs or unreadable task state; deployment not started.' }
$workerBuild = Join-Path $sourceRoot 'dist\windows-exe\CodexRemoteWorker'
$launcherBuild = Join-Path $sourceRoot 'dist\windows-exe\CodexWorkerLauncher.exe'
if (-not (Test-Path -LiteralPath (Join-Path $workerBuild 'CodexRemoteWorker.exe')) -or -not (Test-Path -LiteralPath $launcherBuild)) { throw 'Build output missing.' }
New-Item -ItemType Directory -Path $backup -Force | Out-Null
Copy-Item -LiteralPath (Join-Path $installRoot 'bin\CodexRemoteWorker') -Destination (Join-Path $backup 'CodexRemoteWorker') -Recurse
foreach($item in @(@((Join-Path $installRoot 'bin\CodexWorkerLauncher.exe'),'CodexWorkerLauncher.exe'),@($desktopLauncher,'desktop-launcher.exe'),@((Join-Path $installRoot 'scripts\desktop-start.ps1'),'desktop-start.ps1'),@((Join-Path $installRoot 'desktop.settings.json'),'desktop.settings.json'),@((Join-Path $dataRoot 'preflight.json'),'preflight.json'))) {
  if (Test-Path -LiteralPath $item[0]) { Copy-Item -LiteralPath $item[0] -Destination (Join-Path $backup $item[1]) }
}
$safeStop = @'
import sqlite3, subprocess, sys
with sqlite3.connect(sys.argv[1], isolation_level=None, timeout=10) as db:
    db.execute('BEGIN IMMEDIATE')
    try:
        states=('queued','running','checking','cancelling')
        if db.execute('select count(*) from jobs where status in (?,?,?,?)',states).fetchone()[0]:
            raise RuntimeError('Task became active; deployment stopped without stopping Worker')
        subprocess.run([sys.argv[2],'-NoLogo','-NoProfile','-NonInteractive','-File',sys.argv[3]],check=True)
    finally:
        db.rollback()
print('WORKER_STOPPED_UNDER_SCHEDULER_LOCK')
'@
& $python -X utf8 -c $safeStop (Join-Path $dataRoot 'jobs.sqlite3') $powerShell (Join-Path $installRoot 'scripts\desktop-stop.ps1')
if ($LASTEXITCODE -ne 0) { throw 'Worker did not stop safely; deployment stopped.' }
foreach($process in @(Get-Process -Name 'Codex 服务启动器','CodexWorkerLauncher' -ErrorAction SilentlyContinue)) {
  if ($process.Path -ieq $desktopLauncher -or $process.Path -ieq (Join-Path $installRoot 'bin\CodexWorkerLauncher.exe')) { Stop-Process -Id $process.Id -Force -ErrorAction Stop }
}
foreach($name in @('doctor.py','runner.py','sandbox_state.py','__main__.py','files.py','qa.py','app.py','access_audit.py','task_runtime.py','task_tools.py','store.py')) {
  $module = Join-Path $sourceRoot ('src\codex_remote_worker\' + $name)
  foreach($relative in @('src\codex_remote_worker','.runtime\python313\Lib\site-packages\codex_remote_worker')) {
    $target = Join-Path $installRoot $relative
    if (Test-Path -LiteralPath $target) {
      $original = Join-Path $target $name
      $backupModule = Join-Path $backup ($relative + '\' + $name)
      New-Item -ItemType Directory -Path (Split-Path $backupModule -Parent) -Force | Out-Null
      if (Test-Path -LiteralPath $original) { Copy-Item -LiteralPath $original -Destination $backupModule }
      Copy-Item -LiteralPath $module -Destination $original -Force
    }
  }
}
foreach($file in Get-ChildItem -LiteralPath $workerBuild -Force) { Copy-Item -LiteralPath $file.FullName -Destination (Join-Path $installRoot 'bin\CodexRemoteWorker') -Recurse -Force }
Copy-Item -LiteralPath $launcherBuild -Destination (Join-Path $installRoot 'bin\CodexWorkerLauncher.exe') -Force
Copy-Item -LiteralPath $launcherBuild -Destination $desktopLauncher -Force
Copy-Item -LiteralPath (Join-Path $sourceRoot 'packaging\desktop-repair.ps1') -Destination (Join-Path $installRoot 'scripts\desktop-repair.ps1') -Force
Copy-Item -LiteralPath (Join-Path $sourceRoot 'packaging\desktop-start.ps1') -Destination (Join-Path $installRoot 'scripts\desktop-start.ps1') -Force
Copy-Item -LiteralPath (Join-Path $sourceRoot 'packaging\desktop-ensure.ps1') -Destination (Join-Path $installRoot 'scripts\desktop-ensure.ps1') -Force
Copy-Item -LiteralPath (Join-Path $sourceRoot 'packaging\installer\desktop-stop.ps1') -Destination (Join-Path $installRoot 'scripts\desktop-stop.ps1') -Force
Copy-Item -LiteralPath (Join-Path $sourceRoot 'packaging\start-worker.local.ps1') -Destination (Join-Path $installRoot '.runtime\start-worker.local.ps1') -Force
$settingsPath = Join-Path $installRoot 'desktop.settings.json'
$settings = Get-Content -LiteralPath $settingsPath -Raw | ConvertFrom-Json
$settings | Add-Member -NotePropertyName repair_task -NotePropertyValue 'CodexRemoteWorker-RepairSandbox' -Force
$settings | ConvertTo-Json | Set-Content -LiteralPath $settingsPath -Encoding utf8
$existing = Get-ScheduledTask -TaskName 'CodexRemoteWorker-DesktopStart' -ErrorAction Stop
$taskSettings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::FromMinutes(10)) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
foreach($control in @(@('CodexRemoteWorker-DesktopStart',''),@('CodexRemoteWorker-RepairSandbox',' -Repair'))) {
  $action = New-ScheduledTaskAction -Execute $powerShell -Argument ('-NoLogo -NoProfile -NonInteractive -WindowStyle Hidden -File "' + (Join-Path $installRoot 'scripts\desktop-ensure.ps1') + '"' + $control[1]) -WorkingDirectory $installRoot
  Register-ScheduledTask -TaskName $control[0] -Action $action -Principal $existing.Principal -Settings $taskSettings -Description 'Verify the Worker sandbox and automatically recover an idle Worker under its non-admin account.' -Force | Out-Null
}
$hashes = Get-FileHash -LiteralPath (Join-Path $installRoot 'bin\CodexRemoteWorker\CodexRemoteWorker.exe'),$desktopLauncher -Algorithm SHA256 | Select-Object Path,Hash
[ordered]@{deployed_at=[DateTimeOffset]::UtcNow.ToString('o');backup=$backup;hashes=@($hashes);repair_task='CodexRemoteWorker-RepairSandbox';variant='automatic-recovery-and-utf8-runtime-tools'} | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (Join-Path $backup 'deployment.json') -Encoding utf8
[pscustomobject]@{Deployed=$true;Backup=$backup;RepairTask='CodexRemoteWorker-RepairSandbox'} | ConvertTo-Json -Compress
} finally {
  if ($maintenanceOwned -and (Test-Path -LiteralPath $maintenancePath)) {
    $marker = Get-Content -LiteralPath $maintenancePath -Raw | ConvertFrom-Json
    if ($marker.operation_id -eq $maintenanceId) { Remove-Item -LiteralPath $maintenancePath -Force }
  }
  $controlLock.Dispose()
}
