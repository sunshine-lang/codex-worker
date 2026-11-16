param([ValidateSet('Validate','JsonOnly','Serve')][string]$Mode='Validate')
$ErrorActionPreference='Stop'
$root='C:\CodexRemoteWorker'
$config=Get-Content -LiteralPath (Join-Path $root 'config.local.json') -Raw|ConvertFrom-Json
$data=[string]$config.data_root
$account=Get-Content -LiteralPath (Join-Path $data 'private\service-account.local.json') -Raw|ConvertFrom-Json
$credential=[pscredential]::new($account.username,(ConvertTo-SecureString $account.password -AsPlainText -Force))
function Wait-Check($record){
 $process=Get-Process -Id $record.ProcessId -ErrorAction SilentlyContinue
 if($process -and -not $process.WaitForExit(180000)){if($process.Path -eq $record.Executable -and $process.StartTime.ToUniversalTime().Ticks -eq ([DateTimeOffset]$record.StartedAt).UtcDateTime.Ticks){Stop-Process -Id $process.Id -Force};throw 'Native check timed out'}
 $value=Get-Content -LiteralPath $record.StandardOutput -Raw|ConvertFrom-Json
 if(-not$value.passed){throw ('Native check failed; inspect '+$record.StandardOutput)}
 return $value
}
$environment=@{}
foreach($item in Get-ChildItem Env:){if($item.Name -like 'CODEX_*'){$environment[$item.Name]=$null}}
$profile=Split-Path $config.codex_home -Parent
$environment['CODEX_HOME']=$config.codex_home
$environment['USERPROFILE']=$profile
$environment['APPDATA']=Join-Path $profile 'AppData\Roaming'
$environment['LOCALAPPDATA']=Join-Path $profile 'AppData\Local'
$environment['TEMP']=Join-Path $profile 'AppData\Local\Temp'
$environment['TMP']=$environment['TEMP']
$environment['USERNAME']=$account.username.Split('\')[-1]
$environment['USERDOMAIN']=$env:COMPUTERNAME
$environment['PYTHONUTF8']='1';$environment['PYTHONIOENCODING']='utf-8'
$environment['PATH']=@((Split-Path $config.tool_python),(Split-Path $config.codex_executable),(Split-Path $config.doc_converter),'C:\Windows\System32','C:\Windows','C:\Windows\System32\WindowsPowerShell\v1.0') -join ';'
if($Mode -in @('Validate','JsonOnly')){
 # Doctor mode does not run the old Serve bootstrap's sandbox setup/reset commands.
 if($Mode -eq 'Validate'){
  $doctor=& (Join-Path $root '.runtime\start-worker.local.ps1') -Mode Doctor|ConvertFrom-Json
  $result=Wait-Check $doctor
 }else{
  $result=Get-Content -LiteralPath (Join-Path $data 'preflight.json') -Raw|ConvertFrom-Json
  if(-not$result.passed){throw 'Bootstrap preflight did not pass'}
 }
 $stdout=Join-Path $data 'tm04-json-probe.stdout.log';$stderr=Join-Path $data 'tm04-json-probe.stderr.log'
 $args=@('-X','utf8',(Join-Path $root '.runtime\verify-json-runtime.py'),'--config',(Join-Path $root 'config.local.json'),'--credentials-file',(Join-Path $data 'private\credentials.local.json'))
 $process=Start-Process -FilePath $config.tool_python -ArgumentList $args -Credential $credential -LoadUserProfile -Environment $environment -WorkingDirectory $root -WindowStyle Hidden -RedirectStandardOutput $stdout -RedirectStandardError $stderr -PassThru
 $record=[pscustomobject]@{ProcessId=$process.Id;Executable=$config.tool_python;StartedAt=$process.StartTime.ToUniversalTime().ToString('o');StandardOutput=$stdout}
 $json=Wait-Check $record
 @{passed=$true;model_called=$false;sandbox=$result;json=$json}|ConvertTo-Json -Depth 7|Set-Content -LiteralPath (Join-Path $data 'tm04-native-verification.json') -Encoding utf8
 Write-Output 'TM04_NATIVE_CHECKS_PASSED'
 exit 0
}
$native=Get-Content -LiteralPath (Join-Path $data 'tm04-native-verification.json') -Raw|ConvertFrom-Json
if(-not$native.passed){throw 'Native checks have not passed'}
if([Net.NetworkInformation.IPGlobalProperties]::GetIPGlobalProperties().GetActiveTcpListeners().Port -contains [int]$config.port){throw 'Port is occupied; no other service will be stopped'}
$worker=Join-Path $root 'bin\CodexRemoteWorker\CodexRemoteWorker.exe'
$stdout=Join-Path $data 'worker.local.serve.stdout.log';$stderr=Join-Path $data 'worker.local.serve.stderr.log'
$args=@('serve','--config',(Join-Path $root 'config.local.json'),'--credentials-file',(Join-Path $data 'private\credentials.local.json'))
$process=Start-Process -FilePath $worker -ArgumentList $args -Credential $credential -LoadUserProfile -Environment $environment -WorkingDirectory $root -WindowStyle Hidden -RedirectStandardOutput $stdout -RedirectStandardError $stderr -PassThru
$record=[pscustomobject]@{ProcessId=$process.Id;Mode='Serve';StartedAt=$process.StartTime.ToUniversalTime().ToString('o');Executable=$worker;Config=(Join-Path $root 'config.local.json');StandardOutput=$stdout;StandardError=$stderr}
$record|ConvertTo-Json -Compress|Set-Content -LiteralPath (Join-Path $data 'worker.local.process.json') -Encoding utf8
$record|ConvertTo-Json -Compress
