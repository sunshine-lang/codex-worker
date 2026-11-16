param([Parameter(Mandatory=$true)][string]$InstallRoot)
$ErrorActionPreference='Stop'
$root=[IO.Path]::GetFullPath($InstallRoot).TrimEnd('\');$marker=Get-Content -LiteralPath (Join-Path $root 'Data\private\installation.json') -Raw|ConvertFrom-Json
if($marker.variant -ne 'desktop-subscription' -or $marker.install_dir -ine $root -or $marker.desktop_user_sid -ne [Security.Principal.WindowsIdentity]::GetCurrent().User.Value -or $root -eq [IO.Path]::GetPathRoot($root)){throw 'INSTALLATION_IDENTITY_INVALID'}
& (Join-Path $root '.runtime\powershell\pwsh.exe') -NoLogo -NoProfile -NonInteractive -File (Join-Path $root 'scripts\desktop-stop.ps1');if($LASTEXITCODE -ne 0){throw 'SERVICE_STOP_FAILED'}
$tray=Join-Path $root 'bin\CodexWorkerLauncher.exe';Get-CimInstance Win32_Process -Filter "Name='CodexWorkerLauncher.exe'"|Where-Object{$_.ExecutablePath -ieq $tray}|ForEach-Object{Stop-Process -Id $_.ProcessId -Force}
foreach($name in @('.runtime','bin','tools','scripts','installer','licenses')){$target=[IO.Path]::GetFullPath((Join-Path $root $name));if(-not$target.StartsWith($root+'\',[StringComparison]::OrdinalIgnoreCase)){throw 'UNINSTALL_PATH_INVALID'};if(Test-Path -LiteralPath $target){Remove-Item -LiteralPath $target -Recurse -Force}}
foreach($name in @('config.local.json','desktop.settings.json','desktop-status.json','package-manifest.json','Uninstall.exe')){$target=Join-Path $root $name;if(Test-Path -LiteralPath $target){Remove-Item -LiteralPath $target -Force}}
foreach($key in @('HKCU:\SOFTWARE\CodexRemoteWorker\Desktop','HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\CodexWorkerDesktop')){Remove-Item -LiteralPath $key -Recurse -Force -ErrorAction SilentlyContinue}
$shortcut=Join-Path ([Environment]::GetFolderPath('Desktop')) 'Codex Worker 官方订阅版.lnk';if(Test-Path -LiteralPath $shortcut){Remove-Item -LiteralPath $shortcut}
[IO.File]::WriteAllText((Join-Path $root '保留的数据说明.txt'),'已移除本用户 Worker。Data 历史数据已保留；桌面端 Codex 配置、登录和沙箱未删除。',[Text.UTF8Encoding]::new($false))
