param(
  [Parameter(Mandatory=$true)][string]$Config,
  [Parameter(Mandatory=$true)][string]$CredentialsFile,
  [string]$Python = "$PSScriptRoot\..\.venv\Scripts\python.exe"
)
$ErrorActionPreference = "Stop"
& $Python -m codex_remote_worker doctor --config $Config --credentials-file $CredentialsFile
if ($LASTEXITCODE -ne 0) { throw "Worker not ready. Complete doctor --verify-sandbox under the service account." }
& $Python -m codex_remote_worker serve --config $Config --credentials-file $CredentialsFile
exit $LASTEXITCODE
