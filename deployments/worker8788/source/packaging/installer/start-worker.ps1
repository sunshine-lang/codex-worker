param(
  [ValidateSet('Serve','Doctor','Probe','Initialize','VerifyJson')][string]$Mode = 'Serve',
  [string]$CredentialsFile,
  [switch]$VerifiedStartup
)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path $PSScriptRoot -Parent
$configPath = Join-Path $projectRoot 'config.local.json'
$deployment = Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json
$workerExecutable = Join-Path $projectRoot 'bin\CodexRemoteWorker\CodexRemoteWorker.exe'
if (-not (Test-Path -LiteralPath $workerExecutable)) { $workerExecutable = $deployment.tool_python }
if (-not $CredentialsFile) { $CredentialsFile = Join-Path $deployment.data_root 'private\credentials.local.json' }
$account = Get-Content -LiteralPath (Join-Path $deployment.data_root 'private\service-account.local.json') -Raw | ConvertFrom-Json
$credential = [pscredential]::new($account.username,(ConvertTo-SecureString $account.password -AsPlainText -Force))
function Quote-Argument([string]$Value) {
  if ($Value.Contains('"') -or $Value.Contains("`r") -or $Value.Contains("`n")) { throw 'Unsupported quote or newline in a deployment argument.' }
  return '"' + $Value.TrimEnd('\') + '"'
}
function Command-Line([string[]]$Values) { return (($Values | ForEach-Object { Quote-Argument $_ }) -join ' ') }
$environment = @{}
foreach ($inherited in Get-ChildItem Env:) { if ($inherited.Name -like 'CODEX_*') { $environment[$inherited.Name] = $null } }
$profile = Split-Path $deployment.codex_home -Parent
$environment['OPENAI_API_KEY'] = $null
$environment['OPENROUTER_API_KEY'] = $null
$environment['CODEX_HOME'] = $deployment.codex_home
$environment['USERPROFILE'] = $profile
$environment['APPDATA'] = Join-Path $profile 'AppData\Roaming'
$environment['LOCALAPPDATA'] = Join-Path $profile 'AppData\Local'
$environment['TEMP'] = Join-Path $profile 'AppData\Local\Temp'
$environment['TMP'] = $environment['TEMP']
$environment['USERNAME'] = $account.username.Split('\')[-1]
$environment['USERDOMAIN'] = $env:COMPUTERNAME
$environment['PYTHONUTF8'] = '1'
$environment['PYTHONIOENCODING'] = 'utf-8'
$environment['PATH'] = @((Split-Path $deployment.tool_python),(Join-Path (Split-Path $deployment.tool_python) 'Scripts'),(Split-Path $deployment.codex_executable),(Split-Path $deployment.doc_converter),'C:\Windows\System32','C:\Windows','C:\Windows\System32\WindowsPowerShell\v1.0') -join ';'
function Run-Worker([string]$Command,[switch]$FullVerify) {
  $arguments = @($Command,'--config',$configPath,'--credentials-file',$CredentialsFile)
  if ($workerExecutable -eq $deployment.tool_python) { $arguments = @('-m','codex_remote_worker') + $arguments }
  if ($FullVerify) { $arguments += '--verify-sandbox' }
  $prefix = Join-Path $deployment.data_root ('worker.local.' + $Command)
  $process = Start-Process -FilePath $workerExecutable -ArgumentList (Command-Line $arguments) -Credential $credential -LoadUserProfile -Environment $environment -WorkingDirectory $projectRoot -WindowStyle Hidden -RedirectStandardOutput ($prefix+'.stdout.log') -RedirectStandardError ($prefix+'.stderr.log') -PassThru
  $record = [pscustomobject]@{ProcessId=$process.Id;StartedAt=$process.StartTime.ToUniversalTime().ToString('o');Executable=$workerExecutable;Config=$configPath;StandardOutput=($prefix+'.stdout.log');StandardError=($prefix+'.stderr.log')}
  $record | ConvertTo-Json -Compress | Set-Content -LiteralPath ($prefix+'.process.json') -Encoding utf8
  return $record
}
function Initialize-Sandbox {
  $arguments = @('sandbox','setup','--elevated','--user',$account.username,'--codex-home',$deployment.codex_home)
  $setup = Start-Process -FilePath $deployment.codex_executable -ArgumentList (Command-Line $arguments) -Environment $environment -WorkingDirectory $projectRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $deployment.data_root 'sandbox-setup.stdout.log') -RedirectStandardError (Join-Path $deployment.data_root 'sandbox-setup.stderr.log') -PassThru
  if (-not $setup.WaitForExit(90000)) { Stop-Process -Id $setup.Id -Force; throw 'Sandbox initialization timed out.' }
  if ($setup.ExitCode -ne 0) { throw 'Sandbox initialization failed; inspect sandbox-setup.stderr.log.' }
  $null = & $deployment.tool_python (Join-Path $PSScriptRoot 'wfp-network-lockdown.py')
  if ($LASTEXITCODE -ne 0) { throw 'Sandbox network isolation could not be configured.' }
}
function Wait-Doctor($Record) {
  $process = Get-Process -Id $Record.ProcessId -ErrorAction SilentlyContinue
  if ($process -and -not $process.WaitForExit(120000)) { Stop-Process -Id $process.Id -Force; throw 'Preflight timed out.' }
  $result = Get-Content -LiteralPath $Record.StandardOutput -Raw | ConvertFrom-Json
  if (-not $result.passed -and -not $result.ready) { throw 'Worker preflight failed; inspect the doctor logs.' }
}
if ($Mode -eq 'VerifyJson') {
  $script = Join-Path $projectRoot 'installer\verify-json-runtime.py'
  $arguments = @('-X','utf8',$script,'--config',$configPath,'--credentials-file',$CredentialsFile)
  $prefix = Join-Path $deployment.data_root 'worker.local.json-probe'
  $process = Start-Process -FilePath $deployment.tool_python -ArgumentList (Command-Line $arguments) -Credential $credential -LoadUserProfile -Environment $environment -WorkingDirectory $projectRoot -WindowStyle Hidden -RedirectStandardOutput ($prefix+'.stdout.log') -RedirectStandardError ($prefix+'.stderr.log') -PassThru
  [pscustomobject]@{ProcessId=$process.Id;StartedAt=$process.StartTime.ToUniversalTime().ToString('o');Executable=$deployment.tool_python;StandardOutput=($prefix+'.stdout.log');StandardError=($prefix+'.stderr.log')} | ConvertTo-Json -Compress
  return
}
if ($Mode -eq 'Initialize') { Initialize-Sandbox; [pscustomobject]@{initialized=$true} | ConvertTo-Json -Compress; return }
if ($Mode -eq 'Doctor') { Run-Worker 'doctor' -FullVerify | ConvertTo-Json -Compress; return }
if ($Mode -eq 'Probe') { Run-Worker 'probe' | ConvertTo-Json -Compress; return }
$processRecordPath = Join-Path $deployment.data_root 'worker.local.process.json'
if (Test-Path -LiteralPath $processRecordPath) {
  $record = Get-Content -LiteralPath $processRecordPath -Raw | ConvertFrom-Json
  $existing = Get-Process -Id $record.ProcessId -ErrorAction SilentlyContinue
  if ($existing -and $existing.Path -eq $record.Executable -and $existing.Path -in @($workerExecutable,$deployment.tool_python) -and $existing.StartTime.ToUniversalTime().Ticks -eq ([DateTimeOffset]$record.StartedAt).UtcDateTime.Ticks) { $record | ConvertTo-Json -Compress; return }
}
$listeners = [Net.NetworkInformation.IPGlobalProperties]::GetIPGlobalProperties().GetActiveTcpListeners()
if ($listeners.Port -contains [int]$deployment.port) { throw 'The selected API port is used by another process.' }
if ($VerifiedStartup) { Wait-Doctor (Run-Worker 'doctor') } else { Initialize-Sandbox; Wait-Doctor (Run-Worker 'doctor' -FullVerify) }
$record = Run-Worker 'serve'
$record | ConvertTo-Json -Compress | Set-Content -LiteralPath $processRecordPath -Encoding utf8
$record | ConvertTo-Json -Compress


