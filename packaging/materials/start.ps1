# Start only the installed materials service and wait for its authenticated readiness response.
$ErrorActionPreference = 'Stop'
$root = 'C:\CodexRemoteWorker-AppServer-8790'
$data = 'C:\CodexWorkerData-AppServer-8790'
$port = (Get-Content -LiteralPath "$root\config.trial.json" -Raw | ConvertFrom-Json).port
if ($port -ne 8787) { throw 'Run packaging\materials\install.ps1 to apply port 8787 before starting.' }
& "$root\desktop\start-worker.ps1"
$secret = (Get-Content -LiteralPath "$data\private\credentials.local.json" -Raw | ConvertFrom-Json).CODEX_WORKER_SERVICE_TOKEN
$deadline = [datetime]::UtcNow.AddSeconds(75)
do {
    $listeners = @(Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue)
    if ($listeners.Count) {
        $receipt = Get-Content -LiteralPath "$data\serve.process.json" -Raw -ErrorAction SilentlyContinue | ConvertFrom-Json
        if (!$receipt.ProcessId -or ($listeners | Where-Object { $_.OwningProcess -ne $receipt.ProcessId })) {
            throw "Port $port is owned by another process. Check the listener before starting the materials Worker."
        }
        try {
            $status = Invoke-RestMethod "http://127.0.0.1:$port/v1/ready" -TimeoutSec 3 -Headers @{Authorization="Bearer $secret"}
            if ($status.ready -and $status.service -eq 'materials') {
                Write-Output "Materials Worker ready on $port, execution slots: $($status.execution_slots)."
                return
            }
        } catch {}
    }
    Start-Sleep -Milliseconds 500
} while ([datetime]::UtcNow -lt $deadline)
throw "Materials Worker did not become ready. See $data\serve.stderr.log."
