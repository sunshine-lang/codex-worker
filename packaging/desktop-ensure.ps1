param([switch]$Repair)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path $PSScriptRoot -Parent
$config = Get-Content -LiteralPath (Join-Path $projectRoot 'config.local.json') -Raw | ConvertFrom-Json
$dataRoot = [string]$config.data_root
$statusPath = Join-Path $projectRoot 'desktop-status.json'
$maintenancePath = Join-Path $dataRoot '.maintenance.json'
$logPath = Join-Path $dataRoot 'desktop-ensure.log'
$powerShell = Join-Path $projectRoot '.runtime\powershell\pwsh.exe'
$operationId = [Guid]::NewGuid().ToString('N')
$operation = if ($Repair) { 'repair' } else { 'ensure' }
$controlLock = $null
$maintenanceOwned = $false
$credentials = Get-Content -LiteralPath (Join-Path $dataRoot 'private\credentials.local.json') -Raw | ConvertFrom-Json
function Write-ControlStatus([string]$State,[string]$Message,[string]$Code='', $Existing=$null) {
  $record = [ordered]@{state=$State;message=$Message;code=$Code;process_id=0;public_api='';operation=$operation;operation_id=$operationId;updated_at=[DateTimeOffset]::UtcNow.ToString('o')}
  if ($null -ne $Existing) {
    $record.process_id = $Existing.process_id
    $record.public_api = $Existing.public_api
  }
  $temporary = $statusPath + '.' + $operationId + '.tmp'
  $record | ConvertTo-Json -Compress | Set-Content -LiteralPath $temporary -Encoding utf8
  Move-Item -LiteralPath $temporary -Destination $statusPath -Force
}
function Read-Readiness {
  $request = [Net.HttpWebRequest]::Create('http://127.0.0.1:' + $config.port + '/v1/ready')
  $request.Proxy=$null; $request.Timeout=15000; $request.ReadWriteTimeout=15000
  $request.Headers['Authorization']='Bearer '+$credentials.CODEX_WORKER_SERVICE_TOKEN
  try { $response=$request.GetResponse() }
  catch { $response=$_.Exception.Response; if ($null -eq $response) { return $null } }
  try {
    $reader=[IO.StreamReader]::new($response.GetResponseStream())
    try { return ($reader.ReadToEnd() | ConvertFrom-Json) } finally { $reader.Dispose() }
  } finally { $response.Dispose() }
}
function Count-ActiveJobs([switch]$RunningOnly) {
  $database = Join-Path $dataRoot 'jobs.sqlite3'
  $script = @'
import pathlib, sqlite3, sys
p=pathlib.Path(sys.argv[1])
if not p.exists():
    print(0)
else:
    with sqlite3.connect(p.as_uri()+'?mode=ro',uri=True,timeout=10) as c:
        states=('running','checking','cancelling') if sys.argv[2]=='running' else ('queued','running','checking','cancelling')
        placeholders=','.join('?' for _ in states)
        print(c.execute('select count(*) from jobs where status in ('+placeholders+')',states).fetchone()[0])
'@
  $scope = if ($RunningOnly) { 'running' } else { 'all' }
  $result = & $config.tool_python -c $script $database $scope
  if ($LASTEXITCODE -ne 0 -or $result -notmatch '^\d+$') { throw '无法确认任务状态，已停止自动维护。' }
  return [int]$result
}
function Known-Worker {
  $path = Join-Path $dataRoot 'worker.local.process.json'
  if (-not (Test-Path -LiteralPath $path)) { return $null }
  $record = Get-Content -LiteralPath $path -Raw | ConvertFrom-Json
  $process = Get-Process -Id $record.ProcessId -ErrorAction SilentlyContinue
  $allowed = @((Join-Path $projectRoot 'bin\CodexRemoteWorker\CodexRemoteWorker.exe'),$config.tool_python)
  if ($process -and $process.Path -eq $record.Executable -and $process.Path -in $allowed -and $process.StartTime.ToUniversalTime().Ticks -eq ([DateTimeOffset]$record.StartedAt).UtcDateTime.Ticks) { return $record }
  return $null
}
function Probe-LiveSandbox {
  Write-ControlStatus 'checking' '正在执行真实沙箱探测，不调用模型。'
  $launch = & (Join-Path $projectRoot '.runtime\start-worker.local.ps1') -Mode Probe | ConvertFrom-Json
  $process = Get-Process -Id $launch.ProcessId -ErrorAction SilentlyContinue
  if ($process -and -not $process.WaitForExit(45000)) {
    # Only terminate the just-created, identity-matched probe controller.
    if ($process.Path -eq $launch.Executable -and $process.StartTime.ToUniversalTime().Ticks -eq ([DateTimeOffset]$launch.StartedAt).UtcDateTime.Ticks) { Stop-Process -Id $process.Id -Force }
    return $false
  }
  try {
    $result = Get-Content -LiteralPath $launch.StandardOutput -Raw | ConvertFrom-Json
    return ($result.passed -eq $true -and $result.probe_only -eq $true)
  } catch { return $false }
}
try {
  $deadline = [DateTime]::UtcNow.AddSeconds(30)
  do {
    try { $controlLock=[IO.File]::Open((Join-Path $dataRoot '.desktop-control.lock'),'OpenOrCreate','ReadWrite','None') }
    catch [IO.IOException] { if ([DateTime]::UtcNow -ge $deadline) { throw '已有启动或修复操作正在执行，请稍候。' }; Start-Sleep -Milliseconds 300 }
  } while ($null -eq $controlLock)
  $previous = Get-Content -LiteralPath $statusPath -Raw -ErrorAction SilentlyContinue | ConvertFrom-Json
  Write-ControlStatus 'checking' '正在检查服务并自动处理沙箱状态。'
  ('{0} operation={1} id={2} started' -f [DateTimeOffset]::UtcNow.ToString('o'),$operation,$operationId) | Add-Content -LiteralPath $logPath -Encoding utf8
  $worker=Known-Worker
  $ready=if($worker){Read-Readiness}else{$null}
  $active=Count-ActiveJobs
  if ($active -gt 0) {
    if (-not $Repair -and $ready.ready) {
      Write-ControlStatus 'ready' '已有任务运行，已验证当前环境并保留任务。' 'READY_BUSY' $previous
      exit 0
    }
    if ((Count-ActiveJobs -RunningOnly) -gt 0) {
      Write-ControlStatus 'error' '仍有执行中的任务，自动修复已延后，任务不会被停止。' 'ACTIVE_JOBS' $previous
      exit 1
    }
  }
  if (-not $Repair -and $worker -and $ready.ready) {
    # Keep submissions/dispatch out of the synthetic probe workspace while testing.
    @{reason='live_sandbox_check';operation_id=$operationId} | ConvertTo-Json -Compress | Set-Content -LiteralPath $maintenancePath -Encoding utf8
    $maintenanceOwned=$true
    if ((Count-ActiveJobs) -eq 0) { $passed=Probe-LiveSandbox } else { $passed=$true }
    Remove-Item -LiteralPath $maintenancePath -Force
    $maintenanceOwned=$false
    $ready=Read-Readiness
    if ($passed -and $ready.ready) {
      Write-ControlStatus 'ready' '真实沙箱探测通过，服务已就绪。' 'READY_VERIFIED' $previous
      ('{0} id={1} verified existing pid={2}' -f [DateTimeOffset]::UtcNow.ToString('o'),$operationId,$worker.ProcessId) | Add-Content -LiteralPath $logPath -Encoding utf8
      exit 0
    }
  }
  # Recovery is bounded. A second concurrent reset can be retried once, never loop forever.
  for ($attempt=1; $attempt -le 2; $attempt++) {
    Write-ControlStatus 'repairing' '沙箱需要恢复，正在自动修复并重新验证。'
    & $powerShell -NoLogo -NoProfile -NonInteractive -File (Join-Path $PSScriptRoot 'desktop-repair.ps1')
    $repairExit=$LASTEXITCODE
    $current = Get-Content -LiteralPath $statusPath -Raw | ConvertFrom-Json
    $ready=Read-Readiness
    if ($repairExit -eq 0 -and $ready.ready) {
      Write-ControlStatus 'ready' '沙箱已自动恢复并通过隔离验证，服务已就绪。' 'READY_REPAIRED' $current
      ('{0} id={1} recovered pid={2} attempt={3}' -f [DateTimeOffset]::UtcNow.ToString('o'),$operationId,$current.process_id,$attempt) | Add-Content -LiteralPath $logPath -Encoding utf8
      exit 0
    }
    if ((Count-ActiveJobs -RunningOnly) -gt 0) { Write-ControlStatus 'error' '检测到执行中的任务，自动修复已延后。' 'ACTIVE_JOBS' $current; exit 1 }
    if ($attempt -eq 1) { Start-Sleep -Seconds 2 }
  }
  throw '自动修复未通过验证。请暂停同机其他 Codex 的沙箱初始化，并查看日志。'
} catch {
  if ($null -ne $controlLock) {
    Write-ControlStatus 'error' $_.Exception.Message 'START_CHECK_FAILED'
    ('{0} id={1} failed: {2}' -f [DateTimeOffset]::UtcNow.ToString('o'),$operationId,$_.Exception.GetType().Name) | Add-Content -LiteralPath $logPath -Encoding utf8
  }
  exit 1
} finally {
  if ($maintenanceOwned) { Remove-Item -LiteralPath $maintenancePath -Force -ErrorAction SilentlyContinue }
  if ($null -ne $controlLock) { $controlLock.Dispose() }
}
