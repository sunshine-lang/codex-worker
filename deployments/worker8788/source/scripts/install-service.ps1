param(
  [Parameter(Mandatory=$true)][string]$Config,
  [Parameter(Mandatory=$true)][string]$CredentialsFile,
  [string]$Python = "$PSScriptRoot\..\.venv\Scripts\python.exe"
)
$ErrorActionPreference = "Stop"
$Config = (Resolve-Path $Config).Path
$CredentialsFile = (Resolve-Path $CredentialsFile).Path
& $Python -m codex_remote_worker.service --startup auto install
if ($LASTEXITCODE -ne 0) { throw "Windows Service registration failed." }
Get-Service CodexRemoteWorker -ErrorAction Stop | Out-Null
$Parameters = "HKLM:\SYSTEM\CurrentControlSet\Services\CodexRemoteWorker\Parameters"
New-Item -Path $Parameters -Force | Out-Null
New-ItemProperty -Path $Parameters -Name ConfigFile -Value $Config -PropertyType String -Force | Out-Null
New-ItemProperty -Path $Parameters -Name CredentialsFile -Value $CredentialsFile -PropertyType String -Force | Out-Null
Write-Output "Service registered but not started. Configure a dedicated non-admin Log On account in services.msc."
Write-Output "Run the sandbox self-test as that same account before starting the service."
