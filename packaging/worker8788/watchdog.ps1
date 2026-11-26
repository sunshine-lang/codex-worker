$ErrorActionPreference='Stop'
$data='C:\CodexWorkerData-AppServer-8788'
if(Test-Path -LiteralPath (Join-Path $data '.desktop-stopped')){exit 0}
# Never kill a listener or restart a busy/unhealthy process automatically.
if(Get-NetTCPConnection -LocalPort 8788 -State Listen -ErrorAction SilentlyContinue){exit 0}
$existing=Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" | Where-Object { $_.CommandLine -like '*CodexRemoteWorker-AppServer-8788*packaged-background.py*serve*' }
if($existing){exit 0}
try {
 & 'C:\CodexRemoteWorker-AppServer-8788\desktop\start-worker.ps1' -Mode Serve | Out-Null
 Add-Content -LiteralPath "$data\watchdog.log" -Value "$([DateTime]::UtcNow.ToString('o')) start requested"
} catch {
 Add-Content -LiteralPath "$data\watchdog.log" -Value "$([DateTime]::UtcNow.ToString('o')) start failed: $($_.Exception.Message)"
 exit 1
}
