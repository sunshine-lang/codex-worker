$ErrorActionPreference='Stop'
$workspace=Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
$output=Join-Path $workspace 'build\app-server-8789\Worker8789.exe'
New-Item -ItemType Directory -Force -Path (Split-Path $output -Parent) | Out-Null
& "$env:WINDIR\Microsoft.NET\Framework64\v4.0.30319\csc.exe" /nologo /target:winexe /platform:anycpu /optimize+ /reference:System.dll /reference:System.Core.dll /reference:System.Drawing.dll /reference:System.Windows.Forms.dll /reference:System.Web.Extensions.dll ("/win32manifest:"+(Join-Path $workspace 'packaging\installer\setup.manifest')) ("/out:"+$output) (Join-Path $PSScriptRoot 'Worker8789.cs')
if($LASTEXITCODE -ne 0){throw '8789 desktop build failed'}
Get-FileHash -LiteralPath $output -Algorithm SHA256
