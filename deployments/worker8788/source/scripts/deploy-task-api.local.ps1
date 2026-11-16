$ErrorActionPreference='Stop'
$workspace=Split-Path $PSScriptRoot -Parent
$root='C:\CodexRemoteWorker'
$data='C:\CodexWorkerData'
$python=Join-Path $root '.runtime\python313\python.exe'
$powerShell=Join-Path $root '.runtime\powershell\pwsh.exe'
$operation=[Guid]::NewGuid().ToString('N')
$marker=Join-Path $data '.maintenance.json'
$control=$null
$owned=$false
$installed=$false
$stopped=$false
function Check-Exit([string]$message) { if ($LASTEXITCODE -ne 0) { throw $message } }
function Stop-Idle {
  $script=@'
import pathlib,sqlite3,subprocess,sys
connections=[]
try:
 for table in ('jobs','tasks'):
  path=pathlib.Path(sys.argv[1])/(table+'.sqlite3')
  if not path.exists(): continue
  db=sqlite3.connect(path,isolation_level=None,timeout=10);connections.append(db);db.execute('BEGIN IMMEDIATE')
  states=('queued','running','checking','cancelling') if table=='jobs' else ('queued','running')
  assert db.execute('select count(*) from '+table+' where status in ('+','.join('?' for _ in states)+')',states).fetchone()[0]==0,'Active jobs or whole tasks; service not stopped'
 subprocess.run([sys.argv[2],'-NoLogo','-NoProfile','-NonInteractive','-WindowStyle','Hidden','-File',sys.argv[3],'-PreserveStartTask'],check=True)
finally:
 for db in reversed(connections):db.rollback();db.close()
'@
  & $python -X utf8 -c $script $data $powerShell (Join-Path $root 'scripts\desktop-stop.ps1')
  Check-Exit 'Idle stop failed'
}
function Run-Doctor {
  $record=& (Join-Path $root '.runtime\start-worker.local.ps1') -Mode Doctor | ConvertFrom-Json
  $process=Get-Process -Id $record.ProcessId -ErrorAction SilentlyContinue
  if ($process -and -not $process.WaitForExit(120000)) { throw 'Native doctor timed out' }
  $report=Get-Content -LiteralPath $record.StandardOutput -Raw | ConvertFrom-Json
  if (-not $report.passed) { throw 'Native sandbox checks failed' }
  Write-Output 'NATIVE_SANDBOX_CHECKS_PASSED'
}
function Start-Verified {
  $record=& $powerShell -NoLogo -NoProfile -NonInteractive -WindowStyle Hidden -File (Join-Path $PSScriptRoot 'validate-tm-native.local.ps1') -Mode Serve | ConvertFrom-Json
  Check-Exit 'Verified service start failed'
  return $record
}
try {
  $deadline=[DateTime]::UtcNow.AddSeconds(60)
  do {
    try { $control=[IO.File]::Open((Join-Path $data '.desktop-control.lock'),'OpenOrCreate','ReadWrite','None') }
    catch [IO.IOException] { if ([DateTime]::UtcNow -ge $deadline) { throw 'Desktop control is busy; no installation changed' }; Start-Sleep -Milliseconds 300 }
  } while ($null -eq $control)
  if (Test-Path -LiteralPath $marker) { throw 'Another maintenance operation exists; no installation changed' }
  # Establish the existing launcher/sandbox baseline before replacing the EXE.
  $config=Get-Content -LiteralPath (Join-Path $root 'config.local.json') -Raw | ConvertFrom-Json
  $credentials=Get-Content -LiteralPath (Join-Path $data 'private\credentials.local.json') -Raw | ConvertFrom-Json
  $baseline=$null
  try { $baseline=Invoke-RestMethod -Uri ('http://127.0.0.1:'+$config.port+'/v1/ready') -Headers @{Authorization='Bearer '+$credentials.CODEX_WORKER_SERVICE_TOKEN} -TimeoutSec 10 -NoProxy } catch { $baseline=$null }
  if (-not $baseline.ready) {
    & $powerShell -NoLogo -NoProfile -NonInteractive -WindowStyle Hidden -File (Join-Path $root 'scripts\desktop-repair.ps1')
    Check-Exit 'Existing service recovery failed before deployment; installation unchanged'
  }
  $previous=Get-Content -LiteralPath (Join-Path $root 'desktop-status.json') -Raw | ConvertFrom-Json
  @{operation_id=$operation;reason='translation-task-api-upgrade'} | ConvertTo-Json -Compress | Set-Content -LiteralPath $marker -Encoding utf8
  $owned=$true
  Stop-Idle
  $stopped=$true
  & $python -X utf8 (Join-Path $PSScriptRoot 'deploy-task-api.local.py') install --operation-id $operation
  Check-Exit 'Task API installation failed'
  $installed=$true
  Run-Doctor
  & $python -X utf8 (Join-Path $PSScriptRoot 'deploy-task-api.local.py') verify --operation-id $operation
  Check-Exit 'Installed files, v4 runtime or existing history changed'
  $serve=Start-Verified
  $stopped=$false
  $config=Get-Content -LiteralPath (Join-Path $root 'config.local.json') -Raw | ConvertFrom-Json
  $credentials=Get-Content -LiteralPath (Join-Path $data 'private\credentials.local.json') -Raw | ConvertFrom-Json
  $api='http://127.0.0.1:'+$config.port
  $headers=@{Authorization='Bearer '+$credentials.CODEX_WORKER_SERVICE_TOKEN}
  $deadline=[DateTime]::UtcNow.AddSeconds(25)
  do {
    try { $capability=Invoke-RestMethod -Uri ($api+'/v1/tasks/capabilities') -Headers $headers -TimeoutSec 3 -NoProxy } catch { $capability=$null }
    if ($capability.contract -eq 'translation-task/1') { break }
    Start-Sleep -Milliseconds 400
  } while ([DateTime]::UtcNow -lt $deadline)
  if ($capability.contract -ne 'translation-task/1' -or -not $capability.automatic_batching) { throw 'New frozen EXE did not expose whole-task capabilities' }
  & $python -X utf8 (Join-Path $PSScriptRoot 'deploy-task-api.local.py') verify --operation-id $operation
  Check-Exit 'Startup verification changed protected state'
  Remove-Item -LiteralPath $marker -Force
  $owned=$false
  $ready=Invoke-RestMethod -Uri ($api+'/v1/ready') -Headers $headers -TimeoutSec 10 -NoProxy
  if (-not $ready.ready) { throw ('Readiness failed: '+$ready.code) }
  [ordered]@{state='ready';message='整单任务接口已接入，原生验证通过。';process_id=$serve.ProcessId;public_api=$previous.public_api;updated_at=[DateTimeOffset]::UtcNow.ToString('o')} | ConvertTo-Json -Compress | Set-Content -LiteralPath (Join-Path $root 'desktop-status.json') -Encoding utf8
  $statePath=Join-Path $workspace 'build\task-api\deployment-state.json'
  $state=Get-Content -LiteralPath $statePath -Raw | ConvertFrom-Json
  $state | Add-Member -NotePropertyName ready_verified -NotePropertyValue $true -Force
  $state | Add-Member -NotePropertyName completed_at -NotePropertyValue ([DateTimeOffset]::UtcNow.ToString('o')) -Force
  $state | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath $statePath -Encoding utf8
  [pscustomobject]@{deployed=$true;ready=$ready.ready;contract=$capability.contract;batch_rows=$capability.batch_policy.max_rows;batch_chars=$capability.batch_policy.max_chars;model_called=$false;backup=$state.backup} | ConvertTo-Json -Compress
} catch {
  $failure=$_.Exception.Message
  if ($installed -or $stopped) {
    if (-not (Test-Path -LiteralPath $marker)) {
      @{operation_id=$operation;reason='translation-task-api-rollback'} | ConvertTo-Json -Compress | Set-Content -LiteralPath $marker -Encoding utf8
      $owned=$true
    }
    Stop-Idle
    $stopped=$true
    if ($installed) {
      & $python -X utf8 (Join-Path $PSScriptRoot 'deploy-task-api.local.py') rollback --operation-id $operation
      Check-Exit 'Rollback failed; maintenance retained'
    }
    Run-Doctor
    $null=Start-Verified
    $installed=$false
    $stopped=$false
  }
  throw $failure
} finally {
  if ($owned -and -not $installed -and -not $stopped -and (Test-Path -LiteralPath $marker)) {
    $owner=Get-Content -LiteralPath $marker -Raw | ConvertFrom-Json
    if ($owner.operation_id -eq $operation) { Remove-Item -LiteralPath $marker -Force }
  }
  if ($null -ne $control) { $control.Dispose() }
}
