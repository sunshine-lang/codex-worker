$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path $PSScriptRoot -Parent
$config = Get-Content -LiteralPath (Join-Path $projectRoot 'config.local.json') -Raw | ConvertFrom-Json
$statusPath = Join-Path $projectRoot 'desktop-status.json'
$logPath = Join-Path $config.data_root 'desktop-start.log'
function Write-DesktopStatus([string]$State, [string]$Message, [string]$PublicApi = '', [int]$WorkerProcessId = 0) {
  $record = [ordered]@{state=$State;message=$Message;public_api=$PublicApi;process_id=$WorkerProcessId;updated_at=[DateTimeOffset]::UtcNow.ToString('o')}
  $temporary = $statusPath + '.' + [Guid]::NewGuid().ToString('N') + '.tmp'
  $record | ConvertTo-Json -Compress | Set-Content -LiteralPath $temporary -Encoding utf8
  for ($attempt=0; $attempt -lt 5; $attempt++) {
    try { Move-Item -LiteralPath $temporary -Destination $statusPath -Force; return } catch { if ($attempt -eq 4) { throw }; Start-Sleep -Milliseconds 100 }
  }
}
function Resolve-WorkerApiAddress {
  $settingsPath = Join-Path $projectRoot 'desktop.settings.json'
  if (Test-Path -LiteralPath $settingsPath) {
    $displaySettings = Get-Content -LiteralPath $settingsPath -Raw | ConvertFrom-Json
    if ($displaySettings.public_api_url) { return ([string]$displaySettings.public_api_url).TrimEnd('/') }
  }
  try {
    $session = Invoke-RestMethod -Method Put -Uri 'http://169.254.169.254/latest/api/token' -Headers @{'X-aws-ec2-metadata-token-ttl-seconds'='60'} -TimeoutSec 2 -NoProxy
    $address = [string](Invoke-RestMethod -Uri 'http://169.254.169.254/latest/meta-data/public-ipv4' -Headers @{'X-aws-ec2-metadata-token'=$session} -TimeoutSec 2 -NoProxy)
    $parsedAddress = $null
    if ([Net.IPAddress]::TryParse($address,[ref]$parsedAddress)) { return ('http://' + $address + ':' + $config.port) }
  } catch { }
  $localAddress = [Net.Dns]::GetHostAddresses([Environment]::MachineName) | Where-Object { $_.AddressFamily -eq [Net.Sockets.AddressFamily]::InterNetwork -and -not [Net.IPAddress]::IsLoopback($_) -and $_.ToString() -notlike '169.254.*' } | Select-Object -First 1
  if ($localAddress) { return ('http://' + $localAddress.ToString() + ':' + $config.port) }
  return ('http://127.0.0.1:' + $config.port)
}
Write-DesktopStatus 'starting' '正在检查并启动服务。'
try {
  $launchRecord = & (Join-Path $projectRoot '.runtime\start-worker.local.ps1') | ConvertFrom-Json
  $credentials = Get-Content -LiteralPath (Join-Path $config.data_root 'private\credentials.local.json') -Raw | ConvertFrom-Json
  $deadline = [DateTime]::UtcNow.AddSeconds(90)
  $ready = $null
  $lastReadyError = 'none'
  ('{0} checking API pid={1}' -f [DateTimeOffset]::UtcNow.ToString('o'), $launchRecord.ProcessId) | Add-Content -LiteralPath $logPath -Encoding utf8
  do {
    try {
      $request = [System.Net.HttpWebRequest]::Create('http://127.0.0.1:' + $config.port + '/v1/ready')
      $request.Proxy = $null
      $request.Timeout = 60000
      $request.ReadWriteTimeout = 60000
      $request.Headers['Authorization'] = 'Bearer ' + $credentials.CODEX_WORKER_SERVICE_TOKEN
      $response = $request.GetResponse()
      try { $reader = [System.IO.StreamReader]::new($response.GetResponseStream()); try { $ready = $reader.ReadToEnd() | ConvertFrom-Json } finally { $reader.Dispose() } } finally { $response.Dispose() }
      if ($ready.ready) { break }
      $lastReadyError = 'NOT_READY:' + $ready.code
    } catch {
      $lastReadyError = $_.Exception.GetType().Name
      if ($_.Exception.Response) { $lastReadyError += ':HTTP' + [int]$_.Exception.Response.StatusCode }
      $ready = $null
      if ($_.Exception.InnerException -is [System.Net.WebException] -and $_.Exception.InnerException.Status -eq [System.Net.WebExceptionStatus]::Timeout) { throw 'API readiness request timed out.' }
    }
    ('{0} readiness pending: {1}' -f [DateTimeOffset]::UtcNow.ToString('o'), $lastReadyError) | Add-Content -LiteralPath $logPath -Encoding utf8
    Start-Sleep -Milliseconds 500
  } while ([DateTime]::UtcNow -lt $deadline)
  if (-not $ready.ready) { throw ('Worker did not become ready: ' + $lastReadyError) }
  Write-DesktopStatus 'ready' '服务已启动，API 已就绪。' (Resolve-WorkerApiAddress) $launchRecord.ProcessId
  ('{0} ready pid={1}' -f [DateTimeOffset]::UtcNow.ToString('o'), $launchRecord.ProcessId) | Add-Content -LiteralPath $logPath -Encoding utf8
  exit 0
} catch {
  Write-DesktopStatus 'error' '启动或沙箱验证未通过，请点击“修复沙箱”或打开日志查看结果。'
  ('{0} startup failed: {1}' -f [DateTimeOffset]::UtcNow.ToString('o'), $_.Exception.Message) | Add-Content -LiteralPath $logPath -Encoding utf8
  exit 1
}


