# Run once as administrator. Only public model settings are seeded from 8788.
$ErrorActionPreference = 'Stop'
$data = 'C:\CodexWorkerData-AppServer-8789'
$sourceData = 'C:\CodexWorkerData-AppServer-8788'
$codexHome = Join-Path $data 'private\codex-home'
$marker = Join-Path $data '.worker8789'
if (Test-Path -LiteralPath $data) {
    if ((Get-Item -LiteralPath $data).Attributes -band [IO.FileAttributes]::ReparsePoint -or
        !(Test-Path -LiteralPath $marker)) { throw 'Unrecognized 8789 data directory.' }
} else {
    New-Item -ItemType Directory -Path $data | Out-Null
    Set-Acl -LiteralPath $data -AclObject (Get-Acl -LiteralPath $sourceData)
    New-Item -ItemType File -Path $marker | Out-Null
}
New-Item -ItemType Directory -Force -Path $codexHome | Out-Null
$creds = Join-Path $data 'private\credentials.local.json'
if (!(Test-Path -LiteralPath $creds)) {
    $bytes = [Security.Cryptography.RandomNumberGenerator]::GetBytes(48)
    @{ CODEX_WORKER_SERVICE_TOKEN = [Convert]::ToBase64String($bytes) } |
        ConvertTo-Json | Set-Content -LiteralPath $creds -Encoding utf8NoBOM
}
$sourceHome = Join-Path $sourceData 'private\codex-home'
$config = Join-Path $codexHome 'config.toml'
if (!(Test-Path -LiteralPath $config)) {
    $lines = @(Get-Content -LiteralPath (Join-Path $sourceHome 'config.toml') |
        Where-Object { $_ -match '^\s*"?(model|model_reasoning_effort|model_provider)"?\s*=\s*"[A-Za-z0-9./_-]+"\s*$' })
    if (!($lines -match '^\s*"?model_provider"?\s*=\s*"openai"')) {
        throw 'This installation expects the official ChatGPT provider.'
    }
    @($lines; 'cli_auth_credentials_store = "file"'; 'thread_unload_delay_secs = 0'; '[windows]'; 'sandbox = "elevated"') |
        Set-Content -LiteralPath $config -Encoding utf8NoBOM
}
foreach ($name in @('models_cache.json','worker-models.json')) {
    $target = Join-Path $codexHome $name
    if (!(Test-Path -LiteralPath $target)) {
        Copy-Item -LiteralPath (Join-Path $sourceHome $name) -Destination $target
    }
}
