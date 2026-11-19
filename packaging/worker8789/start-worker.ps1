param([ValidateSet('Serve')][string]$Mode='Serve')
$ErrorActionPreference = 'Stop'
$root = 'C:\CodexRemoteWorker-AppServer-8789'
$data = 'C:\CodexWorkerData-AppServer-8789'
$base = Get-Content -LiteralPath 'C:\CodexRemoteWorker\config.local.json' -Raw | ConvertFrom-Json
$account = Get-Content -LiteralPath (Join-Path $base.data_root 'private\service-account.local.json') -Raw | ConvertFrom-Json
$credential = [pscredential]::new($account.username,(ConvertTo-SecureString $account.password -AsPlainText -Force))
$lock = [IO.File]::Open((Join-Path $data '.launch-control.lock'),'OpenOrCreate','ReadWrite','None')
try {
    if (Get-NetTCPConnection -LocalPort 8789 -State Listen -ErrorAction SilentlyContinue) { return }
    $id = [guid]::NewGuid().ToString()
    $argsList = @('-NoProfile','-NonInteractive','-File',(Join-Path $root 'launch-packaged-user.ps1'),'-Mode','serve','-OperationId',$id)
    # The existing worker account already has a registered desktop package.
    # Explicitly skip profile loading. The Python entrypoint records its own logs.
    $launch = Start-Process -FilePath "$env:WINDIR\System32\WindowsPowerShell\v1.0\powershell.exe" -ArgumentList $argsList -LoadUserProfile:$false -Credential $credential -WorkingDirectory $root -WindowStyle Hidden -PassThru
    if (!$launch.WaitForExit(30000) -or $launch.ExitCode -ne 0) { throw 'Windows package activation failed.' }
    $deadline = [datetime]::UtcNow.AddSeconds(60)
    do {
        $receipt = Get-Content -LiteralPath (Join-Path $data 'serve.process.json') -Raw -ErrorAction SilentlyContinue | ConvertFrom-Json
        if ($receipt.operation_id -eq $id) { return }
        Start-Sleep -Milliseconds 300
    } while ([datetime]::UtcNow -lt $deadline)
    throw '8789 did not record its startup.'
} finally {
    $lock.Dispose()
}
