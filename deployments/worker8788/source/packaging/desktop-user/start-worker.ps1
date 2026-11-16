param([ValidateSet('Serve','Doctor','Probe','VerifyJson','ProfileCheck')][string]$Mode='Serve',[string]$CredentialsFile,[switch]$VerifiedStartup)
$ErrorActionPreference='Stop'
$root=Split-Path $PSScriptRoot -Parent
$configPath=Join-Path $root 'config.local.json'
$config=Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json
$identity=[Security.Principal.WindowsIdentity]::GetCurrent()
$principal=[Security.Principal.WindowsPrincipal]::new($identity)
if($principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)){throw 'USER_CONTEXT_REQUIRED: 请不要以管理员身份运行；需要复用桌面端同一用户的普通登录会话。'}
if($identity.User.Value -ne (Get-Content -LiteralPath (Join-Path $root 'desktop.settings.json') -Raw|ConvertFrom-Json).desktop_user_sid){throw 'DESKTOP_USER_MISMATCH: 请使用已登录 Codex 的同一 Windows 用户。'}
if(-not $CredentialsFile){$CredentialsFile=Join-Path $config.data_root 'private\credentials.local.json'}
$worker=Join-Path $root 'bin\CodexRemoteWorker\CodexRemoteWorker.exe'
$environment=@{}
foreach($item in Get-ChildItem Env:){if($item.Name -like 'CODEX_*'){$environment[$item.Name]=$null}}
foreach($name in @('OPENAI_API_KEY','OPENROUTER_API_KEY','OPENAI_BASE_URL')){$environment[$name]=$null}
$environment['CODEX_HOME']=$config.codex_home
$environment['PYTHONUTF8']='1';$environment['PYTHONIOENCODING']='utf-8'
$environment['PATH']=@((Split-Path $config.tool_python),(Split-Path $config.codex_executable),(Split-Path $config.doc_converter),'C:\Windows\System32','C:\Windows','C:\Windows\System32\WindowsPowerShell\v1.0') -join ';'
function Quote([string]$Value){if($Value -match '["\r\n]'){throw 'INVALID_ARGUMENT'};return '"'+$Value.TrimEnd('\')+'"'}
function Launch([string]$Executable,[string[]]$Arguments,[string]$Name){
 $prefix=Join-Path $config.data_root ('worker.local.'+$Name)
 $line=($Arguments|ForEach-Object{Quote $_}) -join ' '
 $process=Start-Process -FilePath $Executable -ArgumentList $line -Environment $environment -WorkingDirectory $root -WindowStyle Hidden -RedirectStandardOutput ($prefix+'.stdout.log') -RedirectStandardError ($prefix+'.stderr.log') -PassThru
 $record=[pscustomobject]@{ProcessId=$process.Id;StartedAt=$process.StartTime.ToUniversalTime().ToString('o');Executable=$Executable;Config=$configPath;StandardOutput=($prefix+'.stdout.log');StandardError=($prefix+'.stderr.log')}
 $record|ConvertTo-Json -Compress|Set-Content -LiteralPath ($prefix+'.process.json') -Encoding utf8
 return $record
}
function Wait-Result($Record){
 $process=Get-Process -Id $Record.ProcessId -ErrorAction SilentlyContinue
 if($process -and -not $process.WaitForExit(120000)){
  if($process.Path -eq $Record.Executable -and $process.StartTime.ToUniversalTime().Ticks -eq ([DateTimeOffset]$Record.StartedAt).UtcDateTime.Ticks){Stop-Process -Id $process.Id -Force}
  throw 'CHECK_TIMEOUT'
 }
 $value=Get-Content -LiteralPath $Record.StandardOutput -Raw|ConvertFrom-Json
 if(-not $value.passed -and -not $value.ready){throw ('CHECK_FAILED: '+$value.code+' '+$value.message)}
 return $value
}
if($Mode -eq 'ProfileCheck'){
 Launch $config.tool_python @('-X','utf8','-m','codex_remote_worker.subscription','--config',$configPath,'--credentials-file',$CredentialsFile) 'profile-check'|ConvertTo-Json -Compress
 return
}
if($Mode -eq 'VerifyJson'){
 Launch $config.tool_python @('-X','utf8',(Join-Path $root 'installer\verify-json-runtime.py'),'--config',$configPath,'--credentials-file',$CredentialsFile) 'json-probe'|ConvertTo-Json -Compress
 return
}
if($Mode -in @('Doctor','Probe')){
 $arguments=@($Mode.ToLowerInvariant(),'--config',$configPath,'--credentials-file',$CredentialsFile)
 if($Mode -eq 'Doctor'){$arguments+='--verify-sandbox'}
 Launch $worker $arguments $Mode.ToLowerInvariant()|ConvertTo-Json -Compress
 return
}
$recordPath=Join-Path $config.data_root 'worker.local.process.json'
if(Test-Path -LiteralPath $recordPath){
 $record=Get-Content -LiteralPath $recordPath -Raw|ConvertFrom-Json
 $process=Get-Process -Id $record.ProcessId -ErrorAction SilentlyContinue
 if($process -and $process.Path -eq $worker -and $process.Path -eq $record.Executable -and $process.StartTime.ToUniversalTime().Ticks -eq ([DateTimeOffset]$record.StartedAt).UtcDateTime.Ticks){$record|ConvertTo-Json -Compress;return}
}
if([Net.NetworkInformation.IPGlobalProperties]::GetIPGlobalProperties().GetActiveTcpListeners().Port -contains [int]$config.port){throw 'API_PORT_IN_USE'}
$profileRecord=Launch $config.tool_python @('-X','utf8','-m','codex_remote_worker.subscription','--config',$configPath,'--credentials-file',$CredentialsFile) 'profile-check'
$null=Wait-Result $profileRecord
$arguments=@('doctor','--config',$configPath,'--credentials-file',$CredentialsFile)
if(-not $VerifiedStartup){$arguments+='--verify-sandbox'}
$null=Wait-Result (Launch $worker $arguments 'doctor')
$record=Launch $worker @('serve','--config',$configPath,'--credentials-file',$CredentialsFile) 'serve'
$record|ConvertTo-Json -Compress|Set-Content -LiteralPath $recordPath -Encoding utf8
$record|ConvertTo-Json -Compress
