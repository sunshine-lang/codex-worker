$ErrorActionPreference = 'Stop'
$env:CODEX_HOME = 'C:\CodexWorkerData-AppServer-8789\private\codex-home'
$config = Get-Content -LiteralPath 'C:\CodexRemoteWorker-AppServer-8789\config.trial.json' -Raw | ConvertFrom-Json
if ($config.port -ne 8789 -or $config.codex_home -ne $env:CODEX_HOME) { throw 'Wrong instance configuration.' }
# The official browser flow writes only to this instance's private Codex home.
& $config.codex_executable login
exit $LASTEXITCODE
