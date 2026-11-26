$ErrorActionPreference='Stop'
$workspace=Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
$output=Join-Path $workspace 'build\app-server-8788\Worker8788.exe'
New-Item -ItemType Directory -Force -Path (Split-Path $output -Parent) | Out-Null
& "$env:WINDIR\Microsoft.NET\Framework64\v4.0.30319\csc.exe" /nologo /target:winexe "/win32icon:$PSScriptRoot\Worker8788.ico" /platform:anycpu /optimize+ /reference:System.dll /reference:System.Core.dll /reference:System.Drawing.dll /reference:System.Windows.Forms.dll /reference:System.Web.Extensions.dll ("/win32manifest:"+(Join-Path $workspace 'packaging\installer\setup.manifest')) ("/out:"+$output) (Join-Path $PSScriptRoot 'Worker8788.cs')
if($LASTEXITCODE -ne 0){throw '8788 desktop build failed'}
Get-FileHash -LiteralPath $output -Algorithm SHA256
