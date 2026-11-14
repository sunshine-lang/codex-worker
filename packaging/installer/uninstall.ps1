param([Parameter(Mandatory=$true)][string]$InstallRoot)
$ErrorActionPreference='Stop'
$root=[IO.Path]::GetFullPath($InstallRoot).TrimEnd('\')
$markerPath=Join-Path $root 'Data\private\installation.json'
$marker=Get-Content -LiteralPath $markerPath -Raw -Encoding UTF8|ConvertFrom-Json
if($marker.product_id -ne '5ea84759-74a0-438e-bcaf-cf19803d8053' -or $marker.install_dir -ine $root -or $root -eq [IO.Path]::GetPathRoot($root)){throw 'INSTALLATION_IDENTITY_INVALID'}
& (Join-Path $root '.runtime\powershell\pwsh.exe') -NoLogo -NoProfile -NonInteractive -File (Join-Path $root 'scripts\desktop-stop.ps1')
if($LASTEXITCODE -ne 0){throw 'SERVICE_STOP_FAILED'}
foreach($name in @('CodexRemoteWorker-DesktopStart','CodexRemoteWorker-DesktopStop','CodexRemoteWorker-RepairSandbox')){Unregister-ScheduledTask -TaskName $name -Confirm:$false -ErrorAction SilentlyContinue}
& (Join-Path $root '.runtime\python313\python.exe') (Join-Path $root '.runtime\wfp-network-lockdown.py') --remove | Out-Null
Remove-NetFirewallRule -Name ('CodexRemoteWorker-Api-'+$marker.installation_id) -ErrorAction SilentlyContinue
$user=Get-LocalUser -Name $marker.service_account -ErrorAction SilentlyContinue
if($user -and $user.SID.Value -eq $marker.service_account_sid){Disable-LocalUser -Name $user.Name}
$registry='HKLM:\SOFTWARE\CodexRemoteWorker'
if((Get-ItemProperty -Path $registry -ErrorAction SilentlyContinue).InstallDir -ieq $root){Remove-Item -LiteralPath $registry -Recurse -Force}
Remove-Item -LiteralPath 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\CodeXRemoteWorker' -Recurse -Force -ErrorAction SilentlyContinue
$shortcut=Join-Path ([Environment]::GetFolderPath('Desktop')) 'CodeX 服务启动器.lnk'
if(Test-Path -LiteralPath $shortcut){Remove-Item -LiteralPath $shortcut}
$trayPath=Join-Path $root 'bin\CodexWorkerLauncher.exe'
$trayProcesses=Get-CimInstance Win32_Process -Filter "Name='CodexWorkerLauncher.exe'"|Where-Object{$_.ExecutablePath -ieq $trayPath}
foreach($trayProcess in $trayProcesses){Stop-Process -Id $trayProcess.ProcessId -Force -ErrorAction SilentlyContinue}
foreach($name in @('.runtime','bin','tools','scripts','installer','licenses')){
  $target=[IO.Path]::GetFullPath((Join-Path $root $name))
  if(-not $target.StartsWith($root+'\',[StringComparison]::OrdinalIgnoreCase)){throw 'UNINSTALL_PATH_INVALID'}
  if(Test-Path -LiteralPath $target){Remove-Item -LiteralPath $target -Recurse -Force}
}
foreach($name in @('config.local.json','desktop.settings.json','desktop-status.json','package-manifest.json','Uninstall.exe')){ $target=Join-Path $root $name; if(Test-Path -LiteralPath $target){Remove-Item -LiteralPath $target -Force} }
[IO.File]::WriteAllText((Join-Path $root '保留的数据说明.txt'),'程序已卸载。Data 目录及历史任务已保留；专用运行账户已禁用。',[Text.UTF8Encoding]::new($false))
exit 0



