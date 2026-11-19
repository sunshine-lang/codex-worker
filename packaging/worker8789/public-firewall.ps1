$ErrorActionPreference = 'Stop'
$name = 'CodexWorker8789-Public-TCP'
if (!(Get-NetFirewallRule -Name $name -ErrorAction SilentlyContinue)) {
    $route = Get-NetRoute -AddressFamily IPv4 -DestinationPrefix '0.0.0.0/0' |
        Sort-Object { $_.RouteMetric + $_.InterfaceMetric } | Select-Object -First 1
    $rule = @{
        Name = $name
        DisplayName = 'Codex Worker 8789 - Public'
        Direction = 'Inbound'
        Action = 'Allow'
        Profile = 'Public'
        Program = (Get-Item -LiteralPath 'C:\CodexRemoteWorker\.runtime\python313\pythonw.exe').FullName
        InterfaceAlias = $route.InterfaceAlias
        Protocol = 'TCP'
        LocalPort = 8789
    }
    New-NetFirewallRule @rule | Out-Null
}
