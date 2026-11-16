param([Parameter(Mandatory=$true)][string]$PlanFile,[switch]$ValidateOnly,[string]$ValidationReport)
$ErrorActionPreference='Stop'
$plan=Get-Content -LiteralPath $PlanFile -Raw|ConvertFrom-Json
$root=[IO.Path]::GetFullPath([string]$plan.install_dir).TrimEnd('\')
$identity=[Security.Principal.WindowsIdentity]::GetCurrent()
$principal=[Security.Principal.WindowsPrincipal]::new($identity)
if($principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)){throw 'USER_CONTEXT_REQUIRED: 请从正常桌面会话启动安装器，不要使用管理员运行。'}
if($identity.User.Value -ne $plan.operator_sid){throw 'DESKTOP_USER_MISMATCH'}
if(-not [Environment]::Is64BitOperatingSystem){throw 'WINDOWS_X64_REQUIRED'}
if(-not [IO.Path]::IsPathRooted($root) -or $root.StartsWith('\\') -or $root -eq [IO.Path]::GetPathRoot($root) -or $root.Contains('"')){throw 'INVALID_INSTALL_DIRECTORY'}
if([int]$plan.port -lt 1 -or [int]$plan.port -gt 65535){throw 'INVALID_PORT'}
$codexHome=[IO.Path]::GetFullPath([string]$plan.codex_home)
if(-not(Test-Path -LiteralPath $codexHome -PathType Container)){throw 'CODEX_HOME_MISSING: 请先在当前 Windows 用户下打开 Codex 桌面端并登录。'}
if($plan.public_api_url){$uri=$null;if(-not[Uri]::TryCreate([string]$plan.public_api_url,[UriKind]::Absolute,[ref]$uri) -or $uri.Scheme -notin @('http','https') -or $uri.UserInfo -or $uri.Query -or $uri.Fragment){throw 'INVALID_PUBLIC_API_URL'}}
if($ValidateOnly){
 $report=@{valid=$true;mutations_performed=$false;authentication='existing_desktop_chatgpt';creates_windows_account=$false;creates_admin_tasks=$false;changes_firewall=$false;writes_codex_config=$false;bind_host='127.0.0.1'}
 if($ValidationReport){$report|ConvertTo-Json|Set-Content -LiteralPath $ValidationReport -Encoding utf8};$report|ConvertTo-Json -Compress;exit 0
}
$data=Join-Path $root 'Data';$private=Join-Path $data 'private'
$worker=$null;$stage='validate'
function Save([string]$Path,$Value){$Value|ConvertTo-Json -Depth 8|Set-Content -LiteralPath $Path -Encoding utf8}
function Phase([string]$Name,[string]$Text){$script:stage=$Name;Write-Output ('PROGRESS:'+$Text)}
function Protect([string]$Path){
 New-Item -ItemType Directory -Path $Path -Force|Out-Null
 $acl=[Security.AccessControl.DirectorySecurity]::new();$acl.SetAccessRuleProtection($true,$false);$acl.SetOwner($identity.User)
 foreach($sid in @($identity.User,[Security.Principal.SecurityIdentifier]::new('S-1-5-18'),[Security.Principal.SecurityIdentifier]::new('S-1-5-32-544'))){$acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new($sid,'FullControl','ContainerInherit,ObjectInherit','None','Allow'))}
 Set-Acl -LiteralPath $Path -AclObject $acl
}
function Wait-Check($Record){
 $p=Get-Process -Id $Record.ProcessId -ErrorAction SilentlyContinue
 if($p -and -not $p.WaitForExit(120000)){if($p.Path -eq $Record.Executable -and $p.StartTime.ToUniversalTime().Ticks -eq ([DateTimeOffset]$Record.StartedAt).UtcDateTime.Ticks){Stop-Process -Id $p.Id -Force};throw 'PREFLIGHT_TIMEOUT'}
 $v=Get-Content -LiteralPath $Record.StandardOutput -Raw|ConvertFrom-Json
 if(-not $v.passed){throw ('PREFLIGHT_FAILED: '+$v.code+' '+$v.message)}
 return $v
}
try{
 Phase 'configuration' '创建本用户 Worker 配置，不改动桌面端 Codex 配置'
 Protect $data;Protect $private
 $python=Join-Path $root '.runtime\python313\python.exe';$codex=Join-Path $root 'tools\codex\bin\codex.exe';$office=Join-Path $root 'tools\LibreOffice\program\soffice.exe'
 $profile=[Environment]::GetFolderPath('UserProfile')
 $protected=@($profile)
 foreach($name in @('Desktop','MyDocuments','MyPictures','MyMusic','MyVideos')){$path=[Environment]::GetFolderPath($name);if($path -and $protected -notcontains $path){$protected+=$path}}
 $configuration=[ordered]@{data_root=$data;codex_executable=$codex;codex_home=$codexHome;expected_codex_version='0.167.0-alpha.3.2';tool_python=$python;doc_converter=$office;tool_read_roots=@((Join-Path $root '.runtime\python313'),(Join-Path $root 'tools\codex\bin'),(Join-Path $root 'tools\LibreOffice\program'));authentication_mode='chatgpt';protected_read_roots=$protected;windows_sandbox='mxc';bind_host='127.0.0.1';port=[int]$plan.port;queue_capacity=10;min_free_bytes=536870912}
 # The SID is launcher metadata, not a core Settings field.
 Save (Join-Path $root 'config.local.json') $configuration
 Save (Join-Path $root 'desktop.settings.json') @{data_root=$data;port=[int]$plan.port;desktop_user_sid=$identity.User.Value;public_api_url=[string]$plan.public_api_url;allow_remote=$false;authentication='existing_desktop_chatgpt';controller='current_user'}
 $bytes=New-Object byte[] 48;$rng=[Security.Cryptography.RandomNumberGenerator]::Create();try{$rng.GetBytes($bytes)}finally{$rng.Dispose()}
 $token=[Convert]::ToBase64String($bytes).TrimEnd('=').Replace('+','-').Replace('/','_')
 Save (Join-Path $private 'credentials.local.json') @{CODEX_WORKER_SERVICE_TOKEN=$token}
 $start=Join-Path $root '.runtime\start-worker.local.ps1'
 Phase 'authentication' '只读验证当前桌面用户的官方订阅登录，不复制凭据'
 $profileRecord=& $start -Mode ProfileCheck|ConvertFrom-Json;$auth=Wait-Check $profileRecord
 $configuration.windows_sandbox=$auth.windows_sandbox;Save (Join-Path $root 'config.local.json') $configuration
 Phase 'preflight' '复用现有桌面沙箱并验证隔离，不重置共享账户'
 $doctor=& $start -Mode Doctor|ConvertFrom-Json;$preflight=Wait-Check $doctor
 Phase 'json' '验证 JSON 工具和中文文档处理'
 $json=& $start -Mode VerifyJson|ConvertFrom-Json;$jsonCheck=Wait-Check $json
 [IO.File]::WriteAllText((Join-Path $private 'service-token.txt'),$token,[Text.UTF8Encoding]::new($false))
 Save (Join-Path $private 'installation.json') @{product_id='5ea84759-74a0-438e-bcaf-cf19803d8053';variant='desktop-subscription';installation_id=[string]$plan.installation_id;install_dir=$root;data_root=$data;desktop_user_sid=$identity.User.Value;codex_home=$codexHome;created_at=[DateTimeOffset]::UtcNow.ToString('o')}
 Phase 'start' '在当前用户的非提权会话启动 Worker'
 $worker=& $start -Mode Serve -VerifiedStartup|ConvertFrom-Json
 $deadline=[DateTime]::UtcNow.AddSeconds(60);$ready=$null
 do{
  try{$request=[Net.HttpWebRequest]::Create('http://127.0.0.1:'+$plan.port+'/v1/ready');$request.Proxy=$null;$request.Timeout=15000;$request.Headers['Authorization']='Bearer '+$token;$response=$request.GetResponse();try{$reader=[IO.StreamReader]::new($response.GetResponseStream());try{$ready=$reader.ReadToEnd()|ConvertFrom-Json}finally{$reader.Dispose()}}finally{$response.Dispose()}}catch{$ready=$null}
  if($ready.ready){break};Start-Sleep -Milliseconds 500
 }while([DateTime]::UtcNow -lt $deadline)
 if(-not$ready.ready -or $ready.service_version -ne '0.4.0' -or 'siemens-json/1' -notin $ready.input_contracts -or 'siemens-json/2' -notin $ready.input_contracts){throw 'SERVICE_NOT_READY'}
 $api=if($plan.public_api_url){([string]$plan.public_api_url).TrimEnd('/')}else{'http://127.0.0.1:'+$plan.port}
 Save (Join-Path $root 'desktop-status.json') @{state='ready';code='READY';message='服务已启动，复用当前桌面官方登录。';public_api=$api;process_id=$worker.ProcessId;updated_at=[DateTimeOffset]::UtcNow.ToString('o')}
 $registry='HKCU:\SOFTWARE\CodexRemoteWorker\Desktop';New-Item -Path $registry -Force|Out-Null;New-ItemProperty -Path $registry -Name InstallDir -Value $root -PropertyType String -Force|Out-Null
 $shell=New-Object -ComObject WScript.Shell;$shortcut=$shell.CreateShortcut((Join-Path ([Environment]::GetFolderPath('Desktop')) 'Codex Worker 官方订阅版.lnk'));$shortcut.TargetPath=Join-Path $root 'bin\CodexWorkerLauncher.exe';$shortcut.Arguments='--root "'+$root+'"';$shortcut.WorkingDirectory=$root;$shortcut.Save()
 $uninstall='HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\CodexWorkerDesktop';New-Item -Path $uninstall -Force|Out-Null
 foreach($entry in @{DisplayName='Codex Worker 官方订阅版';DisplayVersion='0.4.0';Publisher='Codex Worker';InstallLocation=$root;UninstallString=('"'+(Join-Path $root 'Uninstall.exe')+'" --root "'+$root+'"')}.GetEnumerator()){New-ItemProperty -Path $uninstall -Name $entry.Key -Value $entry.Value -PropertyType String -Force|Out-Null}
 Save (Join-Path $private 'installation-result.json') @{success=$true;install_dir=$root;data_root=$data;port=[int]$plan.port;api=$api;token_file=(Join-Path $private 'service-token.txt');worker_version=$ready.service_version;authentication='existing_desktop_chatgpt';sandbox_checks=@($preflight.checks);json_checks=@($jsonCheck.checks);completed_at=[DateTimeOffset]::UtcNow.ToString('o')}
 Write-Output 'PROGRESS:安装完成，使用当前用户已有 Codex 官方登录'
 exit 0
}catch{
 if($worker){$process=Get-Process -Id $worker.ProcessId -ErrorAction SilentlyContinue;if($process -and $process.Path -eq $worker.Executable -and $process.StartTime.ToUniversalTime().Ticks -eq ([DateTimeOffset]$worker.StartedAt).UtcDateTime.Ticks){Stop-Process -Id $process.Id -Force}}
 if(Test-Path -LiteralPath $data){('{0} phase={1} type={2} code={3}'-f [DateTimeOffset]::UtcNow.ToString('o'),$stage,$_.Exception.GetType().Name,$(if($_.Exception.Message -match '^([A-Z_]+)'){$Matches[1]}else{'CHECK_FAILED'}))|Add-Content -LiteralPath (Join-Path $data 'install.log') -Encoding utf8}
 Write-Output ('INSTALL_ERROR:'+$stage);exit 1
}
