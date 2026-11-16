param([string]$Stage="$PSScriptRoot\..\build\installer-stage",[string]$Python='C:\CodexRemoteWorker\.runtime\python313\python.exe',[switch]$ReuseArchive,[string]$Output="$PSScriptRoot\..\dist\migration-0.3.0\CodexWorkerSetup-0.3.0.exe")
$ErrorActionPreference='Stop'
$project=Split-Path $PSScriptRoot -Parent
$source=Join-Path $project 'packaging\installer'
$stageApp=Join-Path $Stage 'app'
$compiler=Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
foreach($required in @('.runtime\python313\python.exe','.runtime\powershell\pwsh.exe','tools\codex\bin\codex.exe','tools\LibreOffice\program\soffice.exe','bin\CodexRemoteWorker\CodexRemoteWorker.exe')){if(-not(Test-Path -LiteralPath (Join-Path $stageApp $required))){throw ('Prepare the runtime stage before building: '+$required)}}
Copy-Item -LiteralPath (Join-Path $source 'configure-install.ps1'),(Join-Path $source 'uninstall.ps1'),(Join-Path $source 'write_model_config.py'),(Join-Path $source 'verify-json-runtime.py') -Destination (Join-Path $stageApp 'installer') -Force
Copy-Item -LiteralPath (Join-Path $source 'start-worker.ps1') -Destination (Join-Path $stageApp '.runtime\start-worker.local.ps1') -Force
Copy-Item -LiteralPath (Join-Path $source 'wfp-network-lockdown.py') -Destination (Join-Path $stageApp '.runtime\wfp-network-lockdown.py') -Force
Copy-Item -LiteralPath (Join-Path $source 'desktop-start.ps1'),(Join-Path $source 'desktop-stop.ps1') -Destination (Join-Path $stageApp 'scripts') -Force
Copy-Item -LiteralPath (Join-Path $project 'packaging\desktop-repair.ps1') -Destination (Join-Path $stageApp 'scripts\desktop-repair.ps1') -Force
Copy-Item -LiteralPath (Join-Path $project 'packaging\desktop-ensure.ps1') -Destination (Join-Path $stageApp 'scripts\desktop-ensure.ps1') -Force
&$compiler /nologo /target:winexe /platform:x64 /optimize+ /reference:System.dll /reference:System.Core.dll /reference:System.Drawing.dll /reference:System.Windows.Forms.dll /reference:System.Web.Extensions.dll ('/win32manifest:'+(Join-Path $project 'packaging\desktop.manifest')) ('/out:'+(Join-Path $stageApp 'bin\CodexWorkerLauncher.exe')) (Join-Path $source 'TrayLauncher.cs')
if($LASTEXITCODE -ne 0){throw 'Tray compilation failed.'}
&$compiler /nologo /target:winexe /platform:x64 /optimize+ /reference:System.dll /reference:System.Core.dll /reference:System.Drawing.dll /reference:System.Windows.Forms.dll ('/win32manifest:'+(Join-Path $source 'setup.manifest')) ('/out:'+(Join-Path $stageApp 'Uninstall.exe')) (Join-Path $source 'Maintenance.cs')
if($LASTEXITCODE -ne 0){throw 'Uninstaller compilation failed.'}
$stub=Join-Path $project 'build\CodeXSetupStub.exe'
&$compiler /nologo /target:winexe /platform:x64 /optimize+ /reference:System.dll /reference:System.Core.dll /reference:System.Drawing.dll /reference:System.Windows.Forms.dll /reference:System.Web.Extensions.dll /reference:System.IO.Compression.dll ('/win32manifest:'+(Join-Path $source 'setup.manifest')) ('/out:'+$stub) (Join-Path $source 'SetupWizard.cs')
if($LASTEXITCODE -ne 0){throw 'Installer compilation failed.'}
$arguments=@((Join-Path $project 'scripts\build-installer-payload.py'),'--stage',$Stage,'--stub',$stub,'--output',$Output)
if($ReuseArchive){$arguments+='--reuse-archive'}
&$Python @arguments
if($LASTEXITCODE -ne 0){throw 'Installer payload build failed.'}
