param([ValidateSet('Serve')][string]$Mode = 'Serve')
$ErrorActionPreference = 'Stop'
$root = 'C:\CodexRemoteWorker-AppServer-8789'
$data = 'C:\CodexWorkerData-AppServer-8789'

$lockPath = Join-Path $data '.launch-control.lock'
$lockDeadline = [datetime]::UtcNow.AddSeconds(90)
while ($true) {
    if (Get-NetTCPConnection -LocalPort 8789 -State Listen -ErrorAction SilentlyContinue) { return }
    try {
        $lock = [IO.File]::Open($lockPath, 'OpenOrCreate', 'ReadWrite', 'None')
        break
    } catch [IO.IOException] {
        if (($_.Exception.GetBaseException().HResult -band 0xffff) -notin 32,33) { throw }
        if ([datetime]::UtcNow -ge $lockDeadline) { throw 'Timed out waiting for the 8789 startup lock.' }
        Start-Sleep -Milliseconds 500
    }
}

try {
    if (Get-NetTCPConnection -LocalPort 8789 -State Listen -ErrorAction SilentlyContinue) { return }
    # The previous launch may have recorded its process but not opened the port yet.
    $entrypoint = Join-Path $root 'packaged-background.py'
    $starting = Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" | Where-Object {
        $_.CommandLine -match ([regex]::Escape($entrypoint) + '"?\s+serve(?:\s|$)')
    }
    if ($starting) { return }

    $base = Get-Content -LiteralPath 'C:\CodexRemoteWorker\config.local.json' -Raw | ConvertFrom-Json
    $account = Get-Content -LiteralPath (Join-Path $base.data_root 'private\service-account.local.json') -Raw | ConvertFrom-Json
    $credential = [pscredential]::new($account.username, (ConvertTo-SecureString $account.password -AsPlainText -Force))
    $sid = [Security.Principal.NTAccount]::new($credential.UserName).Translate([Security.Principal.SecurityIdentifier]).Value
    # Package activation needs this account's Windows profile even before its first logon after boot.
    $loadProfile = -not (Test-Path -LiteralPath ('Registry::HKEY_USERS\' + $sid))
    if ($loadProfile) {
        $profileKey = 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\ProfileList\' + $sid
        $registered = Get-ItemProperty -LiteralPath $profileKey -Name ProfileImagePath
        $profileDir = [Environment]::ExpandEnvironmentVariables($registered.ProfileImagePath)
        if ((Test-Path -LiteralPath "$profileKey.bak") -or
            -not (Test-Path -LiteralPath (Join-Path $profileDir 'NTUSER.DAT') -PathType Leaf)) {
            throw 'The registered Windows profile of the 8789 service account needs repair.'
        }
    }

    $id = [guid]::NewGuid().ToString()
    $argsList = @('-NoLogo','-NoProfile','-NonInteractive','-File',(Join-Path $root 'launch-packaged-user.ps1'),'-Mode','serve','-OperationId',$id)
    $launch = Start-Process -FilePath "$env:WINDIR\System32\WindowsPowerShell\v1.0\powershell.exe" -ArgumentList $argsList -LoadUserProfile:$loadProfile -Credential $credential -WorkingDirectory $root -WindowStyle Hidden -PassThru
    try {
        $receiptPath = Join-Path $data 'serve.process.json'
        $deadline = [datetime]::UtcNow.AddSeconds(90)
        while ($true) {
            $receipt = Get-Content -LiteralPath $receiptPath -Raw -ErrorAction SilentlyContinue | ConvertFrom-Json
            if ($receipt.operation_id -eq $id) { return }
            if ($launch.HasExited -and $launch.ExitCode -ne 0) {
                throw "Windows package activation failed for 8789 (exit $($launch.ExitCode))."
            }
            if ([datetime]::UtcNow -ge $deadline) { break }
            Start-Sleep -Milliseconds 300
        }
        # Only terminate the activation helper created by this call.
        if (-not $launch.HasExited) {
            try { $launch.Kill() } catch [InvalidOperationException] { if (-not $launch.HasExited) { throw } }
        }
        throw '8789 did not record its startup within 90 seconds.'
    } finally {
        $launch.Dispose()
    }
} finally {
    $lock.Dispose()
}
