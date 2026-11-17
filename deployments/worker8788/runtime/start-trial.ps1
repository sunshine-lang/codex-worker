param([ValidateSet('ProtocolProbe','Doctor','Serve')][string]$Mode='Serve')
$ErrorActionPreference='Stop'
$root='C:\CodexRemoteWorker-AppServer-8788';$data='C:\CodexWorkerData-AppServer-8788'
$base='C:\CodexRemoteWorker';$configPath=Join-Path $root 'config.trial.json'
$baseConfig=Get-Content -LiteralPath (Join-Path $base 'config.local.json') -Raw|ConvertFrom-Json
$account=Get-Content -LiteralPath (Join-Path $baseConfig.data_root 'private\service-account.local.json') -Raw|ConvertFrom-Json
$credential=[pscredential]::new($account.username,(ConvertTo-SecureString $account.password -AsPlainText -Force))
$environment=@{}
foreach($item in Get-ChildItem Env:){if($item.Name -like 'CODEX_*'){$environment[$item.Name]=$null}}
$profile=Split-Path $baseConfig.codex_home -Parent
$environment['CODEX_WINDOWS_REGISTERED_CORE']='1'
$environment['CODEX_WINDOWS_SANDBOX_PACKAGE_FAMILY']='OpenAI.Codex_2p2nqsd0c76g0'
$environment['CODEX_HOME']=$baseConfig.codex_home;$environment['USERPROFILE']=$profile
$environment['APPDATA']=Join-Path $profile 'AppData\Roaming';$environment['LOCALAPPDATA']=Join-Path $profile 'AppData\Local'
$environment['TEMP']=Join-Path $profile 'AppData\Local\Temp';$environment['TMP']=$environment['TEMP']
$environment['USERNAME']=$account.username.Split('\')[-1];$environment['USERDOMAIN']=$env:COMPUTERNAME
$environment['PYTHONUTF8']='1';$environment['PYTHONIOENCODING']='utf-8'
$environment['PYTHONPATH']=$null;$environment['PYTHONHOME']=$null
$environment['PATH']=@((Split-Path $baseConfig.tool_python),(Join-Path (Split-Path $baseConfig.tool_python) 'Scripts'),(Split-Path $baseConfig.codex_executable),(Split-Path $baseConfig.doc_converter),'C:\Windows\System32','C:\Windows','C:\Windows\System32\WindowsPowerShell\v1.0') -join ';'
$control=[IO.File]::Open((Join-Path $data '.launch-control.lock'),'OpenOrCreate','ReadWrite','None')
try{
 if($Mode -eq 'Serve'){
  $listener=Get-NetTCPConnection -LocalPort 8788 -State Listen -ErrorAction SilentlyContinue
  if($listener){throw 'Port 8788 is already in use; no process was stopped.'}
 }
$verb=switch($Mode){'ProtocolProbe'{'protocol-probe'}'Doctor'{'doctor'}'Serve'{'serve'}}
$stdout=Join-Path $data ($verb+'.stdout.log');$stderr=Join-Path $data ($verb+'.stderr.log')
$arguments=@('-X','utf8',(Join-Path $root 'worker-app-server.py'),$verb,'--config',$configPath)
$p=Start-Process -FilePath $baseConfig.tool_python -ArgumentList $arguments -Credential $credential -LoadUserProfile -Environment $environment -WorkingDirectory $root -WindowStyle Hidden -RedirectStandardOutput $stdout -RedirectStandardError $stderr -PassThru
$record=[ordered]@{ProcessId=$p.Id;StartedAt=$p.StartTime.ToUniversalTime().ToString('o');Executable=$baseConfig.tool_python;Entrypoint=(Join-Path $root 'worker-app-server.py');Mode=$Mode;StandardOutput=$stdout;StandardError=$stderr}
$record|ConvertTo-Json|Set-Content -LiteralPath (Join-Path $data ($verb+'.process.json')) -Encoding utf8
if($Mode -ne 'Serve'){
 if(-not$p.WaitForExit(150000)){
  # Identity is the exact process object launched above, not an enumerated account process.
  Stop-Process -Id $p.Id -Force
  throw 'Fixed trial verification timed out'
 }
 $text=Get-Content -LiteralPath $stdout -Raw
 try{$report=$text|ConvertFrom-Json}catch{$report=$null}
 if(-not$report.passed){
  if(Test-Path -LiteralPath $stderr){Get-Content -LiteralPath $stderr -Tail 20}
  throw ('Trial verification did not pass: '+$Mode)
 }
 $report|ConvertTo-Json -Depth 8
}else{$record|ConvertTo-Json -Compress}
}finally{$control.Dispose()}
