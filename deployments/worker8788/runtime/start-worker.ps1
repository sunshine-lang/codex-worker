param([ValidateSet('IdentityProbe','ProtocolProbe','ProtocolProbeNoEnvironment','Doctor','Serve')][string]$Mode='Serve')
$ErrorActionPreference='Stop'
$root='C:\CodexRemoteWorker-AppServer-8788';$data='C:\CodexWorkerData-AppServer-8788'
$base='C:\CodexRemoteWorker';$configPath=Join-Path $root 'config.trial.json'
$baseConfig=Get-Content -LiteralPath (Join-Path $base 'config.local.json') -Raw|ConvertFrom-Json
$trialConfig=Get-Content -LiteralPath $configPath -Raw|ConvertFrom-Json
$account=Get-Content -LiteralPath (Join-Path $baseConfig.data_root 'private\service-account.local.json') -Raw|ConvertFrom-Json
$credential=[pscredential]::new($account.username,(ConvertTo-SecureString $account.password -AsPlainText -Force))
$environment=@{}
foreach($item in Get-ChildItem Env:){if($item.Name -like 'CODEX_*'){$environment[$item.Name]=$null}}
$profile=Split-Path $baseConfig.codex_home -Parent
$environment['CODEX_WINDOWS_REGISTERED_CORE']='1'
$environment['CODEX_WINDOWS_SANDBOX_PACKAGE_FAMILY']='OpenAI.Codex_2p2nqsd0c76g0'
$environment['CODEX_HOME']=if($trialConfig.codex_home){$trialConfig.codex_home}else{$baseConfig.codex_home};$environment['USERPROFILE']=$profile
$environment['APPDATA']=Join-Path $profile 'AppData\Roaming';$environment['LOCALAPPDATA']=Join-Path $profile 'AppData\Local'
$environment['TEMP']=Join-Path $profile 'AppData\Local\Temp';$environment['TMP']=$environment['TEMP']
$environment['USERNAME']=$account.username.Split('\')[-1];$environment['USERDOMAIN']=$env:COMPUTERNAME
$environment['PYTHONUTF8']='1';$environment['PYTHONIOENCODING']='utf-8'
$environment['PYTHONPATH']=$null;$environment['PYTHONHOME']=$null
$environment['PATH']=@((Split-Path $baseConfig.tool_python),(Join-Path (Split-Path $baseConfig.tool_python) 'Scripts'),(Split-Path $baseConfig.codex_executable),(Split-Path $baseConfig.doc_converter),'C:\Windows\System32','C:\Windows','C:\Windows\System32\WindowsPowerShell\v1.0') -join ';'
$control=[IO.File]::Open((Join-Path $data '.launch-control.lock'),'OpenOrCreate','ReadWrite','None')
try{
 if($Mode -eq 'Serve'){
  if(Get-NetTCPConnection -LocalPort 8788 -State Listen -ErrorAction SilentlyContinue){throw 'Port 8788 already in use; no process stopped'}
 }
 $verb=switch($Mode){'IdentityProbe'{'identity-probe'}'ProtocolProbe'{'protocol-probe'}'ProtocolProbeNoEnvironment'{'protocol-probe-no-environment'}'Doctor'{'doctor'}'Serve'{'serve'}}
 $operation=[Guid]::NewGuid().ToString()
 $launch=Start-Process -FilePath 'C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe' -ArgumentList @('-NoLogo','-NoProfile','-NonInteractive','-File',(Join-Path $root 'launch-packaged-user.ps1'),'-Mode',$verb,'-OperationId',$operation) -Credential $credential -LoadUserProfile -Environment $environment -WorkingDirectory $root -WindowStyle Hidden -RedirectStandardOutput (Join-Path $data 'package-activation.stdout.log') -RedirectStandardError (Join-Path $data 'package-activation.stderr.log') -PassThru
 if(-not$launch.WaitForExit(20000)){throw 'Official package activation timed out'}
 $deadline=[DateTime]::UtcNow.AddSeconds(150)
 do{
  $receipt=$null;$record=$null
  try{$record=Get-Content -LiteralPath (Join-Path $data ($verb+'.process.json')) -Raw -ErrorAction Stop|ConvertFrom-Json}catch{}
  try{$receipt=Get-Content -LiteralPath (Join-Path $data ($verb+'.exit.json')) -Raw -ErrorAction Stop|ConvertFrom-Json}catch{}
  if($Mode -eq 'Serve' -and $record.operation_id -eq $operation){$record|ConvertTo-Json -Compress;return}
  if($receipt.operation_id -eq $operation){break}
  if($launch.ExitCode -ne 0 -and $record.operation_id -ne $operation){
   Get-Content -LiteralPath (Join-Path $data 'package-activation.stderr.log') -Tail 15
   throw 'Official package activation failed'
  }
  Start-Sleep -Milliseconds 200
 }while([DateTime]::UtcNow -lt $deadline)
 if($receipt.operation_id -ne $operation){throw 'Waiting for matching fixed-probe receipt timed out; do not duplicate launch'}
 $out=Get-Content -LiteralPath (Join-Path $data ($verb+'.stdout.log')) -Raw
 try{$report=$out|ConvertFrom-Json}catch{$report=$null}
 if($receipt.exit_code -ne 0 -or -not$report.passed){
  Get-Content -LiteralPath (Join-Path $data ($verb+'.stderr.log')) -Tail 15
  throw ('Packaged trial check failed: '+$Mode)
 }
 $report|ConvertTo-Json -Depth 10
}finally{$control.Dispose()}
