# Start only the installed materials service and wait for its authenticated readiness response.
$ErrorActionPreference = 'Stop'
$root = 'C:\CodexRemoteWorker-AppServer-8790'
$data = 'C:\CodexWorkerData-AppServer-8790'
& "$root\desktop\start-worker.ps1"
$secret = (Get-Content -LiteralPath "$data\private\credentials.local.json" -Raw | ConvertFrom-Json).CODEX_WORKER_SERVICE_TOKEN
$deadline = [datetime]::UtcNow.AddSeconds(75)
do {
    try {
        $status = Invoke-RestMethod 'http://127.0.0.1:8790/v1/ready' -TimeoutSec 3 -Headers @{Authorization="Bearer $secret"}
        if ($status.ready -and $status.service -eq 'materials') {
            Write-Output "Materials Worker ready on 8790, execution slots: $($status.execution_slots)."
            return
        }
    } catch {}
    Start-Sleep -Milliseconds 500
} while ([datetime]::UtcNow -lt $deadline)
throw "Materials Worker did not become ready. See $data\serve.stderr.log."
