$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path $PSScriptRoot -Parent
$candidate = 'C:\CodexRemoteWorker\candidate-runtime-tools'
$config = Get-Content -LiteralPath 'C:\CodexRemoteWorker\config.local.json' -Raw | ConvertFrom-Json
$account = Get-Content -LiteralPath (Join-Path $config.data_root 'private\service-account.local.json') -Raw | ConvertFrom-Json
$credential = [pscredential]::new($account.username,(ConvertTo-SecureString $account.password -AsPlainText -Force))
New-Item -ItemType Directory -Path (Join-Path $candidate 'src\codex_remote_worker'),(Join-Path $candidate 'scripts') -Force | Out-Null
foreach($file in Get-ChildItem -LiteralPath (Join-Path $projectRoot 'src\codex_remote_worker') -File -Filter '*.py') { Copy-Item -LiteralPath $file.FullName -Destination (Join-Path $candidate 'src\codex_remote_worker') -Force }
Copy-Item -LiteralPath (Join-Path $projectRoot 'scripts\validate-runtime-tools.local.py') -Destination (Join-Path $candidate 'scripts\validate-runtime-tools.local.py') -Force
$environment=@{}
foreach($item in Get-ChildItem Env:) { if ($item.Name -like 'CODEX_*') { $environment[$item.Name]=$null } }
$profilePath=Split-Path $config.codex_home -Parent
$environment['CODEX_HOME']=$config.codex_home
$environment['USERPROFILE']=$profilePath
$environment['APPDATA']=Join-Path $profilePath 'AppData\Roaming'
$environment['LOCALAPPDATA']=Join-Path $profilePath 'AppData\Local'
$environment['TEMP']=Join-Path $profilePath 'AppData\Local\Temp'
$environment['TMP']=$environment['TEMP']
$environment['PYTHONUTF8']='1';$environment['PYTHONIOENCODING']='utf-8'
$environment['PATH']=@((Split-Path $config.tool_python),(Split-Path $config.codex_executable),(Split-Path $config.doc_converter),'C:\Windows\System32','C:\Windows','C:\Windows\System32\WindowsPowerShell\v1.0') -join ';'
$stdout=Join-Path $config.data_root 'runtime-validation.stdout.log'
$stderr=Join-Path $config.data_root 'runtime-validation.stderr.log'
$arguments=@('-X','utf8',(Join-Path $candidate 'scripts\validate-runtime-tools.local.py'),'--source-root',$candidate)
$process=Start-Process -FilePath $config.tool_python -ArgumentList $arguments -Credential $credential -LoadUserProfile -Environment $environment -WorkingDirectory $candidate -WindowStyle Hidden -RedirectStandardOutput $stdout -RedirectStandardError $stderr -PassThru
[pscustomobject]@{ProcessId=$process.Id;StartedAt=$process.StartTime.ToUniversalTime().ToString('o');Stdout=$stdout;Stderr=$stderr}|ConvertTo-Json -Compress
