$ErrorActionPreference='Stop'
$workspace=Split-Path $PSScriptRoot -Parent
$root='C:\CodexRemoteWorker'
$data='C:\CodexWorkerData'
$python=Join-Path $root '.runtime\python313\python.exe'
$powerShell=Join-Path $root '.runtime\powershell\pwsh.exe'
$marker=Join-Path $data '.maintenance.json'
$operationId=[Guid]::NewGuid().ToString('N')
$control=$null
$owned=$false
$installed=$false
$serviceStopped=$false
function Check-Exit([string]$message) { if ($LASTEXITCODE -ne 0) { throw $message } }
function Wait-Doctor($record) {
  $process=Get-Process -Id $record.ProcessId -ErrorAction SilentlyContinue
  if ($process -and -not $process.WaitForExit(120000)) { throw 'Native doctor timed out; process retained for inspection' }
  $report=Get-Content -LiteralPath $record.StandardOutput -Raw | ConvertFrom-Json
  if (-not $report.passed) { throw 'Native doctor did not pass' }
  Write-Output 'NATIVE_DOCTOR_PASSED'
}
function Stop-IdleWorker {
  $script=@'
import sqlite3,subprocess,sys
with sqlite3.connect(sys.argv[1],isolation_level=None,timeout=10) as db:
 db.execute('BEGIN IMMEDIATE')
 try:
  assert db.execute("select count(*) from jobs where status in ('queued','running','checking','cancelling')").fetchone()[0]==0,'Active jobs; service not stopped'
  subprocess.run([sys.argv[2],'-NoLogo','-NoProfile','-NonInteractive','-WindowStyle','Hidden','-File',sys.argv[3],'-PreserveStartTask'],check=True)
 finally: db.rollback()
'@
  & $python -X utf8 -c $script (Join-Path $data 'jobs.sqlite3') $powerShell (Join-Path $root 'scripts\desktop-stop.ps1')
  Check-Exit 'Idle service stop failed'
}
try {
  $deadline=[DateTime]::UtcNow.AddSeconds(60)
  do {
    try { $control=[IO.File]::Open((Join-Path $data '.desktop-control.lock'),'OpenOrCreate','ReadWrite','None') }
    catch [IO.IOException] { if ([DateTime]::UtcNow -ge $deadline) { throw 'Desktop maintenance is busy; no installation changed' }; Start-Sleep -Milliseconds 300 }
  } while ($null -eq $control)
  if (Test-Path -LiteralPath $marker) { throw 'Another maintenance marker exists; no installation changed' }
  $previous=Get-Content -LiteralPath (Join-Path $root 'desktop-status.json') -Raw | ConvertFrom-Json
  @{operation_id=$operationId;reason='bounded-context-upgrade'} | ConvertTo-Json -Compress | Set-Content -LiteralPath $marker -Encoding utf8
  $owned=$true
  Stop-IdleWorker
  $serviceStopped=$true
  & $python -X utf8 (Join-Path $PSScriptRoot 'deploy-context.local.py') install --operation-id $operationId
  Check-Exit 'Candidate installation failed'
  $installed=$true
  $doctor=& (Join-Path $root '.runtime\start-worker.local.ps1') -Mode Doctor | ConvertFrom-Json
  Wait-Doctor $doctor
  & $powerShell -NoLogo -NoProfile -NonInteractive -WindowStyle Hidden -File (Join-Path $PSScriptRoot 'validate-tm-native.local.ps1') -Mode JsonOnly
  Check-Exit 'Native JSON/context checks failed'
  # Refresh the sandbox receipt after all fixed native probes, without running sandbox setup/reset.
  $doctor=& (Join-Path $root '.runtime\start-worker.local.ps1') -Mode Doctor | ConvertFrom-Json
  Wait-Doctor $doctor
  & $python -X utf8 (Join-Path $PSScriptRoot 'deploy-context.local.py') verify --operation-id $operationId
  Check-Exit 'Installed files or preserved configuration verification failed'
  $launch=& $powerShell -NoLogo -NoProfile -NonInteractive -WindowStyle Hidden -File (Join-Path $PSScriptRoot 'validate-tm-native.local.ps1') -Mode Serve | ConvertFrom-Json
  Check-Exit 'Verified Worker launch failed'
  $serviceStopped=$false
  $config=Get-Content -LiteralPath (Join-Path $root 'config.local.json') -Raw | ConvertFrom-Json
  $credentials=Get-Content -LiteralPath (Join-Path $data 'private\credentials.local.json') -Raw | ConvertFrom-Json
  $api='http://127.0.0.1:'+$config.port
  $healthDeadline=[DateTime]::UtcNow.AddSeconds(25)
  do {
    try { $health=Invoke-RestMethod -Uri ($api+'/healthz') -TimeoutSec 3 -NoProxy } catch { $health=$null }
    if ($health.status -eq 'alive') { break }
    Start-Sleep -Milliseconds 400
  } while ([DateTime]::UtcNow -lt $healthDeadline)
  if ($health.status -ne 'alive') { throw 'Worker did not become alive' }
  Remove-Item -LiteralPath $marker -Force
  $owned=$false
  $ready=Invoke-RestMethod -Uri ($api+'/v1/ready') -Headers @{Authorization='Bearer '+$credentials.CODEX_WORKER_SERVICE_TOKEN} -TimeoutSec 10 -NoProxy
  if (-not $ready.ready) { throw ('Readiness failed: '+$ready.code) }
  [ordered]@{state='ready';message='分页与批量输入更新完成，原生检查通过。';process_id=$launch.ProcessId;public_api=$previous.public_api;updated_at=[DateTimeOffset]::UtcNow.ToString('o')} | ConvertTo-Json -Compress | Set-Content -LiteralPath (Join-Path $root 'desktop-status.json') -Encoding utf8
  $statePath=Join-Path $workspace 'build\bounded-context\deployment-state.json'
  $state=Get-Content -LiteralPath $statePath -Raw | ConvertFrom-Json
  $state | Add-Member -NotePropertyName ready_verified -NotePropertyValue $true -Force
  $state | Add-Member -NotePropertyName completed_at -NotePropertyValue ([DateTimeOffset]::UtcNow.ToString('o')) -Force
  $state | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $statePath -Encoding utf8
  [pscustomobject]@{deployed=$true;ready=$ready.ready;service_version=$ready.service_version;runtime='utf8-absolute-tools-v4-bounded-context';model_called=$false;backup=$state.backup} | ConvertTo-Json -Compress
} catch {
  $failure=$_.Exception.Message
  if ($installed -or $serviceStopped) {
    if (-not (Test-Path -LiteralPath $marker)) {
      @{operation_id=$operationId;reason='bounded-context-rollback'} | ConvertTo-Json -Compress | Set-Content -LiteralPath $marker -Encoding utf8
      $owned=$true
    }
    Stop-IdleWorker
    $serviceStopped=$true
    if ($installed) {
      & $python -X utf8 (Join-Path $PSScriptRoot 'deploy-context.local.py') rollback --operation-id $operationId
      Check-Exit 'Rollback failed; maintenance gate retained'
    }
    $doctor=& (Join-Path $root '.runtime\start-worker.local.ps1') -Mode Doctor | ConvertFrom-Json
    Wait-Doctor $doctor
    & $powerShell -NoLogo -NoProfile -NonInteractive -WindowStyle Hidden -File (Join-Path $PSScriptRoot 'validate-tm-native.local.ps1') -Mode Serve
    Check-Exit 'Original Worker restart failed'
    $installed=$false
    $serviceStopped=$false
  }
  throw $failure
} finally {
  if ($owned -and -not $installed -and -not $serviceStopped -and (Test-Path -LiteralPath $marker)) {
    $current=Get-Content -LiteralPath $marker -Raw | ConvertFrom-Json
    if ($current.operation_id -eq $operationId) { Remove-Item -LiteralPath $marker -Force }
  }
  if ($null -ne $control) { $control.Dispose() }
}
