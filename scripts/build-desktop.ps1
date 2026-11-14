param([string]$Python = "$PSScriptRoot\..\.runtime\python313\python.exe")
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path $PSScriptRoot -Parent
$distRoot = Join-Path $projectRoot 'dist\windows-exe'
New-Item -ItemType Directory -Path $distRoot -Force | Out-Null
& $Python -m PyInstaller --noconfirm --clean --onedir --console --name CodexRemoteWorker --paths (Join-Path $projectRoot 'src') --add-data ((Join-Path $projectRoot 'src\codex_remote_worker\task_tools.py') + ';codex_remote_worker') --collect-submodules uvicorn --hidden-import win32timezone --hidden-import win32api --hidden-import win32con --hidden-import win32event --hidden-import win32file --hidden-import win32job --hidden-import win32process --hidden-import win32security --hidden-import win32com.shell.shell --hidden-import pythoncom --hidden-import pywintypes --distpath $distRoot --workpath (Join-Path $projectRoot 'build\pyinstaller-desktop') --specpath (Join-Path $projectRoot 'build\exe-entry') (Join-Path $projectRoot 'packaging\worker_entry.py')
if ($LASTEXITCODE -ne 0) { throw 'Worker EXE build failed.' }
$compiler = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
& $compiler /nologo /target:winexe /platform:anycpu /optimize+ /reference:System.dll /reference:System.Core.dll /reference:System.Drawing.dll /reference:System.Windows.Forms.dll /reference:System.Web.Extensions.dll ('/win32manifest:' + (Join-Path $projectRoot 'packaging\desktop.manifest')) ('/out:' + (Join-Path $distRoot 'CodexWorkerLauncher.exe')) (Join-Path $projectRoot 'packaging\installer\TrayLauncher.cs')
if ($LASTEXITCODE -ne 0) { throw 'Desktop launcher build failed.' }
Get-FileHash -LiteralPath (Join-Path $distRoot 'CodexWorkerLauncher.exe'),(Join-Path $distRoot 'CodexRemoteWorker\CodexRemoteWorker.exe') -Algorithm SHA256 | Select-Object Path,Hash
