# Request a graceful shutdown from the specific registered materials process.
$ErrorActionPreference = 'Stop'
$data = 'C:\CodexWorkerData-AppServer-8790'
$root = 'C:\CodexRemoteWorker-AppServer-8790'
$gate = [IO.File]::Open((Join-Path $data '.launch-control.lock'), 'OpenOrCreate', 'ReadWrite', 'None')
try {
    $listeners = @(Get-NetTCPConnection -State Listen -LocalPort 8787 -ErrorAction SilentlyContinue)
    $receiptPath = Join-Path $data 'serve.process.json'
    if (!(Test-Path -LiteralPath $receiptPath)) {
        if ($listeners.Count) { throw '8787 has a listener without a materials startup receipt.' }
        return
    }
    $receipt = Get-Content -LiteralPath $receiptPath -Raw | ConvertFrom-Json
    $operation = [guid]::Parse($receipt.operation_id).ToString()
    $workerId = [int]$receipt.ProcessId
    if ($listeners | Where-Object { $_.OwningProcess -ne $workerId }) {
        throw '8787 belongs to another process.'
    }
    $worker = Get-Process -Id $workerId -ErrorAction SilentlyContinue
    if (!$worker) { return }
    try {
        # Keep a handle to this process through the identity check and shutdown.
        $null = $worker.Handle
        $record = Get-CimInstance Win32_Process -Filter "ProcessId=$workerId"
        $expected = [regex]::Escape((Join-Path $root 'packaged-background.py'))
        if (!$record -or $record.CommandLine -notmatch ('(?i)"?' + $expected + '"?\s+serve\s+--operation-id\s+' + $operation + '(?:\s|$)')) {
            if ($listeners.Count) { throw 'Materials process identity mismatch.' }
            return
        }
        if ($receipt.desktop_stop -ne $true) { throw 'This materials process predates desktop stop support. Upgrade and restart it first.' }
        [IO.File]::WriteAllText((Join-Path $data "serve.stop.$operation"), '')
        if (!$worker.WaitForExit(300000)) { throw 'The materials process has not finished shutting down. See serve.stderr.log.' }
    } finally { $worker.Dispose() }
} finally { $gate.Dispose() }
