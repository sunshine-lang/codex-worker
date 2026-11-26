# Install the side-by-side test instance. Run with PowerShell 7 as administrator.
$ErrorActionPreference = 'Stop'
$workspace = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
$snapshot = Join-Path $workspace 'deployments\worker8788'
$base = 'C:\CodexRemoteWorker'
$prodRoot = 'C:\CodexRemoteWorker-AppServer-8788'
$root = 'C:\CodexRemoteWorker-AppServer-8789'
$data = 'C:\CodexWorkerData-AppServer-8789'
$desktop = Join-Path $root 'desktop'
$recovery = 'CodexWorker8789-Recovery'
$marker = Join-Path $root '.worker8789'
if (Get-NetTCPConnection -LocalPort 8789 -State Listen -ErrorAction SilentlyContinue) {
    throw '8789 is already listening. Deploy code updates only after draining and stopping that instance.'
}
if (Test-Path -LiteralPath $root) {
    if ((Get-Item -LiteralPath $root).Attributes -band [IO.FileAttributes]::ReparsePoint -or
        !(Test-Path -LiteralPath $marker)) { throw 'Unrecognized 8789 installation.' }
} else {
    New-Item -ItemType Directory -Path $root | Out-Null
    $acl = [Security.AccessControl.DirectorySecurity]::new()
    $acl.SetAccessRuleProtection($true, $false)
    $serviceSid = (Get-LocalUser -Name CodexWorkerSvc).SID
    foreach ($sid in @('S-1-5-18','S-1-5-32-544',$serviceSid.Value)) {
        $rights = if ($sid -eq $serviceSid.Value) {'ReadAndExecute'} else {'FullControl'}
        $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new(
            [Security.Principal.SecurityIdentifier]::new($sid),$rights,'ContainerInherit,ObjectInherit','None','Allow'))
    }
    Set-Acl -LiteralPath $root -AclObject $acl
    New-Item -ItemType File -Path $marker | Out-Null
}
& (Join-Path $PSScriptRoot 'initialize-data.ps1')
New-Item -ItemType Directory -Force -Path $desktop | Out-Null
# Stage the development-only runtime from the synchronized repository source.
$python = Join-Path $base '.runtime\python313\python.exe'
& (Join-Path $PSScriptRoot 'stage-and-swap.ps1') -Python $python -PrepareAndInstall
foreach ($name in @('launch-packaged-user.ps1','packaged-background.py','worker-app-server.py')) {
    $contents = [IO.File]::ReadAllText((Join-Path $snapshot "runtime\$name")).Replace('8788','8789')
    [IO.File]::WriteAllText((Join-Path $root $name),$contents)
}
foreach ($name in @('start-worker.ps1','watchdog.ps1','settings8789.py','login.ps1')) {
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot $name) -Destination $desktop
}
$pwsh = (Get-Process -Id $PID).Path
@{pwsh=$pwsh} | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $desktop 'runtime.json') -Encoding utf8NoBOM
$instanceConfig = Join-Path $root 'config.trial.json'
if (!(Test-Path -LiteralPath $instanceConfig)) {
    $cfg = Get-Content -Raw -LiteralPath (Join-Path $base 'config.local.json') | ConvertFrom-Json -AsHashtable
    $production = Get-Content -Raw -LiteralPath (Join-Path $prodRoot 'config.trial.json') | ConvertFrom-Json
    $cfg.protected_read_roots = @($cfg.protected_read_roots | Where-Object {$_}) + @($cfg.data_root,$cfg.codex_home,$production.data_root,$prodRoot)
    $cfg.data_root = $data
    $cfg.codex_home = Join-Path $data 'private\codex-home'
    $cfg.port = 8789
    $cfg.bind_host = '0.0.0.0'
    foreach ($field in @('codex_executable','expected_codex_version','authentication_mode')) { $cfg[$field] = $production.$field }
    $cfg.tool_read_roots = @($cfg.tool_read_roots) + @(Split-Path $cfg.codex_executable -Parent)
} else {
    $cfg = Get-Content -Raw -LiteralPath $instanceConfig | ConvertFrom-Json -AsHashtable
    if ($cfg.port -ne 8789 -or $cfg.data_root -ne $data) { throw 'Unexpected existing 8789 configuration.' }
}
$cfg.bind_host = '0.0.0.0'
$cfg.max_concurrent_jobs = 5
$cfg.task_batch_concurrency = 1
$cfg.project_context_enabled = $true
$cfg.continuous_project_session = $true
$cfg | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $instanceConfig -Encoding utf8NoBOM
$exe = Join-Path $workspace 'build\app-server-8789\Worker8789.exe'
$installedExe = Join-Path $desktop 'Worker8789.exe'
if (!(Test-Path -LiteralPath $installedExe) -or (Get-FileHash $exe).Hash -ne (Get-FileHash $installedExe).Hash) {
    Copy-Item -LiteralPath $exe -Destination $installedExe
}
$userExe = Join-Path ([Environment]::GetFolderPath('Desktop')) 'Worker8789.exe'
if ((Test-Path -LiteralPath $userExe) -and
    (Get-FileHash -LiteralPath $userExe).Hash -ne (Get-FileHash -LiteralPath $exe).Hash -and
    !(Test-Path -LiteralPath (Join-Path $root '.desktop-installed'))) { throw 'Desktop filename is already taken.' }
if (!(Test-Path -LiteralPath $userExe) -or (Get-FileHash $exe).Hash -ne (Get-FileHash $userExe).Hash) {
    Copy-Item -LiteralPath $exe -Destination $userExe
}
New-Item -ItemType File -Force -Path (Join-Path $root '.desktop-installed') | Out-Null
$action = New-ScheduledTaskAction -Execute $pwsh -Argument ('-NoProfile -NonInteractive -WindowStyle Hidden -File "'+(Join-Path $desktop 'watchdog.ps1')+'"') -WorkingDirectory $desktop
$existing = Get-ScheduledTask -TaskName $recovery -ErrorAction SilentlyContinue
if ($existing -and $existing.Actions.Arguments -ne $action.Arguments) { throw 'Recovery task name already in use.' }
# Desktop package activation requires an interactive Windows session on this host.
$operatorName = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$onLogon = New-ScheduledTaskTrigger -AtLogOn -User $operatorName
$principal = New-ScheduledTaskPrincipal -UserId $operatorName -LogonType Interactive -RunLevel Highest
$repeat = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 1)
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 4) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName $recovery -Action $action -Trigger @($onLogon,$repeat) -Settings $settings -Principal $principal -Force | Out-Null
& (Join-Path $PSScriptRoot 'public-firewall.ps1')
Start-ScheduledTask -TaskName $recovery
Write-Output "Installed $userExe and started $recovery."
