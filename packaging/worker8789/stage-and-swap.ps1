param([Parameter(Mandatory)][string]$Python, [switch]$PrepareAndInstall)
$ErrorActionPreference = 'Stop'
$root = 'C:\CodexRemoteWorker-AppServer-8789'
$data = 'C:\CodexWorkerData-AppServer-8789'
if (!(Test-Path -LiteralPath (Join-Path $root '.worker8789')) -or
    !(Test-Path -LiteralPath (Join-Path $data '.worker8789'))) { throw 'Expected isolated 8789 instance.' }
$src = Join-Path $root 'src'
foreach ($dir in @($root,$data,$src)) {
    if ((Test-Path -LiteralPath $dir) -and ((Get-Item -LiteralPath $dir).Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw "Unexpected linked directory: $dir" }
}
New-Item -ItemType Directory -Force -Path $src | Out-Null
$id = [guid]::NewGuid().ToString('N')
$stage = Join-Path $src "stage-8789-$id"
$pkg = Join-Path $src 'codex_remote_worker'
if ((Test-Path -LiteralPath $pkg) -and ((Get-Item -LiteralPath $pkg).Attributes -band [IO.FileAttributes]::ReparsePoint)) {
    throw '8789 runtime package is a link.'
}
& $Python -B (Join-Path $PSScriptRoot 'order_materials\stage_runtime.py') --output $stage | Out-Host
if ($LASTEXITCODE -ne 0) { throw '8789 staging validation failed.' }
if (!$PrepareAndInstall) { return $stage }
if (Get-NetTCPConnection -LocalPort 8789 -State Listen -ErrorAction SilentlyContinue) { throw '8789 is already listening.' }
if (Test-Path -LiteralPath $pkg) { throw 'For an existing 8789 runtime use update.ps1.' }
[IO.Directory]::Move($stage,$pkg)
