$ErrorActionPreference='Stop'
$root='C:\CodexRemoteWorker-AppServer-8788';$data='C:\CodexWorkerData-AppServer-8788'
$family='OpenAI.Codex_2p2nqsd0c76g0'
$manifest='C:\Program Files\WindowsApps\OpenAI.Codex_26.1111.2228.0_x64__2p2nqsd0c76g0\AppxManifest.xml'
$before=Get-AppxPackage -Name OpenAI.Codex
if(-not$before){
 # Register only the existing signed package for this non-administrator user.
 # No ForceApplicationShutdown, package replacement or sandbox-account operations.
 Add-AppxPackage -Register -DisableDevelopmentMode -Path $manifest -ErrorAction Stop
}
$after=Get-AppxPackage -Name OpenAI.Codex
if(-not$after -or $after.PackageFamilyName -ne $family){throw 'Official package registration not confirmed'}
[ordered]@{registered=$true;existing_registration=[bool]$before;package_full_name=$after.PackageFullName;package_family=$after.PackageFamilyName;user=[Security.Principal.WindowsIdentity]::GetCurrent().Name;force_shutdown_requested=$false}|ConvertTo-Json|Set-Content -LiteralPath (Join-Path $data 'package-registration.json') -Encoding UTF8
