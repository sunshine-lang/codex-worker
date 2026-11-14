param([string]$Python = "python", [string]$Venv = "$PSScriptRoot\..\.venv")
$ErrorActionPreference = "Stop"
if ($env:OS -ne "Windows_NT") { throw "Run this installer on Windows." }
$Project = (Resolve-Path "$PSScriptRoot\..").Path
& $Python -m venv $Venv
if ($LASTEXITCODE -ne 0) { throw "Virtual environment creation failed." }
$WorkerPython = Join-Path $Venv "Scripts\python.exe"
& $WorkerPython -m pip install $Project
if ($LASTEXITCODE -ne 0) { throw "Package installation failed." }
Write-Output "Worker installed. Point codex_home at the service account's existing Codex configuration and run doctor before starting."
