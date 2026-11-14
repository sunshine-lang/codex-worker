param([Parameter(Mandatory=$true)][string]$Python)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path $PSScriptRoot -Parent
$Python = (Resolve-Path -LiteralPath $Python).Path
$distRoot = Join-Path $projectRoot 'dist\concurrency-runtime'
New-Item -ItemType Directory -Path $distRoot -Force | Out-Null

# Build only the Worker. The installed launcher, account and sandbox scripts stay in place.
& $Python -m PyInstaller --version
if ($LASTEXITCODE -ne 0) { throw 'PyInstaller must be installed in the selected build Python.' }
& $Python -m PyInstaller --noconfirm --clean --onedir --console --name CodexRemoteWorker --paths (Join-Path $projectRoot 'src') --add-data ((Join-Path $projectRoot 'src\codex_remote_worker\task_tools.py') + ';codex_remote_worker') --collect-submodules uvicorn --hidden-import win32timezone --hidden-import win32api --hidden-import win32con --hidden-import win32event --hidden-import win32file --hidden-import win32job --hidden-import win32process --hidden-import win32security --hidden-import win32com.shell.shell --hidden-import pythoncom --hidden-import pywintypes --distpath $distRoot --workpath (Join-Path $projectRoot 'build\concurrency-runtime') --specpath (Join-Path $projectRoot 'build\concurrency-spec') (Join-Path $projectRoot 'packaging\worker_entry.py')
if ($LASTEXITCODE -ne 0) { throw 'Worker EXE build failed; no deployed files were changed.' }
& $Python (Join-Path $PSScriptRoot 'write-build-manifest.py') --source-root $projectRoot --runtime-root (Join-Path $distRoot 'CodexRemoteWorker')
if ($LASTEXITCODE -ne 0) { throw 'Build manifest generation failed.' }
Write-Host "Built runtime: $distRoot\CodexRemoteWorker"
Write-Host 'Deployment has NOT been performed. Follow docs/CONCURRENCY_UPGRADE.md.'
