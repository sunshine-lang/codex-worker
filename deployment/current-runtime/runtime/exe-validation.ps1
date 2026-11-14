param(
  [ValidateSet('Doctor','Diagnostic','Serve')][string]$Mode = 'Serve'
)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path $PSScriptRoot -Parent
$configPath = Join-Path $projectRoot 'config.exe-validation.local.json'
$deployment = Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json
$packagedWorkerPath = Join-Path $projectRoot 'bin\CodexRemoteWorker\CodexRemoteWorker.exe'
$workerExecutable = if (Test-Path -LiteralPath $packagedWorkerPath) { $packagedWorkerPath } else { $deployment.tool_python }
if ($Mode -eq 'Serve') {
  $existingListener = Get-NetTCPConnection -LocalPort $deployment.port -State Listen -ErrorAction SilentlyContinue
  if ($existingListener) {
    $existingMetadataPath = Join-Path $deployment.data_root 'worker.local.process.json'
    if (Test-Path -LiteralPath $existingMetadataPath) {
      $existingMetadata = Get-Content -LiteralPath $existingMetadataPath -Raw | ConvertFrom-Json
      $existingProcess = Get-Process -Id $existingMetadata.ProcessId -ErrorAction SilentlyContinue
      if ($existingProcess -and $existingProcess.Id -in $existingListener.OwningProcess -and
          $existingProcess.Path -eq $existingMetadata.Executable -and $existingProcess.Path -in @($deployment.tool_python,$packagedWorkerPath) -and
          $existingProcess.StartTime.ToUniversalTime().Ticks -eq ([DateTimeOffset]$existingMetadata.StartedAt).UtcDateTime.Ticks) {
        $existingMetadata | ConvertTo-Json -Compress
        return
      }
    }
    throw 'The configured port is already used by another process.'
  }
  $bootstrapAccount = Get-Content -LiteralPath (Join-Path $deployment.data_root 'private\service-account.local.json') -Raw | ConvertFrom-Json
  $setupEnvironment = @{}
  foreach ($inheritedVariable in Get-ChildItem Env:) { if ($inheritedVariable.Name -like 'CODEX_*') { $setupEnvironment[$inheritedVariable.Name] = $null } }
  $setupEnvironment['CODEX_HOME'] = $deployment.codex_home
  $setupEnvironment['PATH'] = @((Split-Path $deployment.tool_python),(Split-Path $deployment.codex_executable),(Split-Path $deployment.doc_converter),'C:\Windows\System32','C:\Windows','C:\Windows\System32\WindowsPowerShell\v1.0') -join ';'
  $setupProcess = Start-Process -FilePath $deployment.codex_executable -ArgumentList @('sandbox','setup','--elevated','--user',$bootstrapAccount.username,'--codex-home',$deployment.codex_home) -Environment $setupEnvironment -WorkingDirectory $projectRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $deployment.data_root 'desktop-sandbox-setup.stdout.log') -RedirectStandardError (Join-Path $deployment.data_root 'desktop-sandbox-setup.stderr.log') -PassThru
  if (-not $setupProcess.WaitForExit(60000)) { Stop-Process -Id $setupProcess.Id -Force; throw 'Sandbox initialization timed out.' }
  if ($setupProcess.ExitCode -ne 0) { throw 'Sandbox initialization did not pass; inspect its logs.' }
  $null = & $deployment.tool_python (Join-Path $PSScriptRoot 'wfp-network-lockdown.py')
  if ($LASTEXITCODE -ne 0) { throw 'Offline sandbox network isolation could not be installed.' }
  $preflightLaunch = & $PSCommandPath -Mode Doctor | ConvertFrom-Json
  $preflightProcess = Get-Process -Id $preflightLaunch.ProcessId -ErrorAction SilentlyContinue
  if ($preflightProcess -and -not $preflightProcess.WaitForExit(90000)) {
    Stop-Process -Id $preflightProcess.Id -Force
    throw 'Preflight timed out; inspect its logs before starting the service.'
  }
  $preflightResult = Get-Content -LiteralPath $preflightLaunch.StandardOutput -Raw | ConvertFrom-Json
  if (-not $preflightResult.passed) { throw 'Sandbox preflight failed; inspect the doctor logs.' }
}
$accountPath = Join-Path $deployment.data_root 'private\service-account.local.json'
$credentialsPath = Join-Path $deployment.data_root 'private\credentials.local.json'
$accountRecord = Get-Content -LiteralPath $accountPath -Raw | ConvertFrom-Json
$workerCredential = [pscredential]::new($accountRecord.username, (ConvertTo-SecureString $accountRecord.password -AsPlainText -Force))
$cleanEnvironment = @{}
foreach ($inheritedVariable in Get-ChildItem Env:) {
  if ($inheritedVariable.Name -like 'CODEX_*') { $cleanEnvironment[$inheritedVariable.Name] = $null }
}
$accountProfile = Split-Path $deployment.codex_home -Parent
$cleanEnvironment['CODEX_HOME'] = $deployment.codex_home
$cleanEnvironment['USERPROFILE'] = $accountProfile
$cleanEnvironment['APPDATA'] = Join-Path $accountProfile 'AppData\Roaming'
$cleanEnvironment['LOCALAPPDATA'] = Join-Path $accountProfile 'AppData\Local'
$cleanEnvironment['TEMP'] = Join-Path $accountProfile 'AppData\Local\Temp'
$cleanEnvironment['TMP'] = $cleanEnvironment['TEMP']
$cleanEnvironment['USERNAME'] = $accountRecord.username.Split('\')[-1]
$cleanEnvironment['USERDOMAIN'] = $env:COMPUTERNAME
$cleanEnvironment['PYTHONUTF8'] = '1'
$cleanEnvironment['PYTHONIOENCODING'] = 'utf-8'
$cleanEnvironment['PATH'] = @(
  (Split-Path $deployment.tool_python),
  (Join-Path (Split-Path $deployment.tool_python) 'Scripts'),
  (Split-Path $deployment.codex_executable),
  (Split-Path $deployment.doc_converter),
  'C:\Windows\System32', 'C:\Windows', 'C:\Windows\System32\WindowsPowerShell\v1.0'
) -join ';'
if ($Mode -eq 'Diagnostic') {
  $workerExecutable = $deployment.tool_python
  $workerArguments = @((Join-Path $PSScriptRoot 'startup_diagnose.py'), $configPath, $credentialsPath)
} else {
  $workerArguments = @($(if ($Mode -eq 'Doctor') {'doctor'} else {'serve'}),'--config',$configPath,'--credentials-file',$credentialsPath)
  if ($workerExecutable -eq $deployment.tool_python) { $workerArguments = @('-m','codex_remote_worker') + $workerArguments }
  if ($Mode -eq 'Doctor') { $workerArguments += @('--verify-sandbox','--startup-trace') }
}
$logPrefix = Join-Path $deployment.data_root ('worker.local.' + $Mode.ToLowerInvariant())
$workerProcess = Start-Process -FilePath $workerExecutable -ArgumentList $workerArguments -Credential $workerCredential -LoadUserProfile -Environment $cleanEnvironment -WorkingDirectory $projectRoot -WindowStyle Hidden -RedirectStandardOutput ($logPrefix + '.stdout.log') -RedirectStandardError ($logPrefix + '.stderr.log') -PassThru
$launchResult = [PSCustomObject]@{ProcessId=$workerProcess.Id;Mode=$Mode;StartedAt=$workerProcess.StartTime.ToUniversalTime().ToString('o');Executable=$workerExecutable;Config=$configPath;StandardOutput=($logPrefix + '.stdout.log');StandardError=($logPrefix + '.stderr.log')}
if ($Mode -eq 'Serve') { $launchResult | ConvertTo-Json -Compress | Set-Content -LiteralPath (Join-Path $deployment.data_root 'worker.local.process.json') -Encoding utf8 }
$launchResult | ConvertTo-Json -Compress





