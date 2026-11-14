$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path $PSScriptRoot -Parent
$config = Get-Content -LiteralPath (Join-Path $projectRoot 'config.local.json') -Raw | ConvertFrom-Json
$statusPath = Join-Path $projectRoot 'desktop-status.json'
$logPath = Join-Path $config.data_root 'desktop-repair.log'
$maintenancePath = Join-Path $config.data_root '.maintenance.json'
$powerShell = Join-Path $projectRoot '.runtime\powershell\pwsh.exe'
$repairLock = $null
$maintenanceOwned = $false
function Write-RepairStatus([string]$State, [string]$Message) {
  $temporary = $statusPath + '.' + [Guid]::NewGuid().ToString('N') + '.tmp'
  [ordered]@{state=$State;message=$Message;process_id=0;public_api='';updated_at=[DateTimeOffset]::UtcNow.ToString('o')} |
    ConvertTo-Json -Compress | Set-Content -LiteralPath $temporary -Encoding utf8
  Move-Item -LiteralPath $temporary -Destination $statusPath -Force
}
try {
  $repairLock = [IO.File]::Open((Join-Path $config.data_root '.desktop-repair.lock'), 'OpenOrCreate', 'ReadWrite', 'None')
  [ordered]@{reason='sandbox_repair';started_at=[DateTimeOffset]::UtcNow.ToString('o')} |
    ConvertTo-Json -Compress | Set-Content -LiteralPath $maintenancePath -Encoding utf8
  $maintenanceOwned = $true
  # The maintenance gate blocks new submissions and dispatch while this check runs.
  $database = Join-Path $config.data_root 'jobs.sqlite3'
  $probe = @'
import pathlib, sqlite3, sys
p=pathlib.Path(sys.argv[1])
if not p.exists():
    print(0)
else:
    with sqlite3.connect(p.as_uri()+'?mode=ro',uri=True,timeout=10) as c:
        print(c.execute("select count(*) from jobs where status in ('running','checking','cancelling')").fetchone()[0])
'@
  $active = & $config.tool_python -c $probe $database
  if ($LASTEXITCODE -ne 0 -or $active -notmatch '^\d+$') { throw '无法确认任务状态，未执行修复。请查看日志。' }
  if ([int]$active -gt 0) { throw '仍有执行中的任务，未停止服务。请等待任务结束后再修复。' }
  Write-RepairStatus 'repairing' '正在重新验证桌面端现有沙箱；不会重置账户。'
  ('{0} repair requested; no active jobs' -f [DateTimeOffset]::UtcNow.ToString('o')) | Add-Content -LiteralPath $logPath -Encoding utf8
  & $powerShell -NoLogo -NoProfile -NonInteractive -File (Join-Path $PSScriptRoot 'desktop-stop.ps1') -PreserveStartTask
  if ($LASTEXITCODE -ne 0) { throw '无法安全停止服务，未重新初始化沙箱。' }
  # No worker can accept a request after its owned process has stopped.
  Remove-Item -LiteralPath $maintenancePath -Force
  $maintenanceOwned = $false
  Write-RepairStatus 'repairing' '正在复用已有沙箱、验证隔离并启动服务。'
  & $powerShell -NoLogo -NoProfile -NonInteractive -File (Join-Path $PSScriptRoot 'desktop-start.ps1')
  if ($LASTEXITCODE -ne 0) { throw '沙箱修复或启动验证未通过，请查看自检日志。' }
  $status = Get-Content -LiteralPath $statusPath -Raw | ConvertFrom-Json
  if ($status.state -ne 'ready') { throw '服务未通过就绪检查。' }
  ('{0} repair completed; pid={1}' -f [DateTimeOffset]::UtcNow.ToString('o'), $status.process_id) | Add-Content -LiteralPath $logPath -Encoding utf8
  exit 0
} catch {
  if ($null -ne $repairLock) {
    Write-RepairStatus 'error' $_.Exception.Message
    ('{0} repair failed: {1}' -f [DateTimeOffset]::UtcNow.ToString('o'), $_.Exception.GetType().Name) | Add-Content -LiteralPath $logPath -Encoding utf8
  }
  exit 1
} finally {
  if ($maintenanceOwned) { Remove-Item -LiteralPath $maintenancePath -Force -ErrorAction SilentlyContinue }
  if ($null -ne $repairLock) { $repairLock.Dispose() }
}
