#Requires -Version 5.1
<#
.SYNOPSIS
Creates a standard Windows account and deploys the verified Worker portable ZIP for it.
.EXAMPLE
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\Configure-CodexWorkerUser.ps1 -AllowRemoteDesktop
.EXAMPLE
.\Configure-CodexWorkerUser.ps1 -PackageZip 'D:\Downloads\CodexWorker-Portable-0.4.0-Official-WithSource.zip' -WhatIf
#>
[CmdletBinding(SupportsShouldProcess=$true)]
param(
    [ValidatePattern('^[A-Za-z][A-Za-z0-9_-]{2,19}$')][string]$UserName = 'CodexWorker',
    [string]$PackageZip,
    [string]$InstallRoot,
    [switch]$AllowRemoteDesktop,
    [Security.SecureString]$Password,
    [ValidatePattern('^[A-Fa-f0-9]{64}$')][string]$ExpectedSha256 = '56d1b82eea9cd84365b9fc4c9b4f2c7ac27a5affdaaf07a535aa6b576fb4a162'
)

Set-StrictMode -Version 2
$ErrorActionPreference = 'Stop'
$script:SetupScriptDirectory = $PSScriptRoot
$script:PackageName = 'CodexWorker-Portable-0.4.0-Official-WithSource.zip'
$script:MarkerName = '.codex-worker-user-setup.json'

function Assert-WorkerSetupEnvironment {
    if (-not [Environment]::Is64BitOperatingSystem -or -not [Environment]::Is64BitProcess) {
        throw '请使用 Windows x64 上的 64 位 PowerShell。'
    }
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw '这个配置脚本需要管理员 PowerShell；配置完成后 Worker 本身由标准用户普通双击运行。'
    }
    foreach ($name in @('Get-LocalUser','New-LocalUser','Get-LocalGroupMember','Add-LocalGroupMember')) {
        $null = Get-Command $name -ErrorAction Stop
    }
    $role = (Get-CimInstance Win32_ComputerSystem -ErrorAction Stop).DomainRole
    if ($role -in @(4,5)) { throw '域控制器没有本地用户；请在普通成员服务器或独立服务器上使用本脚本。' }
}

