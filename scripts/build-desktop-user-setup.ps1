param([string]$Stage="$PSScriptRoot\..\build\desktop-subscription-stage",[string]$Python='C:\CodexRemoteWorker\.runtime\python313\python.exe')
$ErrorActionPreference='Stop'
$project=Split-Path $PSScriptRoot -Parent
$source=Join-Path $project 'packaging\desktop-user'
$app=Join-Path $Stage 'app'
$release=Join-Path $project 'dist\desktop-subscription-0.4.0'
$compiler=Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
New-Item -ItemType Directory -Path $release,(Join-Path $app 'installer'),(Join-Path $app 'scripts') -Force|Out-Null
foreach($name in @('configure-install.ps1','uninstall.ps1','verify-json-runtime.py')){Copy-Item -LiteralPath (Join-Path $source $name) -Destination (Join-Path $app 'installer') -Force}
Copy-Item -LiteralPath (Join-Path $source 'start-worker.ps1') -Destination (Join-Path $app '.runtime\start-worker.local.ps1') -Force
foreach($name in @('desktop-start.ps1','desktop-stop.ps1','desktop-ensure.ps1','desktop-repair.ps1')){Copy-Item -LiteralPath (Join-Path $source $name) -Destination (Join-Path $app 'scripts') -Force}
foreach($relative in @('installer\write_model_config.py','.runtime\wfp-network-lockdown.py')){$path=Join-Path $app $relative;if(Test-Path -LiteralPath $path){Remove-Item -LiteralPath $path -Force}}
&$compiler /nologo /target:winexe /platform:x64 /optimize+ /reference:System.dll /reference:System.Core.dll /reference:System.Drawing.dll /reference:System.Windows.Forms.dll /reference:System.Web.Extensions.dll ('/win32manifest:'+(Join-Path $source 'setup.manifest')) ('/out:'+(Join-Path $app 'bin\CodexWorkerLauncher.exe')) (Join-Path $source 'TrayLauncher.cs')
if($LASTEXITCODE -ne 0){throw 'Desktop launcher compilation failed'}
&$compiler /nologo /target:winexe /platform:x64 /optimize+ /reference:System.dll /reference:System.Core.dll /reference:System.Drawing.dll /reference:System.Windows.Forms.dll ('/win32manifest:'+(Join-Path $source 'setup.manifest')) ('/out:'+(Join-Path $app 'Uninstall.exe')) (Join-Path $source 'Maintenance.cs')
if($LASTEXITCODE -ne 0){throw 'Desktop uninstaller compilation failed'}
$stub=Join-Path $project 'build\desktop-subscription\SetupStub.exe'
&$compiler /nologo /target:winexe /platform:x64 /optimize+ /reference:System.dll /reference:System.Core.dll /reference:System.Drawing.dll /reference:System.Windows.Forms.dll /reference:System.Web.Extensions.dll /reference:System.IO.Compression.dll ('/win32manifest:'+(Join-Path $source 'setup.manifest')) ('/out:'+$stub) (Join-Path $source 'SetupWizard.cs')
if($LASTEXITCODE -ne 0){throw 'Desktop installer compilation failed'}
$output=Join-Path $release 'CodexWorkerSetup-0.4.0-Desktop.exe'
$cache=Join-Path $release 'CodexWorkerSetup-0.4.0-Desktop.payload.zip'
$previous=Join-Path $project 'build\migration-0.4.0\archive-cache\CodexWorkerSetup-0.4.0.payload.zip'
if(-not(Test-Path -LiteralPath $cache) -and (Test-Path -LiteralPath $previous)){Copy-Item -LiteralPath $previous -Destination $cache}
&$Python -X utf8 (Join-Path $project 'scripts\build-installer-payload.py') --stage $Stage --stub $stub --output $output --reuse-archive
if($LASTEXITCODE -ne 0){throw 'Desktop subscription payload build failed'}
