$ErrorActionPreference='Stop'
$workspace=Split-Path $PSScriptRoot -Parent
$root='C:\CodexRemoteWorker';$data='C:\CodexWorkerData'
$pwsh=Join-Path $root '.runtime\powershell\pwsh.exe'
$python=Join-Path $root '.runtime\python313\python.exe'
$statePath=Join-Path $workspace 'build\tm-0.4.0\upgrade-state.json'
$state=Get-Content -LiteralPath $statePath -Raw|ConvertFrom-Json
$marker=Join-Path $data '.maintenance.json'
$lock=[IO.File]::Open((Join-Path $data '.desktop-control.lock'),'OpenOrCreate','ReadWrite','None')
function Check-Exit([string]$message){if($LASTEXITCODE -ne 0){throw $message}}
function Wait-Doctor($record){
 $p=Get-Process -Id $record.ProcessId -ErrorAction SilentlyContinue
 if($p -and -not$p.WaitForExit(180000)){throw 'Final native doctor timed out'}
 $v=Get-Content -LiteralPath $record.StandardOutput -Raw|ConvertFrom-Json
 if(-not$v.passed){throw 'Final native doctor failed; maintenance gate retained'}
 return $v
}
try{
 if(Test-Path -LiteralPath $marker){$previous=Get-Content -LiteralPath $marker -Raw|ConvertFrom-Json;if($previous.operation_id -ne $state.operation_id){throw 'Another maintenance owner'}}
 @{operation_id=$state.operation_id;reason='tm04-final-verification'}|ConvertTo-Json -Compress|Set-Content -LiteralPath $marker -Encoding utf8
 $count=&$python -c 'import sqlite3; c=sqlite3.connect("file:C:/CodexWorkerData/jobs.sqlite3?mode=ro",uri=True); print(c.execute("select count(*) from jobs where status in (?,?,?,?)",("queued","running","checking","cancelling")).fetchone()[0])'
 if($LASTEXITCODE -ne 0 -or [int]$count -ne 0){throw 'Active jobs; no service stop performed'}
 &$pwsh -NoLogo -NoProfile -NonInteractive -File (Join-Path $root 'scripts\desktop-stop.ps1') -PreserveStartTask
 Check-Exit 'Cannot stop idle candidate safely'
 # The existing recovery bootstrap re-establishes the shared sandbox using the original policy.
 $initial=&$pwsh -NoLogo -NoProfile -NonInteractive -File (Join-Path $root '.runtime\start-worker.local.ps1') -Mode Serve
 Check-Exit 'Original sandbox recovery bootstrap failed'
 &$pwsh -NoLogo -NoProfile -NonInteractive -File (Join-Path $workspace 'scripts\validate-tm-native.local.ps1') -Mode JsonOnly
 Check-Exit 'Native JSON /1 and /2 checks failed'
 # Refresh the authoritative receipt LAST, after every fixed sandbox command has completed.
 &$pwsh -NoLogo -NoProfile -NonInteractive -File (Join-Path $root 'scripts\desktop-stop.ps1') -PreserveStartTask
 Check-Exit 'Cannot stop idle verification instance'
 $doctor=& (Join-Path $root '.runtime\start-worker.local.ps1') -Mode Doctor|ConvertFrom-Json
 $fresh=Wait-Doctor $doctor
 $nativePath=Join-Path $data 'tm04-native-verification.json'
 $native=Get-Content -LiteralPath $nativePath -Raw|ConvertFrom-Json
 $native.sandbox=$fresh
 $native|ConvertTo-Json -Depth 8|Set-Content -LiteralPath $nativePath -Encoding utf8
 $serve=&$pwsh -NoLogo -NoProfile -NonInteractive -File (Join-Path $workspace 'scripts\validate-tm-native.local.ps1') -Mode Serve
 Check-Exit 'Verified service could not start'
 &$python -X utf8 (Join-Path $workspace 'scripts\verify-tm-live.local.py')
 Check-Exit 'Live readiness/TM/archive verification failed'
 # Reuse the existing process, update desktop status through its original startup wrapper.
 &$pwsh -NoLogo -NoProfile -NonInteractive -File (Join-Path $root 'scripts\desktop-start.ps1')
 Check-Exit 'Desktop startup wrapper readiness did not pass'
 $report=Join-Path $workspace 'build\tm-0.4.0\launcher-probe.json'
 $launcher=Join-Path $root 'bin\CodexWorkerLauncher.exe'
 $probe=Start-Process -FilePath $launcher -ArgumentList @('--probe-to',('"'+$report+'"')) -WindowStyle Hidden -PassThru
 if(-not$probe.WaitForExit(45000)){throw 'Launcher readiness probe timed out'}
 if(-not(Test-Path -LiteralPath $report)){throw 'Launcher did not write its probe report'}
 Get-Content -LiteralPath $report
 Write-Output 'TM04_UPGRADE_AND_LIVE_VERIFICATION_PASSED'
}finally{
 $lock.Dispose()
}
