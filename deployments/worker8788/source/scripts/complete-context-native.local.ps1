$ErrorActionPreference='Stop'
$workspace=Split-Path $PSScriptRoot -Parent
$root='C:\CodexRemoteWorker'
$data='C:\CodexWorkerData'
$python=Join-Path $root '.runtime\python313\python.exe'
$powerShell=Join-Path $root '.runtime\powershell\pwsh.exe'
$marker=Join-Path $data '.maintenance.json'
$operation=[Guid]::NewGuid().ToString('N')
$owned=$false
$finished=$false
$control=$null
function Check-Exit([string]$message) { if ($LASTEXITCODE -ne 0) { throw $message } }
function Run-Doctor {
  $record=& (Join-Path $root '.runtime\start-worker.local.ps1') -Mode Doctor | ConvertFrom-Json
  $process=Get-Process -Id $record.ProcessId -ErrorAction SilentlyContinue
  if ($process -and -not $process.WaitForExit(120000)) { throw 'Doctor timed out' }
  $report=Get-Content -LiteralPath $record.StandardOutput -Raw | ConvertFrom-Json
  if (-not $report.passed) { throw 'Native isolation checks failed' }
  Write-Output 'NATIVE_ISOLATION_CHECKS_PASSED'
}
try {
  $deadline=[DateTime]::UtcNow.AddSeconds(60)
  do {
    try { $control=[IO.File]::Open((Join-Path $data '.desktop-control.lock'),'OpenOrCreate','ReadWrite','None') }
    catch [IO.IOException] { if ([DateTime]::UtcNow -ge $deadline) { throw 'Desktop control is busy' }; Start-Sleep -Milliseconds 300 }
  } while ($null -eq $control)
  if (Test-Path -LiteralPath $marker) { throw 'Another maintenance operation is active' }
  $previous=Get-Content -LiteralPath (Join-Path $root 'desktop-status.json') -Raw | ConvertFrom-Json
  @{operation_id=$operation;reason='bounded-context-final-native-check'} | ConvertTo-Json -Compress | Set-Content -LiteralPath $marker -Encoding utf8
  $owned=$true
  $stop=@'
import sqlite3,subprocess,sys
with sqlite3.connect(sys.argv[1],isolation_level=None,timeout=10) as db:
 db.execute('BEGIN IMMEDIATE')
 try:
  assert db.execute("select count(*) from jobs where status in ('queued','running','checking','cancelling')").fetchone()[0]==0,'Active jobs; service not stopped'
  subprocess.run([sys.argv[2],'-NoLogo','-NoProfile','-NonInteractive','-WindowStyle','Hidden','-File',sys.argv[3],'-PreserveStartTask'],check=True)
 finally: db.rollback()
'@
  & $python -X utf8 -c $stop (Join-Path $data 'jobs.sqlite3') $powerShell (Join-Path $root 'scripts\desktop-stop.ps1')
  Check-Exit 'Cannot stop idle service'
  Run-Doctor
  $launch=& (Join-Path $PSScriptRoot 'launch-runtime-validation.local.ps1') | ConvertFrom-Json
  $process=Get-Process -Id $launch.ProcessId -ErrorAction SilentlyContinue
  if ($process -and -not $process.WaitForExit(120000)) { throw 'Input replay timed out' }
  $report=Get-Content -LiteralPath $launch.Stdout -Raw | ConvertFrom-Json
  if (-not $report.native_probe.passed -or $report.model_called -ne $false -or $report.preparations.Count -ne 2) { throw 'Service-account input replay failed' }
  foreach ($preparation in $report.preparations) {
    if ($preparation.runtime_version -ne 'utf8-absolute-tools-v4-bounded-context' -or $preparation.context.page_count -lt 1 -or $preparation.model_called -ne $false) { throw 'Context runtime mismatch' }
  }
  if (-not $report.preparations[1].guide_cache_hits.project_style -or -not $report.preparations[1].guide_cache_hits.general_style) { throw 'Guide cache did not warm' }
  $report | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath (Join-Path $workspace 'build\bounded-context\native-input-replay.json') -Encoding utf8
  Write-Output ('NATIVE_INPUT_REPLAY_PASSED pages={0} warm_seconds={1}' -f $report.preparations[1].context.page_count,$report.preparations[1].seconds)
  Run-Doctor
  & $python -X utf8 (Join-Path $PSScriptRoot 'deploy-context.local.py') verify --operation-id $operation
  Check-Exit 'Installed files/configuration/history mismatch'
  $serve=& $powerShell -NoLogo -NoProfile -NonInteractive -WindowStyle Hidden -File (Join-Path $PSScriptRoot 'validate-tm-native.local.ps1') -Mode Serve | ConvertFrom-Json
  Check-Exit 'Service launch failed'
  $config=Get-Content -LiteralPath (Join-Path $root 'config.local.json') -Raw | ConvertFrom-Json
  $credentials=Get-Content -LiteralPath (Join-Path $data 'private\credentials.local.json') -Raw | ConvertFrom-Json
  $api='http://127.0.0.1:'+$config.port
  $deadline=[DateTime]::UtcNow.AddSeconds(25)
  do {
    try { $health=Invoke-RestMethod -Uri ($api+'/healthz') -TimeoutSec 3 -NoProxy } catch { $health=$null }
    if ($health.status -eq 'alive') { break }
    Start-Sleep -Milliseconds 400
  } while ([DateTime]::UtcNow -lt $deadline)
  if ($health.status -ne 'alive') { throw 'Service did not become alive' }
  Remove-Item -LiteralPath $marker -Force
  $owned=$false
  $ready=Invoke-RestMethod -Uri ($api+'/v1/ready') -Headers @{Authorization='Bearer '+$credentials.CODEX_WORKER_SERVICE_TOKEN} -TimeoutSec 10 -NoProxy
  if (-not $ready.ready) { throw ('Readiness failed: '+$ready.code) }
  [ordered]@{state='ready';message='分页与批量输入更新完成，原生输入回放和缓存预热通过。';process_id=$serve.ProcessId;public_api=$previous.public_api;updated_at=[DateTimeOffset]::UtcNow.ToString('o')} | ConvertTo-Json -Compress | Set-Content -LiteralPath (Join-Path $root 'desktop-status.json') -Encoding utf8
  $statePath=Join-Path $workspace 'build\bounded-context\deployment-state.json'
  $state=Get-Content -LiteralPath $statePath -Raw | ConvertFrom-Json
  $state | Add-Member -NotePropertyName native_input_replay_passed -NotePropertyValue $true -Force
  $state | Add-Member -NotePropertyName final_ready_verified_at -NotePropertyValue ([DateTimeOffset]::UtcNow.ToString('o')) -Force
  $state | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $statePath -Encoding utf8
  $finished=$true
  [pscustomobject]@{ready=$ready.ready;runtime='utf8-absolute-tools-v4-bounded-context';model_called=$false;pages=$report.preparations[1].context.page_count;warm_preparation_seconds=$report.preparations[1].seconds;configuration_preserved=$true;history_preserved=$true} | ConvertTo-Json -Compress
} finally {
  if ($finished -and $owned -and (Test-Path -LiteralPath $marker)) {
    $current=Get-Content -LiteralPath $marker -Raw | ConvertFrom-Json
    if ($current.operation_id -eq $operation) { Remove-Item -LiteralPath $marker -Force }
  }
  if ($null -ne $control) { $control.Dispose() }
}
