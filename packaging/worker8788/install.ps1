$ErrorActionPreference='Stop'
$workspace=Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
$dest='C:\CodexRemoteWorker-AppServer-8788\desktop'
New-Item -ItemType Directory -Force -Path $dest | Out-Null
Copy-Item -LiteralPath "$workspace\build\app-server-8788\Worker8788.exe" -Destination "$dest\Worker8788.exe"
foreach($name in 'watchdog.ps1','watchdog-hidden.pyw','settings8788.py','codex_provider.py'){
 Copy-Item -LiteralPath (Join-Path $PSScriptRoot $name) -Destination (Join-Path $dest $name)
}
Copy-Item -LiteralPath "$workspace\deployments\worker8788\runtime\start-worker.ps1" -Destination "$dest\start-worker.ps1"
$pwsh=(Get-Command pwsh.exe).Source
@{pwsh=$pwsh} | ConvertTo-Json | Set-Content -LiteralPath "$dest\runtime.json" -Encoding utf8
$action=New-ScheduledTaskAction -Execute 'C:\CodexRemoteWorker\.runtime\python313\pythonw.exe' -Argument ('-I -B "'+$dest+'\watchdog-hidden.pyw"') -WorkingDirectory $dest
$principal=New-ScheduledTaskPrincipal -UserId ([Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive -RunLevel Highest
$logon=New-ScheduledTaskTrigger -AtLogOn -User $principal.UserId
$repeat=New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 1)
$settings=New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 4) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName 'CodexWorker8788-Recovery' -Action $action -Trigger @($logon,$repeat) -Settings $settings -Principal $principal -Force | Out-Null
$desktop=[Environment]::GetFolderPath('Desktop')
Copy-Item -LiteralPath "$dest\Worker8788.exe" -Destination "$desktop\Worker8788.exe"
$uiAction=New-ScheduledTaskAction -Execute "$dest\Worker8788.exe" -Argument '--tray' -WorkingDirectory $dest
$uiSettings=New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName 'CodexWorker8788-Desktop' -Action $uiAction -Trigger (New-ScheduledTaskTrigger -AtLogOn -User $principal.UserId) -Principal $principal -Settings $uiSettings -Force | Out-Null
Write-Output 'Installed the 8788 desktop and interactive logon/minute recovery.'
