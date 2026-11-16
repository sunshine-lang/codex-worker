param([switch]$PreserveStartTask)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path $PSScriptRoot -Parent
$config = Get-Content -LiteralPath (Join-Path $projectRoot 'config.local.json') -Raw | ConvertFrom-Json
$settingsPath = Join-Path $projectRoot 'desktop.settings.json'
$settings = if (Test-Path -LiteralPath $settingsPath) { Get-Content -LiteralPath $settingsPath -Raw | ConvertFrom-Json } else { $null }
$startTask = if ($settings.start_task) { $settings.start_task } else { 'CodexRemoteWorker-DesktopStart' }
$task = $null
$starting = $null -ne $task -and $task.State -eq 'Running'

function Stop-OwnedProcess([string]$Path) {
  if (-not (Test-Path -LiteralPath $Path)) { return }
  $record = Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json
  $process = Get-Process -Id $record.ProcessId -ErrorAction SilentlyContinue
  if (-not $process) { return }
  $allowed = @((Join-Path $projectRoot 'bin\CodexRemoteWorker\CodexRemoteWorker.exe'),$config.tool_python)
  if ($process.Path -ne $record.Executable -or $process.Path -notin $allowed -or $process.StartTime.ToUniversalTime().Ticks -ne ([DateTimeOffset]$record.StartedAt).UtcDateTime.Ticks) { return }
  Stop-Process -Id $process.Id -Force -ErrorAction Stop
  if (-not $process.WaitForExit(15000)) { throw 'The service process has not stopped.' }
}
try {
  Stop-OwnedProcess (Join-Path $config.data_root 'worker.local.doctor.process.json')
  Stop-OwnedProcess (Join-Path $config.data_root 'worker.local.serve.process.json')
  Stop-OwnedProcess (Join-Path $config.data_root 'worker.local.process.json')
  if($starting) {
    $native=Join-Path $projectRoot 'bin\CodexRemoteWorker\CodexRemoteWorker.exe'
    $configPath=Join-Path $projectRoot 'config.local.json'
    $pending=Get-CimInstance Win32_Process -Filter "Name='CodexRemoteWorker.exe'" | Where-Object{$_.ExecutablePath -eq $native -and $_.CommandLine.IndexOf($configPath,[StringComparison]::OrdinalIgnoreCase) -ge 0}
    foreach($process in $pending){Stop-Process -Id $process.ProcessId -Force -ErrorAction Stop}
  }
  [ordered]@{state='stopped';message='服务已停止。';process_id=0;public_api='';updated_at=[DateTimeOffset]::UtcNow.ToString('o')} | ConvertTo-Json -Compress | Set-Content -LiteralPath (Join-Path $projectRoot 'desktop-status.json') -Encoding utf8
  exit 0
} catch {
  ('{0} stop failed: {1}' -f [DateTimeOffset]::UtcNow.ToString('o'),$_.Exception.GetType().Name) | Add-Content -LiteralPath (Join-Path $config.data_root 'desktop-stop.log') -Encoding utf8
  exit 1
}


