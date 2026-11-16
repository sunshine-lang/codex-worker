param([Parameter(Mandatory=$true)][string]$PlanFile,[switch]$ValidateOnly,[string]$ValidationReport)
$ErrorActionPreference = 'Stop'
$ProductId = '5ea84759-74a0-438e-bcaf-cf19803d8053'
$plan = Get-Content -LiteralPath $PlanFile -Raw | ConvertFrom-Json
$root = [IO.Path]::GetFullPath([string]$plan.install_dir).TrimEnd('\')
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
function Assert-Plan {
  if (-not [Environment]::Is64BitOperatingSystem -or [Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) { throw 'WINDOWS_X64_REQUIRED' }
  if (-not [IO.Path]::IsPathRooted($root) -or $root.StartsWith('\\') -or $root -eq [IO.Path]::GetPathRoot($root) -or $root.Length -gt 120 -or $root.Contains('"')) { throw 'INVALID_INSTALL_DIRECTORY' }
  foreach ($reserved in @($env:WINDIR,[Environment]::GetFolderPath('ProgramFiles'),[Environment]::GetFolderPath('CommonApplicationData'),[Environment]::GetFolderPath('UserProfile'))) { if ($root -ieq $reserved.TrimEnd('\')) { throw 'RESERVED_INSTALL_DIRECTORY' } }
  if ([int]$plan.port -lt 1 -or [int]$plan.port -gt 65535) { throw 'INVALID_API_PORT' }
  $uri = $null
  if (-not [Uri]::TryCreate([string]$plan.model_base_url,[UriKind]::Absolute,[ref]$uri) -or $uri.Scheme -notin @('http','https') -or $uri.UserInfo -or $uri.Query -or $uri.Fragment) { throw 'INVALID_MODEL_API_URL' }
  if ([string]::IsNullOrWhiteSpace($plan.model_name) -or [string]$plan.model_name -match '[\r\n]') { throw 'INVALID_MODEL_NAME' }
  if ([string]$plan.model_api_key -match '[\r\n]') { throw 'INVALID_MODEL_KEY_FORMAT' }
  if ($plan.public_api_url) { $uri=$null; if (-not [Uri]::TryCreate([string]$plan.public_api_url,[UriKind]::Absolute,[ref]$uri) -or $uri.Scheme -notin @('http','https')) { throw 'INVALID_PUBLIC_API_URL' } }
  $parsedInstallationId=[Guid]::Empty; if(-not [Guid]::TryParse([string]$plan.installation_id,[ref]$parsedInstallationId)){throw 'INVALID_INSTALLATION_ID'}
  if ($plan.operator_sid -ne $identity.User.Value) { throw 'INSTALL_OPERATOR_MISMATCH' }
}
Assert-Plan
$existingTask = Get-ScheduledTask -TaskName 'CodexRemoteWorker-DesktopStart' -ErrorAction SilentlyContinue
if ($ValidateOnly) {
  $report = [ordered]@{valid=$true;install_dir=$root;port=[int]$plan.port;allow_remote=[bool]$plan.allow_remote;token_generation='after_full_preflight';existing_installation=($null -ne $existingTask);mutations_performed=$false}
  if ($ValidationReport) { $report | ConvertTo-Json -Compress | Set-Content -LiteralPath $ValidationReport -Encoding utf8 }
  $report | ConvertTo-Json -Compress
  exit 0
}
if ($existingTask) { throw 'EXISTING_INSTALLATION: uninstall or migrate the existing worker before installing another instance.' }
$data = Join-Path $root 'Data'
$private = Join-Path $data 'private'
$python = Join-Path $root '.runtime\python313\python.exe'
$powerShell = Join-Path $root '.runtime\powershell\pwsh.exe'
$codex = Join-Path $root 'tools\codex\bin\codex.exe'
$office = Join-Path $root 'tools\LibreOffice\program\soffice.exe'
$startScript = Join-Path $root '.runtime\start-worker.local.ps1'
$startTask = 'CodexRemoteWorker-DesktopStart'
$stopTask = 'CodexRemoteWorker-DesktopStop'
$repairTask = 'CodexRemoteWorker-RepairSandbox'
$installationId = [Guid]::Parse([string]$plan.installation_id).ToString('N')
$accountName = 'CodexW_' + $installationId.Substring(0,10)
$accountCreated = $false
$tasksCreated = $false
$createdTasks = [Collections.Generic.List[string]]::new()
$filtersCreated = $false
$registryWritten = $false
$workerRecord = $null
$stage = 'validate'
$adminSid = [Security.Principal.SecurityIdentifier]::new('S-1-5-32-544')
$systemSid = [Security.Principal.SecurityIdentifier]::new('S-1-5-18')
$usersSid = [Security.Principal.SecurityIdentifier]::new('S-1-5-32-545')
$operatorSid = [Security.Principal.SecurityIdentifier]::new($plan.operator_sid)
function Phase([string]$Name,[string]$Text) { $script:stage=$Name; Write-Output ('PROGRESS:'+$Text) }
function New-RandomText([int]$Count=48) { $bytes=New-Object byte[] $Count; $rng=[Security.Cryptography.RandomNumberGenerator]::Create(); try { $rng.GetBytes($bytes) } finally { $rng.Dispose() }; return [Convert]::ToBase64String($bytes).TrimEnd('=').Replace('+','-').Replace('/','_') }
function Protect-Directory([string]$Path,$ServiceSid,[bool]$ReadableProgram) {
  New-Item -ItemType Directory -Path $Path -Force | Out-Null
  $acl=[Security.AccessControl.DirectorySecurity]::new(); $acl.SetAccessRuleProtection($true,$false); $acl.SetOwner($adminSid)
  foreach($sid in @($adminSid,$systemSid)) { $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new($sid,'FullControl','ContainerInherit,ObjectInherit','None','Allow')) }
  if($ReadableProgram) { $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new($usersSid,'ReadAndExecute','ContainerInherit,ObjectInherit','None','Allow')) }
  if($ServiceSid) { $rights=if($ReadableProgram){'ReadAndExecute'}else{'FullControl'}; $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new($ServiceSid,$rights,'ContainerInherit,ObjectInherit','None','Allow')) }
  Set-Acl -LiteralPath $Path -AclObject $acl
}
function Save-Json([string]$Path,$Value) { $Value | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $Path -Encoding utf8 }
function Wait-Check($Record) {
  $process=Get-Process -Id $Record.ProcessId -ErrorAction SilentlyContinue
  if($process -and -not $process.WaitForExit(120000)) { Stop-Process -Id $process.Id -Force; throw 'PREFLIGHT_TIMEOUT' }
  $value=Get-Content -LiteralPath $Record.StandardOutput -Raw | ConvertFrom-Json
  if(-not $value.passed) { throw 'PREFLIGHT_FAILED' }
  return $value
}
try {
  foreach($file in @($python,$powerShell,$codex,$office,$startScript)) { if(-not(Test-Path -LiteralPath $file -PathType Leaf)){ throw 'PACKAGE_COMPONENT_MISSING' } }
  Phase 'runtime' '检查 Windows 运行组件'
  $redistributable=Join-Path (Split-Path $PlanFile -Parent) 'dependencies\vc_redist.x64.exe'
  if(Test-Path -LiteralPath $redistributable) {
    $signature=Get-AuthenticodeSignature -LiteralPath $redistributable
    if($signature.Status -ne 'Valid' -or $signature.SignerCertificate.Subject -notmatch 'Microsoft Corporation'){throw 'RUNTIME_SIGNATURE_INVALID'}
    $runtime=Start-Process -FilePath $redistributable -ArgumentList '/install /quiet /norestart' -WindowStyle Hidden -PassThru
    if(-not $runtime.WaitForExit(180000)){throw 'RUNTIME_INSTALL_TIMEOUT'}
    if($runtime.ExitCode -notin @(0,3010,1638)){throw 'RUNTIME_INSTALL_FAILED'}
  }
  Phase 'account' '配置专用运行账户和目录权限'
  if(Get-LocalUser -Name $accountName -ErrorAction SilentlyContinue){throw 'SERVICE_ACCOUNT_COLLISION'}
  $accountPassword='Aa1!'+(New-RandomText 36)
  $localUser=New-LocalUser -Name $accountName -Password (ConvertTo-SecureString $accountPassword -AsPlainText -Force) -PasswordNeverExpires -AccountNeverExpires -UserMayNotChangePassword -Description 'CodeX Worker dedicated runtime account'
  $accountCreated=$true
  $usersGroup=Get-LocalGroup -SID $usersSid
  if(-not(Get-LocalGroupMember -Group $usersGroup|Where-Object{$_.SID.Value -eq $localUser.SID.Value})){Add-LocalGroupMember -Group $usersGroup -Member $localUser}
  Protect-Directory $root $localUser.SID $true
  Protect-Directory $data $localUser.SID $false
  Protect-Directory $private $localUser.SID $false
  $localUser=Get-LocalUser -Name $accountName
  $bootstrapLogon=[pscredential]::new(($env:COMPUTERNAME+'\'+$accountName),(ConvertTo-SecureString $accountPassword -AsPlainText -Force))
  $profileLog=Join-Path $private 'profile-bootstrap.log'
  $profileProcess=Start-Process -FilePath (Join-Path $env:WINDIR 'System32\whoami.exe') -ArgumentList '/user' -Credential $bootstrapLogon -LoadUserProfile -WorkingDirectory (Join-Path $env:WINDIR 'System32') -WindowStyle Hidden -RedirectStandardOutput $profileLog -PassThru
  if(-not $profileProcess.WaitForExit(30000) -or $profileProcess.ExitCode -ne 0){throw 'SERVICE_PROFILE_LOGON_FAILED'}
  $profileProcess.Dispose()
  if((Get-Content -LiteralPath $profileLog -Raw) -notmatch [regex]::Escape($localUser.SID.Value)){throw 'SERVICE_PROFILE_IDENTITY_MISMATCH'}
  $profileEntry=Get-ItemProperty -LiteralPath ('HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\ProfileList\'+$localUser.SID.Value)
  $profile=[Environment]::ExpandEnvironmentVariables($profileEntry.ProfileImagePath)
  if(-not(Test-Path -LiteralPath $profile -PathType Container)){throw 'SERVICE_PROFILE_MISSING'}
  $codexHome=Join-Path $profile '.codex'
  Protect-Directory $codexHome $localUser.SID $false
  New-Item -ItemType Directory -Path (Join-Path $profile 'AppData\Local\Temp') -Force | Out-Null
  Save-Json (Join-Path $private 'service-account.local.json') @{username=($env:COMPUTERNAME+'\'+$accountName);password=$accountPassword}
  Phase 'configuration' '写入本机配置'
  $provider=@{model=[string]$plan.model_name;base_url=[string]$plan.model_base_url}
  $providerInput=Join-Path $private 'model-input.local.json'; Save-Json $providerInput $provider
  & $python (Join-Path $root 'installer\write_model_config.py') $providerInput (Join-Path $codexHome 'config.toml')
  if($LASTEXITCODE -ne 0){throw 'MODEL_CONFIGURATION_FAILED'}
  Remove-Item -LiteralPath $providerInput
  $configuration=[ordered]@{data_root=$data;codex_executable=$codex;codex_home=$codexHome;expected_codex_version='0.167.0-alpha.3.2';tool_python=$python;doc_converter=$office;tool_read_roots=@((Join-Path $root '.runtime\python313'),(Join-Path $root 'tools\codex\bin'),(Join-Path $root 'tools\LibreOffice\program'));windows_sandbox='elevated';bind_host=$(if($plan.allow_remote){'0.0.0.0'}else{'127.0.0.1'});port=[int]$plan.port;queue_capacity=10;min_free_bytes=536870912}
  Save-Json (Join-Path $root 'config.local.json') $configuration
  Save-Json (Join-Path $root 'desktop.settings.json') @{data_root=$data;port=[int]$plan.port;start_task=$startTask;stop_task=$stopTask;repair_task=$repairTask;public_api_url=[string]$plan.public_api_url;allow_remote=[bool]$plan.allow_remote}
  $bootstrapCredentials=[ordered]@{CODEX_WORKER_SERVICE_TOKEN=(New-RandomText)}
  if($plan.model_api_key){$bootstrapCredentials['CODEX_WORKER_PROVIDER_API_KEY']=[string]$plan.model_api_key}
  $bootstrapPath=Join-Path $private 'bootstrap-credentials.local.json'; Save-Json $bootstrapPath $bootstrapCredentials
  Phase 'sandbox' '初始化 Windows 沙箱'
  $filtersCreated=$true
  $null=& $startScript -Mode Initialize -CredentialsFile $bootstrapPath
  Phase 'preflight' '验证文件隔离、网络隔离和进程回收'
  $checkRecord=& $startScript -Mode Doctor -CredentialsFile $bootstrapPath | ConvertFrom-Json
  $preflight=Wait-Check $checkRecord
  Phase 'json-preflight' '验证 JSON 输入输出运行工具（不调用模型）'
  $jsonRecord=& $startScript -Mode VerifyJson -CredentialsFile $bootstrapPath | ConvertFrom-Json
  $jsonPreflight=Wait-Check $jsonRecord
  Phase 'token' '生成访问 Token'
  $serviceToken=New-RandomText
  $finalCredentials=[ordered]@{CODEX_WORKER_SERVICE_TOKEN=$serviceToken}
  if($plan.model_api_key){$finalCredentials['CODEX_WORKER_PROVIDER_API_KEY']=[string]$plan.model_api_key}
  Save-Json (Join-Path $private 'credentials.local.json') $finalCredentials
  $tokenPath=Join-Path $private 'service-token.txt'
  [IO.File]::WriteAllText($tokenPath,$serviceToken,[Text.UTF8Encoding]::new($false))
  foreach($directory in @($data,$private)) { $acl=Get-Acl -LiteralPath $directory; $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new($operatorSid,'Traverse','None','None','Allow')); Set-Acl -LiteralPath $directory -AclObject $acl }
  $tokenAcl=Get-Acl -LiteralPath $tokenPath; $tokenAcl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new($operatorSid,'Read','Allow')); Set-Acl -LiteralPath $tokenPath -AclObject $tokenAcl
  Remove-Item -LiteralPath $bootstrapPath
  Phase 'tasks' '创建桌面启动和停止任务'
  $principal=New-ScheduledTaskPrincipal -UserId $identity.Name -LogonType Interactive -RunLevel Highest
  $taskSettings=New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::FromMinutes(10)) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
  foreach($item in @(@($startTask,'desktop-ensure.ps1',''),@($stopTask,'desktop-stop.ps1',''),@($repairTask,'desktop-ensure.ps1',' -Repair'))) {
    $action=New-ScheduledTaskAction -Execute $powerShell -Argument ('-NoLogo -NoProfile -NonInteractive -WindowStyle Hidden -File "'+(Join-Path $root ('scripts\'+$item[1]))+'"'+$item[2]) -WorkingDirectory $root
    Register-ScheduledTask -TaskName $item[0] -Action $action -Principal $principal -Settings $taskSettings -Description 'CodeX Worker installed service controller' -Force | Out-Null
    $createdTasks.Add([string]$item[0])
    $tasksCreated=$true
  }
  $tasksCreated=$true
  $scheduler=New-Object -ComObject 'Schedule.Service'; $scheduler.Connect()
  foreach($name in @($startTask,$stopTask,$repairTask)){ $scheduler.GetFolder('\').GetTask($name).SetSecurityDescriptor(('D:P(A;;GA;;;SY)(A;;GA;;;BA)(A;;GRGX;;;'+$operatorSid.Value+')'),0) }
  if($plan.allow_remote){New-NetFirewallRule -Name ('CodexRemoteWorker-Api-'+$installationId) -DisplayName ('CodeX Worker TCP '+$plan.port) -Direction Inbound -Action Allow -Protocol TCP -LocalPort ([int]$plan.port) -Program (Join-Path $root 'bin\CodexRemoteWorker\CodexRemoteWorker.exe') -Profile Any | Out-Null}
  $registry='HKLM:\SOFTWARE\CodexRemoteWorker'; New-Item -Path $registry -Force | Out-Null
  New-ItemProperty -Path $registry -Name InstallDir -Value $root -PropertyType String -Force | Out-Null
  New-ItemProperty -Path $registry -Name InstallationId -Value $installationId -PropertyType String -Force | Out-Null
  $registryWritten = $true
  Save-Json (Join-Path $private 'installation.json') @{product_id=$ProductId;installation_id=$installationId;install_dir=$root;data_root=$data;service_account=$accountName;service_account_sid=$localUser.SID.Value;operator_sid=$operatorSid.Value;created_at=[DateTimeOffset]::UtcNow.ToString('o')}
  Phase 'start' '启动服务并确认 API 就绪'
  $workerRecord=& $startScript -Mode Serve -VerifiedStartup | ConvertFrom-Json
  $deadline=[DateTime]::UtcNow.AddSeconds(60)
  do { $client=[Net.Sockets.TcpClient]::new(); try { $connection=$client.ConnectAsync('127.0.0.1',[int]$plan.port); if($connection.Wait(1000) -and $client.Connected){break} } catch{} finally{$client.Dispose()}; Start-Sleep -Milliseconds 300 } while([DateTime]::UtcNow -lt $deadline)
  $request=[Net.HttpWebRequest]::Create('http://127.0.0.1:'+$plan.port+'/v1/ready'); $request.Proxy=$null; $request.Timeout=120000; $request.ReadWriteTimeout=120000; $request.Headers['Authorization']='Bearer '+$serviceToken
  $response=$request.GetResponse(); try{$reader=[IO.StreamReader]::new($response.GetResponseStream()); try{$ready=$reader.ReadToEnd()|ConvertFrom-Json}finally{$reader.Dispose()}}finally{$response.Dispose()}
  if (-not $ready.ready){throw 'SERVICE_NOT_READY'}
  if ($ready.service_version -ne '0.4.0' -or 'siemens-json/1' -notin $ready.input_contracts -or 'siemens-json/2' -notin $ready.input_contracts -or 'table-csv/1' -notin $ready.input_contracts) { throw 'PACKAGE_CONTRACT_MISMATCH' }
  $publicApi=if($plan.public_api_url){([string]$plan.public_api_url).TrimEnd('/')}else{'http://127.0.0.1:'+$plan.port}
  if (-not $plan.public_api_url -and $plan.allow_remote) {
    try {
      $metadataSession=Invoke-RestMethod -Method Put -Uri 'http://169.254.169.254/latest/api/token' -Headers @{'X-aws-ec2-metadata-token-ttl-seconds'='60'} -TimeoutSec 2 -NoProxy
      $metadataAddress=[string](Invoke-RestMethod -Uri 'http://169.254.169.254/latest/meta-data/public-ipv4' -Headers @{'X-aws-ec2-metadata-token'=$metadataSession} -TimeoutSec 2 -NoProxy)
      $parsedAddress=$null
      if([Net.IPAddress]::TryParse($metadataAddress,[ref]$parsedAddress)){$publicApi='http://'+$metadataAddress+':'+$plan.port}
    } catch {
      $local=[Net.Dns]::GetHostAddresses([Environment]::MachineName)|Where-Object{$_.AddressFamily -eq [Net.Sockets.AddressFamily]::InterNetwork -and -not [Net.IPAddress]::IsLoopback($_)}|Select-Object -First 1
      if($local){$publicApi='http://'+$local.ToString()+':'+$plan.port}
    }
  }
  [ordered]@{state='ready';message='服务已启动，API 已就绪。';public_api=$publicApi;process_id=$workerRecord.ProcessId;started_at=$workerRecord.StartedAt;updated_at=[DateTimeOffset]::UtcNow.ToString('o')} | ConvertTo-Json -Compress | Set-Content -LiteralPath (Join-Path $root 'desktop-status.json') -Encoding utf8
  $desktop=[Environment]::GetFolderPath('Desktop')
  $shell=New-Object -ComObject WScript.Shell
  $shortcut=$shell.CreateShortcut((Join-Path $desktop 'CodeX 服务启动器.lnk')); $shortcut.TargetPath=Join-Path $root 'bin\CodexWorkerLauncher.exe'; $shortcut.Arguments='--root "'+$root+'"'; $shortcut.WorkingDirectory=$root; $shortcut.IconLocation=$shortcut.TargetPath+',0'; $shortcut.Save()
  $uninstallKey='HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\CodeXRemoteWorker'
  New-Item -Path $uninstallKey -Force|Out-Null
  foreach($entry in @{DisplayName='CodeX 任务服务';DisplayVersion='0.4.0';Publisher='CodeX Worker';InstallLocation=$root;UninstallString=('"'+(Join-Path $root 'Uninstall.exe')+'" --root "'+$root+'"')}.GetEnumerator()){New-ItemProperty -Path $uninstallKey -Name $entry.Key -Value $entry.Value -PropertyType String -Force|Out-Null}
  Save-Json (Join-Path $private 'installation-result.json') @{success=$true;install_dir=$root;data_root=$data;port=[int]$plan.port;api=$publicApi;token_file=$tokenPath;model_key_configured=([bool]$plan.model_api_key);service_pid=$workerRecord.ProcessId;worker_version=$ready.service_version;input_contracts=@($ready.input_contracts);sandbox_checks=@($preflight.checks);json_checks=@($jsonPreflight.checks);completed_at=[DateTimeOffset]::UtcNow.ToString('o')}
  Write-Output 'PROGRESS:安装完成'
  exit 0
} catch {
  if($workerRecord){ $process=Get-Process -Id $workerRecord.ProcessId -ErrorAction SilentlyContinue; if($process -and $process.Path -eq $workerRecord.Executable -and $process.StartTime.ToUniversalTime().Ticks -eq ([DateTimeOffset]$workerRecord.StartedAt).UtcDateTime.Ticks){Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue} }
  if($tasksCreated){foreach($name in $createdTasks){Unregister-ScheduledTask -TaskName $name -Confirm:$false -ErrorAction SilentlyContinue}}
  if($filtersCreated){try{$null=&$python (Join-Path $root '.runtime\wfp-network-lockdown.py') --remove}catch{}}
  Remove-NetFirewallRule -Name ('CodexRemoteWorker-Api-'+$installationId) -ErrorAction SilentlyContinue
  if($registryWritten){$key='HKLM:\SOFTWARE\CodexRemoteWorker';if((Get-ItemProperty -Path $key -ErrorAction SilentlyContinue).InstallationId -eq $installationId){Remove-Item -LiteralPath $key -Recurse -Force -ErrorAction SilentlyContinue}}
  if($accountCreated){Remove-LocalUser -Name $accountName -ErrorAction SilentlyContinue}
  if(Test-Path -LiteralPath $data){('{0} phase={1} error={2}'-f[DateTimeOffset]::UtcNow.ToString('o'),$stage,$_.Exception.GetType().Name)|Add-Content -LiteralPath (Join-Path $data 'install.log') -Encoding utf8}
  Write-Output ('INSTALL_ERROR:'+ $stage)
  exit 1
}