function Get-WorkerInstallPath([string]$Path, [string]$AccountName) {
    if (-not $Path) { $Path = Join-Path $env:ProgramData ('CodexWorkerUsers\' + $AccountName) }
    if (-not [IO.Path]::IsPathRooted($Path) -or $Path.StartsWith('\\') -or $Path.Contains('"')) {
        throw 'InstallRoot 必须是本地磁盘上的绝对目录。'
    }
    $full = [IO.Path]::GetFullPath($Path).TrimEnd('\')
    if ($full -eq [IO.Path]::GetPathRoot($full).TrimEnd('\')) { throw '不能使用磁盘根目录。' }
    foreach ($reserved in @($env:WINDIR, $env:ProgramData, $env:ProgramFiles, $env:USERPROFILE)) {
        if ($reserved -and $full.Equals($reserved.TrimEnd('\'), [StringComparison]::OrdinalIgnoreCase)) {
            throw '请使用新的专用子目录，不能直接使用系统或个人目录。'
        }
    }
    Assert-NoWorkerPathLinks $full
    return $full
}

function Assert-NoWorkerPathLinks([string]$Path) {
    $cursor = [IO.Path]::GetFullPath($Path)
    while ($cursor) {
        if (Test-Path -LiteralPath $cursor) {
            $item = Get-Item -LiteralPath $cursor -Force
            if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
                throw ('目录不能经过符号链接或联接：' + $cursor)
            }
        }
        $parent = [IO.Directory]::GetParent($cursor)
        if ($null -eq $parent) { break }
        $cursor = $parent.FullName
    }
}

function Get-WorkerGroupMembers([string]$GroupSid) {
    return @(Get-LocalGroupMember -SID ([Security.Principal.SecurityIdentifier]::new($GroupSid)) -ErrorAction Stop)
}

function Assert-StandardWorkerAccount($Account) {
    if (-not $Account.Enabled) { throw '同名 Windows 用户已禁用；本脚本不会自动启用或修改它，请换用户名。' }
    if ($Account.SID.Value.EndsWith('-500')) { throw '不能使用 Windows 内置 Administrator 账户。' }
    $admins = Get-WorkerGroupMembers 'S-1-5-32-544'
    if (@($admins | Where-Object { $_.SID.Value -eq $Account.SID.Value }).Count) {
        throw '同名 Windows 用户属于 Administrators；本脚本不会降级现有账户，请换一个新用户名。'
    }
}

function Add-WorkerGroupMembership($Account, [string]$GroupSid) {
    $members = Get-WorkerGroupMembers $GroupSid
    if (-not @($members | Where-Object { $_.SID.Value -eq $Account.SID.Value }).Count) {
        Add-LocalGroupMember -SID ([Security.Principal.SecurityIdentifier]::new($GroupSid)) -Member $Account -ErrorAction Stop
    }
}

function Read-WorkerAccountPassword {
    $first = Read-Host '设置新 Windows 用户密码（输入不回显）' -AsSecureString
    $second = Read-Host '再次输入密码' -AsSecureString
    $one = [IntPtr]::Zero
    $two = [IntPtr]::Zero
    try {
        if ($first.Length -eq 0 -or $first.Length -ne $second.Length) { throw '两次密码为空或不一致，请重新运行。' }
        $one = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($first)
        $two = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($second)
        for ($index=0; $index -lt $first.Length; $index++) {
            if ([Runtime.InteropServices.Marshal]::ReadInt16($one, 2*$index) -ne [Runtime.InteropServices.Marshal]::ReadInt16($two, 2*$index)) {
                throw '两次密码不一致，请重新运行。'
            }
        }
        return $first
    }
    catch { $first.Dispose(); throw }
    finally {
        if ($one -ne [IntPtr]::Zero) { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($one) }
        if ($two -ne [IntPtr]::Zero) { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($two) }
        $second.Dispose()
    }
}

function Resolve-WorkerPackage([string]$Path, [string]$ExpectedHash) {
    if (-not $Path) {
        $candidates = @(
            (Join-Path $script:SetupScriptDirectory $script:PackageName),
            (Join-Path (Get-Location).Path $script:PackageName),
            (Join-Path $env:USERPROFILE ('Downloads\' + $script:PackageName))
        )
        $Path = $candidates | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } | Select-Object -First 1
    }
    if (-not $Path -or -not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw ('找不到迁移 ZIP。请将 ' + $script:PackageName + ' 放在脚本同目录，或使用 -PackageZip 指定完整路径。')
    }
    $full = (Resolve-Path -LiteralPath $Path).ProviderPath
    if ((Get-FileHash -LiteralPath $full -Algorithm SHA256).Hash -ne $ExpectedHash) {
        throw '迁移 ZIP 的 SHA-256 不匹配；尚未创建或修改 Windows 用户。请重新下载本次交付的原始 ZIP。'
    }
    return $full
}

function Assert-WorkerArchiveEntries($Archive, [string]$Destination) {
    $prefix = [IO.Path]::GetFullPath($Destination).TrimEnd('\') + '\'
    $names = [Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
    [long]$bytes = 0
    foreach ($entry in $Archive.Entries) {
        $relative = $entry.FullName.Replace('\','/')
        if (-not $relative.StartsWith('CodexWorker-Portable/', [StringComparison]::Ordinal) -or
            $relative.Contains(':') -or ($relative.Split('/') -contains '..') -or
            [IO.Path]::IsPathRooted($relative) -or -not $names.Add($relative)) {
            throw 'ZIP 含非法、重复或不属于便携包的路径。'
        }
        foreach ($part in $relative.TrimEnd('/').Split('/')) {
            if (-not $part -or $part.EndsWith('.') -or $part.EndsWith(' ') -or
                $part -match '^(?i:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)') {
                throw 'ZIP 含 Windows 不支持的文件名。'
            }
        }
        $target = [IO.Path]::GetFullPath((Join-Path $Destination $relative.Replace('/','\')))
        if (-not $target.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) { throw 'ZIP 路径超出目标目录。' }
        $unixType = ($entry.ExternalAttributes -shr 16) -band 61440
        if ($unixType -eq 40960 -or ($entry.ExternalAttributes -band 1024)) { throw 'ZIP 不能包含链接。' }
        $bytes += $entry.Length
    }
    if ($bytes -gt 3GB) { throw 'ZIP 解压大小超出本版运行组件范围。' }
    foreach ($required in @('CodexWorker.exe','portable.py','worker-settings.json','.runtime/python313/python.exe',
            'bin/CodexRemoteWorker/CodexRemoteWorker.exe','internal/verify-json-runtime.py','package-manifest.json')) {
        if (-not $names.Contains('CodexWorker-Portable/' + $required)) { throw ('ZIP 缺少组件：' + $required) }
    }
    return $bytes
}

function Set-WorkerDirectoryAcl([string]$Path, [string]$UserSid, [switch]$ParentOnly) {
    $acl = [Security.AccessControl.DirectorySecurity]::new()
    $acl.SetAccessRuleProtection($true, $false)
    $acl.SetOwner([Security.Principal.SecurityIdentifier]::new('S-1-5-32-544'))
    foreach ($sid in @('S-1-5-18','S-1-5-32-544')) {
        $rule = [Security.AccessControl.FileSystemAccessRule]::new(
            [Security.Principal.SecurityIdentifier]::new($sid), 'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Allow')
        $acl.AddAccessRule($rule)
    }
    if ($ParentOnly) {
        $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new(
            [Security.Principal.SecurityIdentifier]::new('S-1-5-32-545'), 'ReadAndExecute', 'ContainerInherit,ObjectInherit', 'None', 'Allow'))
    }
    elseif ($UserSid) {
        $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new(
            [Security.Principal.SecurityIdentifier]::new($UserSid), 'Modify', 'ContainerInherit,ObjectInherit', 'None', 'Allow'))
    }
    Set-Acl -LiteralPath $Path -AclObject $acl
}

function Initialize-WorkerParent([string]$Path) {
    Assert-NoWorkerPathLinks $Path
    if (-not (Test-Path -LiteralPath $Path)) {
        $null = New-Item -ItemType Directory -Path $Path
        Assert-NoWorkerPathLinks $Path
        $owner = (Get-Acl -LiteralPath $Path).GetOwner([Security.Principal.SecurityIdentifier]).Value
        if ($owner -notin @('S-1-5-18','S-1-5-32-544',[Security.Principal.WindowsIdentity]::GetCurrent().User.Value)) {
            throw '新建的父目录所有者发生变化，请选择其他专用目录。'
        }
        Set-WorkerDirectoryAcl -Path $Path -ParentOnly
    }
    else {
        $acl = Get-Acl -LiteralPath $Path
        $trusted = @('S-1-5-18','S-1-5-32-544',[Security.Principal.WindowsIdentity]::GetCurrent().User.Value,
            'S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464')
        if ($acl.GetOwner([Security.Principal.SecurityIdentifier]).Value -notin $trusted) { throw '已有父目录由其他用户拥有，请选择新的受保护目录。' }
        $writes = [Security.AccessControl.FileSystemRights]'Write,Delete,ChangePermissions,TakeOwnership,DeleteSubdirectoriesAndFiles'
        foreach ($rule in $acl.GetAccessRules($true,$true,[Security.Principal.SecurityIdentifier])) {
            if ($rule.AccessControlType -eq 'Allow' -and $rule.IdentityReference.Value -notin $trusted -and
                ($rule.PropagationFlags -band [Security.AccessControl.PropagationFlags]::InheritOnly) -eq 0 -and ($rule.FileSystemRights -band $writes)) {
                throw '已有父目录可被普通用户修改，请使用默认目录或另一个专用目录。'
            }
        }
    }
}

function Initialize-WorkerShortcutType {
    if ('CodexWorkerUserSetup.Shortcuts' -as [type]) { return }
    Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
using System.Runtime.InteropServices.ComTypes;
using System.Text;
namespace CodexWorkerUserSetup {
    [ComImport, Guid("00021401-0000-0000-C000-000000000046")]
    class ShellLink {}
    [ComImport, Guid("000214F9-0000-0000-C000-000000000046"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface ILink {
        void GetPath([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder path, int count, IntPtr data, uint flags);
        void GetIDList(out IntPtr item); void SetIDList(IntPtr item);
        void GetDescription([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder text, int count);
        void SetDescription([MarshalAs(UnmanagedType.LPWStr)] string text);
        void GetWorkingDirectory([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder path, int count);
        void SetWorkingDirectory([MarshalAs(UnmanagedType.LPWStr)] string path);
        void GetArguments([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder text, int count);
        void SetArguments([MarshalAs(UnmanagedType.LPWStr)] string text);
        void GetHotkey(out short hotkey); void SetHotkey(short hotkey);
        void GetShowCmd(out int show); void SetShowCmd(int show);
        void GetIconLocation([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder path, int count, out int index);
        void SetIconLocation([MarshalAs(UnmanagedType.LPWStr)] string path, int index);
        void SetRelativePath([MarshalAs(UnmanagedType.LPWStr)] string path, uint reserved);
        void Resolve(IntPtr window, uint flags);
        void SetPath([MarshalAs(UnmanagedType.LPWStr)] string path);
    }
    public static class Shortcuts {
        public static void Save(string file, string target, string directory, string description) {
            ILink link = (ILink)new ShellLink();
            try { link.SetPath(target); link.SetWorkingDirectory(directory); link.SetDescription(description);
                link.SetShowCmd(1); ((IPersistFile)link).Save(file, true); }
            finally { Marshal.FinalReleaseComObject(link); }
        }
        public static string Target(string file) { return Read(file, false); }
        public static string Directory(string file) { return Read(file, true); }
        static string Read(string file, bool directory) {
            ILink link = (ILink)new ShellLink();
            try { ((IPersistFile)link).Load(file, 0); StringBuilder text = new StringBuilder(32768);
                if (directory) link.GetWorkingDirectory(text, text.Capacity);
                else link.GetPath(text, text.Capacity, IntPtr.Zero, 0);
                return text.ToString(); }
            finally { Marshal.FinalReleaseComObject(link); }
        }
    }
}
'@
}

function Write-WorkerDesktopShortcut([string]$Root, [string]$AccountName, [string]$DesktopDirectory) {
    $desktop = $DesktopDirectory
    if (-not $desktop) { $desktop = [Environment]::GetFolderPath('CommonDesktopDirectory') }
    if (-not $desktop) { throw '找不到公共桌面，无法建立启动入口。' }
    Assert-NoWorkerPathLinks $desktop
    Initialize-WorkerShortcutType
    $path = Join-Path $desktop ('Codex Worker - ' + $AccountName + '.lnk')
    if (Test-Path -LiteralPath $path) {
        Assert-NoWorkerPathLinks $path
        if ([CodexWorkerUserSetup.Shortcuts]::Target($path) -ne (Join-Path $Root 'CodexWorker.exe')) { throw '公共桌面有同名其他快捷方式，未覆盖。' }
    }
    [CodexWorkerUserSetup.Shortcuts]::Save($path, (Join-Path $Root 'CodexWorker.exe'), $Root,
        ('请先用 Windows 用户 ' + $AccountName + ' 登录，再普通双击。'))
    return $path
}

function Invoke-CodexWorkerUserSetup {
    [CmdletBinding(SupportsShouldProcess=$true)]
    param(
        [ValidatePattern('^[A-Za-z][A-Za-z0-9_-]{2,19}$')][string]$UserName = 'CodexWorker',
        [string]$PackageZip, [string]$InstallRoot, [switch]$AllowRemoteDesktop,
        [Security.SecureString]$Password,
        [ValidatePattern('^[A-Fa-f0-9]{64}$')][string]$ExpectedSha256 = '56d1b82eea9cd84365b9fc4c9b4f2c7ac27a5affdaaf07a535aa6b576fb4a162'
    )
    $root = Get-WorkerInstallPath $InstallRoot $UserName
    $action = '配置标准用户 ' + $UserName + '、部署已验证便携 ZIP、创建桌面快捷方式；允许 RDP=' + [bool]$AllowRemoteDesktop
    if (-not $PSCmdlet.ShouldProcess($root, $action)) { return }
    Assert-WorkerSetupEnvironment
    $zip = Resolve-WorkerPackage $PackageZip $ExpectedSha256
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $archive = [IO.Compression.ZipFile]::OpenRead($zip)
    try { $unpackedBytes = Assert-WorkerArchiveEntries $archive (Join-Path ([IO.Path]::GetTempPath()) 'CodexWorkerEntryValidation') }
    finally { $archive.Dispose() }

    $account = Get-LocalUser -Name $UserName -ErrorAction SilentlyContinue
    if ($account) { Assert-StandardWorkerAccount $account }
    $marker = Join-Path $root $script:MarkerName
    $reuse = Test-Path -LiteralPath $root
    if ($reuse) {
        if (-not $account -or -not (Test-Path -LiteralPath $marker -PathType Leaf)) { throw '目标目录已存在且不属于本脚本的同一用户部署，请选择新的 InstallRoot。' }
        Assert-NoWorkerPathLinks $marker
        $prior = Get-Content -LiteralPath $marker -Raw -Encoding UTF8 | ConvertFrom-Json
        if ($prior.user_sid -ne $account.SID.Value -or $prior.package_sha256 -ne $ExpectedSha256) { throw '已有部署身份或版本不匹配，未覆盖。' }
        foreach ($name in @('CodexWorker.exe','portable.py','worker-settings.json')) {
            if (-not (Test-Path -LiteralPath (Join-Path $root $name) -PathType Leaf)) { throw '已有部署不完整，请选择新目录；未覆盖现有 Data。' }
        }
    }

    $parent = Split-Path -Parent $root
    Initialize-WorkerParent $parent
    $drive = [IO.DriveInfo]::new([IO.Path]::GetPathRoot($root))
    if (-not $reuse -and $drive.AvailableFreeSpace -lt ($unpackedBytes + 512MB)) { throw '目标磁盘空间不足，请至少为解压组件和任务预留空间。' }
    if (-not $account) {
        $ownsPassword = $false
        if ($null -eq $Password) { $Password = Read-WorkerAccountPassword; $ownsPassword = $true }
        try {
            $account = New-LocalUser -Name $UserName -Password $Password -FullName 'Codex Worker' -Description 'Codex Worker standard desktop user' -ErrorAction Stop
        }
        finally { if ($ownsPassword) { $Password.Dispose() } }
        Write-Host ('已创建标准 Windows 用户：.\' + $UserName)
    }
    else { Write-Host '复用已有标准用户，没有重置其密码。' }
    Add-WorkerGroupMembership $account 'S-1-5-32-545'
    if ($AllowRemoteDesktop) { Add-WorkerGroupMembership $account 'S-1-5-32-555' }
    Assert-StandardWorkerAccount $account

    if (-not $reuse) {
        $stage = Join-Path $parent ('.setup-' + [Guid]::NewGuid().ToString('N'))
        $null = New-Item -ItemType Directory -Path $stage
        Set-WorkerDirectoryAcl -Path $stage
        Write-Host '正在解压运行组件，请等待……'
        [IO.Compression.ZipFile]::ExtractToDirectory($zip, $stage)
        $stagedRoot = Join-Path $stage 'CodexWorker-Portable'
        Assert-NoWorkerPathLinks $stagedRoot
        if (-not $stagedRoot.StartsWith($parent.TrimEnd('\')+'\',[StringComparison]::OrdinalIgnoreCase) -or
            -not $root.StartsWith($parent.TrimEnd('\')+'\',[StringComparison]::OrdinalIgnoreCase)) { throw '部署目录超出预期父目录。' }
        if (Test-Path -LiteralPath $root) { throw '目标目录在配置期间被占用，已停止。' }
        Move-Item -LiteralPath $stagedRoot -Destination $root
        [IO.Directory]::Delete($stage) # Only the now-empty, freshly-created staging directory.
        $state = [ordered]@{schema='codex-worker-user-setup/1';user_name=$UserName;user_sid=$account.SID.Value;
            package_sha256=$ExpectedSha256.ToLowerInvariant();worker_version='0.4.0';
            codex_login_checked=$false;sandbox_checked=$false;created_at_utc=[DateTime]::UtcNow.ToString('o')}
        $state | ConvertTo-Json | Set-Content -LiteralPath $marker -Encoding UTF8
        Set-WorkerDirectoryAcl -Path $root -UserSid $account.SID.Value
    }
    $shortcut = Write-WorkerDesktopShortcut $root $UserName
    Write-Host ''
    Write-Host 'Windows 用户和 Worker 文件已配置完成。尚未检查 Codex 登录或沙箱，尚未启动 Worker。' -ForegroundColor Green
    Write-Host ('1. 用 Windows 用户 .\' + $UserName + ' 登录这台电脑，密码是你设置的 Windows 密码。')
    Write-Host '2. 在新用户内安装/打开 Codex，登录你现有的官方 ChatGPT/Codex 账号，并确认能正常运行工具任务。'
    Write-Host ('3. 普通双击桌面入口：' + $shortcut)
    Write-Host ('4. 等“服务已就绪”，连接信息在：' + (Join-Path $root 'Data\private\connection.txt'))
    if ($AllowRemoteDesktop) { Write-Host '已加入远程桌面用户组；现有远程桌面启用状态、组织策略和防火墙保持原样。' }
    Write-Host '原 Administrator 会话、原 Codex 登录、系统沙箱及已有服务未作修改。'
}

if ($MyInvocation.InvocationName -ne '.') {
    try { Invoke-CodexWorkerUserSetup @PSBoundParameters }
    catch { Write-Error ('配置未完成：' + $_.Exception.Message); exit 1 }
}
