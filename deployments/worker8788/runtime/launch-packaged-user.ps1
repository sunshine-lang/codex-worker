param([string]$Mode,[string]$OperationId)
$ErrorActionPreference='Stop'
$python='C:\CodexRemoteWorker\.runtime\python313\pythonw.exe'
$script='C:\CodexRemoteWorker-AppServer-8788\packaged-background.py'
if(-not(Test-Path -LiteralPath $python)){throw 'No pythonw.exe runtime'}
if($Mode -notin @('identity-probe','protocol-probe','protocol-probe-no-environment','doctor','serve')){throw 'Invalid mode'}
[void][Guid]::Parse($OperationId)
Invoke-CommandInDesktopPackage -PackageFamilyName 'OpenAI.Codex_2p2nqsd0c76g0' -AppId 'App' -Command $python -Args ('-X utf8 "'+$script+'" '+$Mode+' --operation-id '+$OperationId) -PreventBreakaway
