# Build the Windows Forms launcher and install it for the current operator.
param([string]$BaseUrl)
$ErrorActionPreference = 'Stop'
$root = 'C:\CodexRemoteWorker-AppServer-8790'
$desktop = Join-Path $root 'desktop'
if (!(Test-Path -LiteralPath "$root\.materials-worker" -PathType Leaf) -or
    !(Test-Path -LiteralPath "$desktop\start-worker.ps1" -PathType Leaf)) {
    throw 'Install the materials service before installing its desktop launcher.'
}
$configFile = Join-Path $desktop 'runtime.json'
if (!$BaseUrl -and (Test-Path -LiteralPath $configFile)) {
    $BaseUrl = (Get-Content -LiteralPath $configFile -Raw | ConvertFrom-Json).base_url
}
if (!$BaseUrl) { $BaseUrl = 'http://127.0.0.1:8787' }
$url = [uri]$BaseUrl
if (!$url.IsAbsoluteUri -or $url.Scheme -notin @('http','https') -or $url.UserInfo -or $url.Query -or $url.Fragment) {
    throw 'Expected a Worker HTTP(S) base URL.'
}
$pwsh = (Get-Process -Id $PID).Path
if ([IO.Path]::GetFileName($pwsh) -ne 'pwsh.exe') { throw 'Run this installer in PowerShell 7.' }
$installedExe = Join-Path $desktop 'Worker8787.exe'
$userExe = Join-Path ([Environment]::GetFolderPath('DesktopDirectory')) 'Worker8787.exe'
$buildDir = Join-Path ([IO.Path]::GetTempPath()) ('materials-desktop-' + [guid]::NewGuid().ToString('N'))
$null = New-Item -ItemType Directory -Path $buildDir
$built = Join-Path $buildDir 'Worker8787.exe'
try {
    $manifest = Join-Path $PSScriptRoot '..\installer\setup.manifest'
    $compiler = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
    & $compiler /nologo /target:winexe /platform:anycpu /optimize+ /codepage:65001 /reference:System.Windows.Forms.dll /reference:System.Drawing.dll /reference:System.Web.Extensions.dll "/win32manifest:$manifest" "/out:$built" "$PSScriptRoot\Worker8787.cs"
    if ($LASTEXITCODE) { throw 'Worker8787 compilation failed.' }
    $hash = (Get-FileHash -LiteralPath $built).Hash
    $installedHash = if (Test-Path -LiteralPath $installedExe) { (Get-FileHash -LiteralPath $installedExe).Hash }
    $userHash = if (Test-Path -LiteralPath $userExe) { (Get-FileHash -LiteralPath $userExe).Hash }
    if ($userHash -and $userHash -ne $installedHash -and $userHash -ne $hash) { throw "Desktop filename already in use: $userExe" }
    if ($userHash -ne $hash -and (Get-Process -Name Worker8787 -ErrorAction SilentlyContinue)) {
        throw 'Exit the running 8787 desktop launcher before upgrading it.'
    }
    Copy-Item -LiteralPath "$PSScriptRoot\start.ps1" -Destination "$desktop\start-materials.ps1" -Force
    Copy-Item -LiteralPath "$PSScriptRoot\stop.ps1" -Destination "$desktop\stop-materials.ps1" -Force
    @{ pwsh=$pwsh; base_url=$BaseUrl.TrimEnd('/') } | ConvertTo-Json | Set-Content -LiteralPath $configFile -Encoding utf8NoBOM
    if ($userHash -ne $hash) { Copy-Item -LiteralPath $built -Destination $userExe -Force }
    if ($installedHash -ne $hash) { Copy-Item -LiteralPath $built -Destination $installedExe -Force }
    Write-Output "Installed $userExe"
} finally {
    if (Test-Path -LiteralPath $built) { Remove-Item -LiteralPath $built -Force }
    [IO.Directory]::Delete($buildDir)
}
