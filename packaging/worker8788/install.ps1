$ErrorActionPreference='Stop'
$workspace=Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
$dest='C:\CodexRemoteWorker-AppServer-8788\desktop'
New-Item -ItemType Directory -Force -Path $dest | Out-Null
Copy-Item -LiteralPath "$workspace\build\app-server-8788\Worker8788.exe" -Destination "$dest\Worker8788.exe"
Copy-Item -LiteralPath "$PSScriptRoot\watchdog.ps1" -Destination "$dest\watchdog.ps1"
Copy-Item -LiteralPath "$PSScriptRoot\settings8788.py" -Destination "$dest\settings8788.py"
Copy-Item -LiteralPath "$workspace\build\app-server-8788\start-reuse-subscription.ps1" -Destination "$dest\start-worker.ps1"
$pwsh=(Get-Command pwsh.exe).Source
@{pwsh=$pwsh} | ConvertTo-Json | Set-Content -LiteralPath "$dest\runtime.json" -Encoding utf8
$action=New-ScheduledTaskAction -Execute $pwsh -Argument ('-NoProfile -NonInteractive -WindowStyle Hidden -File "'+$dest+'\watchdog.ps1"') -WorkingDirectory $dest
$boot=New-ScheduledTaskTrigger -AtStartup
$repeat=New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 1)
$settings=New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 4) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName 'CodexWorker8788-Recovery' -Action $action -Trigger @($boot,$repeat) -Settings $settings -User SYSTEM -RunLevel Highest -Force | Out-Null
$desktop=[Environment]::GetFolderPath('Desktop')
Copy-Item -LiteralPath "$dest\Worker8788.exe" -Destination "$desktop\Worker8788.exe"
$principal=New-ScheduledTaskPrincipal -UserId ([Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive -RunLevel Highest
$uiAction=New-ScheduledTaskAction -Execute "$dest\Worker8788.exe" -Argument '--tray' -WorkingDirectory $dest
$uiSettings=New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName 'CodexWorker8788-Desktop' -Action $uiAction -Trigger (New-ScheduledTaskTrigger -AtLogOn -User $principal.UserId) -Principal $principal -Settings $uiSettings -Force | Out-Null
Write-Output 'Installed desktop EXE, logon tray and boot/minute recovery for 8788 only.'
