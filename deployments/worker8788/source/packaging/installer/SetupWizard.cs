using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.IO.Compression;
using System.Linq;
using System.Net.NetworkInformation;
using System.Runtime.InteropServices;
using System.Security.AccessControl;
using System.Security.Cryptography;
using System.Security.Principal;
using System.Text;
using System.Threading.Tasks;
using System.Web.Script.Serialization;
using System.Windows.Forms;
using Microsoft.Win32;

namespace CodeXSetup
{
    internal sealed class PayloadStream : Stream
    {
        readonly Stream stream; readonly long offset, length; long position;
        internal PayloadStream(Stream stream,long offset,long length) { this.stream=stream; this.offset=offset; this.length=length; position=0; }
        public override bool CanRead { get { return true; } } public override bool CanSeek { get { return true; } } public override bool CanWrite { get { return false; } }
        public override long Length { get { return length; } } public override long Position { get { return position; } set { Seek(value,SeekOrigin.Begin); } }
        public override int Read(byte[] buffer,int index,int count) { if(position>=length)return 0; stream.Position=offset+position; int result=stream.Read(buffer,index,(int)Math.Min(count,length-position)); position+=result; return result; }
        public override long Seek(long value,SeekOrigin origin) { long next=origin==SeekOrigin.Begin?value:origin==SeekOrigin.Current?position+value:length+value; if(next<0||next>length)throw new IOException("Invalid package stream position"); position=next; return position; }
        public override void Flush(){} public override void SetLength(long value){throw new NotSupportedException();} public override void Write(byte[] buffer,int index,int count){throw new NotSupportedException();}
    }
    internal sealed class Package
    {
        internal const string ProductId="5ea84759-74a0-438e-bcaf-cf19803d8053";
        internal static readonly string[] Required={"app/bin/CodexRemoteWorker/CodexRemoteWorker.exe","app/bin/CodexWorkerLauncher.exe","app/Uninstall.exe","app/.runtime/python313/python.exe","app/.runtime/powershell/pwsh.exe","app/tools/codex/bin/codex.exe","app/tools/LibreOffice/program/soffice.exe","app/installer/configure-install.ps1","app/installer/write_model_config.py","app/installer/verify-json-runtime.py","app/.runtime/start-worker.local.ps1","app/.runtime/wfp-network-lockdown.py","app/scripts/desktop-ensure.ps1","app/scripts/desktop-repair.ps1","app/package-manifest.json","dependencies/vc_redist.x64.exe"};
        internal string Path; internal long Offset,Length; internal byte[] Hash;
        internal Package(string path)
        {
            Path=path; using(FileStream file=File.OpenRead(path)) { if(file.Length<64)throw new InvalidDataException("安装包不完整。"); file.Position=file.Length-64; using(BinaryReader reader=new BinaryReader(file,Encoding.ASCII,true)) { if(Encoding.ASCII.GetString(reader.ReadBytes(16))!="CRWSETUP20261109")throw new InvalidDataException("安装包校验标记缺失。"); Offset=reader.ReadInt64(); Length=reader.ReadInt64(); Hash=reader.ReadBytes(32); } if(Offset<0||Length<=0||Offset+Length!=file.Length-64)throw new InvalidDataException("安装包长度无效。"); }
        }
        internal void Verify()
        {
            using(FileStream file=File.OpenRead(Path)) using(PayloadStream payload=new PayloadStream(file,Offset,Length)) using(SHA256 hash=SHA256.Create()) { if(!hash.ComputeHash(payload).SequenceEqual(Hash))throw new InvalidDataException("安装包校验失败，请重新复制安装文件。"); }
            using(FileStream file=File.OpenRead(Path)) using(PayloadStream payload=new PayloadStream(file,Offset,Length)) using(ZipArchive archive=new ZipArchive(payload,ZipArchiveMode.Read))
            {
                HashSet<string> names=new HashSet<string>(StringComparer.OrdinalIgnoreCase);
                foreach(ZipArchiveEntry entry in archive.Entries) { ValidateEntry(entry.FullName); if(!names.Add(entry.FullName))throw new InvalidDataException("安装包存在重复路径。"); }
                foreach(string required in Required)if(!names.Contains(required))throw new InvalidDataException("安装包缺少组件："+required);
            }
        }
        internal static void ValidateEntry(string name)
        {
            if(String.IsNullOrEmpty(name)||System.IO.Path.IsPathRooted(name)||name.Contains(':')||name.Replace('\\','/').Split('/').Any(part=>part==".."))throw new InvalidDataException("安装包路径无效。");
            string normalized=name.Replace('\\','/'); if(!normalized.StartsWith("app/",StringComparison.Ordinal)||!normalized.Contains('/')) { if(!normalized.StartsWith("dependencies/",StringComparison.Ordinal))throw new InvalidDataException("安装包组件根目录无效。"); }
            string[] parts=normalized.Split('/');
            if(parts.Any(part=>new[]{"private",".codex",".aws","jobs"}.Contains(part,StringComparer.OrdinalIgnoreCase)) || (parts.Length>1 && parts[0]=="app" && parts[1].Equals("Data",StringComparison.OrdinalIgnoreCase)))throw new InvalidDataException("安装包不得包含本机数据或账户状态。");
            string leaf=System.IO.Path.GetFileName(name); if(new[]{"credentials.local.json","service-account.local.json","auth.json","config.local.json","service-token.txt","bootstrap-credentials.local.json","install-plan.json","service-account.json","a.txt"}.Contains(leaf,StringComparer.OrdinalIgnoreCase))throw new InvalidDataException("安装包不应包含现有密钥文件。");
        }
        static string SafeTarget(string root,string relative)
        {
            string full=System.IO.Path.GetFullPath(System.IO.Path.Combine(root,relative.Replace('/',System.IO.Path.DirectorySeparatorChar)));
            if(!full.StartsWith(System.IO.Path.GetFullPath(root).TrimEnd('\\')+"\\",StringComparison.OrdinalIgnoreCase))throw new InvalidDataException("安装包路径超出目标目录。"); return full;
        }
        internal void Extract(string applicationRoot,string dependencyRoot,IProgress<string> progress)
        {
            using(FileStream file=File.OpenRead(Path)) using(PayloadStream payload=new PayloadStream(file,Offset,Length)) using(ZipArchive archive=new ZipArchive(payload,ZipArchiveMode.Read))
            {
                int index=0;
                foreach(ZipArchiveEntry entry in archive.Entries)
                {
                    ValidateEntry(entry.FullName); bool app=entry.FullName.StartsWith("app/",StringComparison.Ordinal); string relative=entry.FullName.Substring(app?4:13); if(relative.Length==0)continue;
                    string target=SafeTarget(app?applicationRoot:dependencyRoot,relative);
                    if(entry.FullName.EndsWith("/")) { Directory.CreateDirectory(target); continue; }
                    Directory.CreateDirectory(System.IO.Path.GetDirectoryName(target));
                    using(Stream input=entry.Open())using(FileStream output=new FileStream(target,FileMode.CreateNew,FileAccess.Write,FileShare.None))input.CopyTo(output,65536);
                    if(++index%500==0)progress.Report("正在解压运行文件（"+index+"）…");
                }
            }
        }
    }
    internal static class Installer
    {
        internal static string OwnExecutable { get { return Process.GetCurrentProcess().MainModule.FileName; } }
        internal static void Protect(string path,bool program)
        {
            DirectorySecurity acl=new DirectorySecurity(); acl.SetAccessRuleProtection(true,false);
            SecurityIdentifier admins=new SecurityIdentifier("S-1-5-32-544"), system=new SecurityIdentifier("S-1-5-18"); acl.SetOwner(admins);
            foreach(SecurityIdentifier sid in new[]{admins,system})acl.AddAccessRule(new FileSystemAccessRule(sid,FileSystemRights.FullControl,InheritanceFlags.ContainerInherit|InheritanceFlags.ObjectInherit,PropagationFlags.None,AccessControlType.Allow));
            if(program)acl.AddAccessRule(new FileSystemAccessRule(new SecurityIdentifier("S-1-5-32-545"),FileSystemRights.ReadAndExecute,InheritanceFlags.ContainerInherit|InheritanceFlags.ObjectInherit,PropagationFlags.None,AccessControlType.Allow));
            Directory.CreateDirectory(path,acl);
            DirectoryInfo created=new DirectoryInfo(path);
            if((created.Attributes & FileAttributes.ReparsePoint)!=0)throw new InvalidOperationException("不能安装到链接目录。");
            if(program) {
                DirectorySecurity existing=created.GetAccessControl(); string[] trusted={"S-1-5-32-544","S-1-5-18","S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464"};
                if(!trusted.Contains(((SecurityIdentifier)existing.GetOwner(typeof(SecurityIdentifier))).Value))throw new InvalidOperationException("安装目录由普通用户拥有，请选择新的专用目录。");
                FileSystemRights writes=FileSystemRights.Write|FileSystemRights.Delete|FileSystemRights.ChangePermissions|FileSystemRights.TakeOwnership|FileSystemRights.DeleteSubdirectoriesAndFiles;
                foreach(FileSystemAccessRule rule in existing.GetAccessRules(true,true,typeof(SecurityIdentifier)))if(rule.AccessControlType==AccessControlType.Allow&&(rule.PropagationFlags&PropagationFlags.InheritOnly)==0&&(rule.FileSystemRights&writes)!=0&&!trusted.Contains(rule.IdentityReference.Value))throw new InvalidOperationException("安装目录可被普通用户修改，请选择新的专用目录。");
            }
            created.SetAccessControl(acl);
        }
        static void ValidateProtectedParent(string full)
        {
            DirectoryInfo parent=Directory.GetParent(full);
            while(parent!=null&&!parent.Exists)parent=parent.Parent;
            if(parent==null)throw new InvalidOperationException("找不到安装磁盘。");
            foreach(DirectoryInfo ancestor in Ancestors(parent))if((ancestor.Attributes&FileAttributes.ReparsePoint)!=0)throw new InvalidOperationException("请选择不经过目录链接的安装位置。");
            DirectorySecurity acl=parent.GetAccessControl();
            string[] trusted={"S-1-5-32-544","S-1-5-18","S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464"};
            string owner=((SecurityIdentifier)acl.GetOwner(typeof(SecurityIdentifier))).Value;
            if(!trusted.Contains(owner))throw new InvalidOperationException("为保护后台任务，请选择 Program Files 或管理员拥有的专用目录。");
            FileSystemRights risky=FileSystemRights.ChangePermissions|FileSystemRights.TakeOwnership|FileSystemRights.DeleteSubdirectoriesAndFiles;
            foreach(FileSystemAccessRule rule in acl.GetAccessRules(true,true,typeof(SecurityIdentifier)))
                if(rule.AccessControlType==AccessControlType.Allow&&(rule.PropagationFlags&PropagationFlags.InheritOnly)==0&&(rule.FileSystemRights&risky)!=0&&!trusted.Contains(rule.IdentityReference.Value))
                    throw new InvalidOperationException("所选目录的上级可被普通用户替换，请选择受保护的专用安装目录。");
        }
        static IEnumerable<DirectoryInfo> Ancestors(DirectoryInfo directory){while(directory!=null){yield return directory;directory=directory.Parent;}}
        internal static void ValidateDirectory(string root)
        {
            if(!System.IO.Path.IsPathRooted(root)||root.StartsWith(@"\\")||root.Length>120||root.Contains('"'))throw new InvalidOperationException("请选择不超过 120 个字符的本机绝对路径。");
            string full=System.IO.Path.GetFullPath(root).TrimEnd('\\'); if(full==System.IO.Path.GetPathRoot(full).TrimEnd('\\'))throw new InvalidOperationException("不能直接安装到磁盘根目录。");
            foreach(string reserved in new[]{Environment.GetFolderPath(Environment.SpecialFolder.Windows),Environment.GetFolderPath(Environment.SpecialFolder.ProgramFiles),Environment.GetFolderPath(Environment.SpecialFolder.CommonApplicationData),Environment.GetFolderPath(Environment.SpecialFolder.UserProfile)}) if(String.Equals(full,reserved.TrimEnd('\\'),StringComparison.OrdinalIgnoreCase))throw new InvalidOperationException("请选择一个专用的安装文件夹。");
            ValidateProtectedParent(full); if(Directory.Exists(full)&&(new DirectoryInfo(full).Attributes&FileAttributes.ReparsePoint)!=0)throw new InvalidOperationException("请选择普通目录，不能使用链接目录。");
            if(Directory.Exists(full)&&Directory.EnumerateFileSystemEntries(full).Any())throw new InvalidOperationException("安装目录需要为空，请选择新目录。");
            if(new DriveInfo(System.IO.Path.GetPathRoot(full)).AvailableFreeSpace<3000000000L)throw new InvalidOperationException("安装磁盘可用空间不足 3 GB。");
        }
        internal static void CheckExistingDeployment()
        {
            using(RegistryKey key=Registry.LocalMachine.OpenSubKey(@"SOFTWARE\CodexRemoteWorker"))if(key!=null&&!String.IsNullOrEmpty(Convert.ToString(key.GetValue("InstallDir",""))))throw new InvalidOperationException("本机已有 CodeX 服务安装记录，请先卸载或迁移旧部署。");
            string task=System.IO.Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.Windows),@"System32\Tasks\CodexRemoteWorker-DesktopStart");
            if(File.Exists(task))throw new InvalidOperationException("本机已有 CodeX 服务启动任务，安装程序不会覆盖现有服务。");
        }
        internal static string Quote(string value)
        {
            if(value.Contains('"')||value.Contains('\r')||value.Contains('\n'))throw new ArgumentException("参数中不能包含引号或换行。"); return "\""+value.TrimEnd('\\')+"\"";
        }
        internal static Dictionary<string,object> Install(Dictionary<string,object> plan,IProgress<string> progress)
        {
            string root=Convert.ToString(plan["install_dir"]); ValidateDirectory(root); CheckExistingDeployment();
            Package package=new Package(OwnExecutable); progress.Report("正在校验安装包…"); package.Verify();
            using(FileStream file=File.OpenRead(OwnExecutable)) using(PayloadStream payload=new PayloadStream(file,package.Offset,package.Length)) using(ZipArchive archive=new ZipArchive(payload,ZipArchiveMode.Read)) { if(archive.Entries.Where(entry=>entry.FullName.StartsWith("app/")).Any(entry=>root.Length+entry.FullName.Length-4>245))throw new InvalidOperationException("安装路径过长，请选择更短的目录。"); }
            string temporary=System.IO.Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.CommonApplicationData),"CodeXWorkerSetup",Guid.NewGuid().ToString("N")); Protect(temporary,false); Protect(root,true);
            string planPath=System.IO.Path.Combine(temporary,"install-plan.json");
            try
            {
                progress.Report("正在解压运行文件…"); package.Extract(root,System.IO.Path.Combine(temporary,"dependencies"),progress);
                File.WriteAllText(planPath,new JavaScriptSerializer().Serialize(plan),new UTF8Encoding(false));
                string powerShell=System.IO.Path.Combine(root,@".runtime\powershell\pwsh.exe");
                string arguments="-NoLogo -NoProfile -NonInteractive -WindowStyle Hidden -File "+Quote(System.IO.Path.Combine(root,@"installer\configure-install.ps1"))+" -PlanFile "+Quote(planPath);
                ProcessStartInfo start=new ProcessStartInfo(powerShell,arguments){UseShellExecute=false,CreateNoWindow=true,RedirectStandardOutput=true,RedirectStandardError=true,WorkingDirectory=root};
                foreach(string key in start.EnvironmentVariables.Keys.Cast<string>().ToArray())if(key.StartsWith("CODEX_",StringComparison.OrdinalIgnoreCase))start.EnvironmentVariables.Remove(key);
                start.EnvironmentVariables["PSModulePath"]=System.IO.Path.Combine(root,@".runtime\powershell\Modules")+";"+System.IO.Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.Windows),@"System32\WindowsPowerShell\v1.0\Modules");
                using(Process process=new Process())
                {
                    process.StartInfo=start; process.OutputDataReceived+=delegate(object sender,DataReceivedEventArgs data){if(data.Data!=null&&data.Data.StartsWith("PROGRESS:",StringComparison.Ordinal))progress.Report(data.Data.Substring(9));};
                    process.ErrorDataReceived+=delegate(object sender,DataReceivedEventArgs data){};
                    process.Start(); process.BeginOutputReadLine(); process.BeginErrorReadLine();
                    if(!process.WaitForExit(900000)){try{process.Kill();}catch{}throw new InvalidOperationException("安装初始化超时，请查看安装目录 Data 中的日志。");}
                    process.WaitForExit(); if(process.ExitCode!=0)throw new InvalidOperationException("本机初始化未完成，请查看 "+System.IO.Path.Combine(root,@"Data\install.log")+" 以及自检日志。");
                }
                string resultPath=System.IO.Path.Combine(root,@"Data\private\installation-result.json");
                Dictionary<string,object> result=new JavaScriptSerializer().Deserialize<Dictionary<string,object>>(File.ReadAllText(resultPath));
                if(!result.ContainsKey("success")||!Convert.ToBoolean(result["success"]))throw new InvalidOperationException("安装结果校验失败。");
                return result;
            }
            finally { try{if(File.Exists(planPath))File.Delete(planPath);}catch{} }
        }
    }
    internal sealed class Wizard : Form
    {
        TextBox directory,baseUrl,model,key,publicUrl; NumericUpDown port; CheckBox remote;
        Button install,cancel; Panel formPanel,progressPanel,finishPanel; Label progressText; ProgressBar bar; bool busy,complete; string installedRoot,tokenValue;
        TextBox finishedApi,finishedToken; CheckBox launchTray;
        internal Wizard()
        {
            Text="CodeX Worker 0.4.0 安装向导"; ClientSize=new Size(690,600); StartPosition=FormStartPosition.CenterScreen; Font=new Font("Microsoft YaHei UI",10); FormBorderStyle=FormBorderStyle.FixedDialog; MaximizeBox=false; BackColor=Color.White; Icon=SystemIcons.Application;
            Label title=new Label{Text="安装 CodeX Worker 0.4.0",Location=new Point(28,22),Size=new Size(630,36),Font=new Font(Font.FontFamily,20,FontStyle.Bold),ForeColor=Color.FromArgb(30,64,175)};
            Label subtitle=new Label{Text="选择安装目录和端口。安装成功后自动生成访问 Token。",Location=new Point(30,66),Size=new Size(630,30),ForeColor=Color.DimGray};
            formPanel=new Panel{Location=new Point(30,112),Size=new Size(630,410)};
            AddLabel(formPanel,"安装位置",0); directory=AddText(formPanel,System.IO.Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ProgramFiles),"CodeX Worker"),26,520);
            Button browse=new Button{Text="浏览…",Location=new Point(532,24),Size=new Size(98,31)}; browse.Click+=delegate{using(FolderBrowserDialog dialog=new FolderBrowserDialog()){dialog.Description="选择 CodeX 服务安装位置";dialog.SelectedPath=directory.Text;if(dialog.ShowDialog(this)==DialogResult.OK)directory.Text=dialog.SelectedPath;}};formPanel.Controls.Add(browse);
            AddLabel(formPanel,"API 端口",72); port=new NumericUpDown{Minimum=1,Maximum=65535,Value=8787,Location=new Point(0,98),Size=new Size(130,29)}; formPanel.Controls.Add(port);
            remote=new CheckBox{Text="允许远程访问，并开放本机 Windows 防火墙端口",Checked=true,Location=new Point(150,98),Size=new Size(480,30)};formPanel.Controls.Add(remote);
            AddLabel(formPanel,"固定访问地址 / 域名（可留空，自动识别当前地址）",142);publicUrl=AddText(formPanel,"",168,630);
            AddLabel(formPanel,"模型服务 API 地址",212);baseUrl=AddText(formPanel,"https://openrouter.ai/api/v1",238,630);
            AddLabel(formPanel,"模型名称",282);model=AddText(formPanel,"deepseek/deepseek-v4.1-flash-1017",308,630);
            AddLabel(formPanel,"模型 API Key（可留空，翻译前再配置）",352);key=AddText(formPanel,"",378,630);key.UseSystemPasswordChar=true;
            progressPanel=new Panel{Location=formPanel.Location,Size=formPanel.Size,Visible=false};progressText=new Label{Text="正在准备安装…",Location=new Point(0,80),Size=new Size(630,80),Font=new Font(Font.FontFamily,13)};bar=new ProgressBar{Location=new Point(0,180),Size=new Size(630,18),Style=ProgressBarStyle.Marquee};progressPanel.Controls.AddRange(new Control[]{progressText,bar});
            finishPanel=new Panel{Location=formPanel.Location,Size=formPanel.Size,Visible=false};AddLabel(finishPanel,"安装完成，已生成访问 Token。",0);AddLabel(finishPanel,"API 地址",60);finishedApi=AddText(finishPanel,"",86,630);finishedApi.ReadOnly=true;AddLabel(finishPanel,"访问 Token",130);finishedToken=AddText(finishPanel,"",156,630);finishedToken.ReadOnly=true;finishedToken.UseSystemPasswordChar=true;
            CheckBox show=new CheckBox{Text="显示 Token",Location=new Point(0,198),AutoSize=true};show.CheckedChanged+=delegate{finishedToken.UseSystemPasswordChar=!show.Checked;};
            Button copy=new Button{Text="复制连接信息",Location=new Point(180,192),Size=new Size(140,34)};copy.Click+=delegate{Clipboard.SetText("token："+tokenValue+"\r\napi："+finishedApi.Text+"\r\n");};
            Button save=new Button{Text="保存到桌面",Location=new Point(335,192),Size=new Size(140,34)};save.Click+=delegate{try{string path=System.IO.Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.DesktopDirectory),"连接信息.txt");File.WriteAllText(path,"token："+tokenValue+"\r\napi："+finishedApi.Text+"\r\n",new UTF8Encoding(false));MessageBox.Show(this,"已保存到：\r\n"+path,Text,MessageBoxButtons.OK,MessageBoxIcon.Information);}catch(Exception error){MessageBox.Show(this,error.Message,Text,MessageBoxButtons.OK,MessageBoxIcon.Information);}};
            launchTray=new CheckBox{Text="完成后打开 CodeX 服务启动器",Checked=true,Location=new Point(0,260),Size=new Size(610,30)};
            Label note=new Label{Text="关闭启动器窗口会隐藏到右下角托盘。右键选择“退出并停止服务”后，服务才会停止。云服务器还需在安全组放行所选端口。",Location=new Point(0,308),Size=new Size(630,84),ForeColor=Color.DimGray};finishPanel.Controls.AddRange(new Control[]{show,copy,save,launchTray,note});
            install=new Button{Text="安装",Location=new Point(420,547),Size=new Size(110,35)};cancel=new Button{Text="取消",Location=new Point(545,547),Size=new Size(110,35)};install.Click+=delegate{if(complete){Finish();}else{BeginInstall();}};cancel.Click+=delegate{Close();};
            Controls.AddRange(new Control[]{title,subtitle,formPanel,progressPanel,finishPanel,install,cancel});FormClosing+=delegate(object sender,FormClosingEventArgs e){if(busy)e.Cancel=true;};
        }
        static void AddLabel(Control parent,string text,int y){parent.Controls.Add(new Label{Text=text,Location=new Point(0,y),Size=new Size(630,25)});}
        static TextBox AddText(Control parent,string value,int y,int width){TextBox box=new TextBox{Text=value,Location=new Point(0,y),Size=new Size(width,28)};parent.Controls.Add(box);return box;}
        void Finish(){if(launchTray.Checked)Process.Start(new ProcessStartInfo(System.IO.Path.Combine(installedRoot,@"bin\CodexWorkerLauncher.exe"),"--root "+Installer.Quote(installedRoot)){UseShellExecute=false,WorkingDirectory=installedRoot});Close();}
        async void BeginInstall()
        {
            Dictionary<string,object> plan;
            try
            {
                installedRoot=System.IO.Path.GetFullPath(directory.Text.Trim()).TrimEnd('\\');Installer.ValidateDirectory(installedRoot);Installer.CheckExistingDeployment();
                Uri api;if(!Uri.TryCreate(baseUrl.Text.Trim(),UriKind.Absolute,out api)||(api.Scheme!="http"&&api.Scheme!="https")||api.UserInfo.Length>0||api.Query.Length>0)throw new InvalidOperationException("模型 API 地址应为不带用户名、密钥或查询参数的接口基址。");
                if(String.IsNullOrWhiteSpace(model.Text))throw new InvalidOperationException("请填写模型名称。");
                if(!String.IsNullOrWhiteSpace(publicUrl.Text)){Uri value;if(!Uri.TryCreate(publicUrl.Text.Trim(),UriKind.Absolute,out value)||(value.Scheme!="http"&&value.Scheme!="https"))throw new InvalidOperationException("固定访问地址需要包含 http:// 或 https://。");}
                if(IPGlobalProperties.GetIPGlobalProperties().GetActiveTcpListeners().Any(item=>item.Port==(int)port.Value))throw new InvalidOperationException("所选端口已被使用，请选择其他端口。");
                WindowsIdentity user=WindowsIdentity.GetCurrent();plan=new Dictionary<string,object>{{"install_dir",installedRoot},{"port",(int)port.Value},{"allow_remote",remote.Checked},{"public_api_url",publicUrl.Text.Trim()},{"model_base_url",baseUrl.Text.Trim()},{"model_name",model.Text.Trim()},{"model_api_key",key.Text.Trim()},{"operator_sid",user.User.Value},{"operator_user",user.Name},{"installation_id",Guid.NewGuid().ToString("D")}};
            }
            catch(Exception error){MessageBox.Show(this,error.Message,Text,MessageBoxButtons.OK,MessageBoxIcon.Information);return;}
            busy=true;install.Enabled=false;cancel.Enabled=false;formPanel.Visible=false;progressPanel.Visible=true;
            try
            {
                Progress<string> progress=new Progress<string>(message=>{if(!IsDisposed)progressText.Text=message;});Dictionary<string,object> result=await Task.Run(()=>Installer.Install(plan,progress));
                tokenValue=File.ReadAllText(Convert.ToString(result["token_file"])).Trim();finishedToken.Text=tokenValue;finishedApi.Text=Convert.ToString(result["api"]);key.Clear();plan["model_api_key"]="";
                progressPanel.Visible=false;finishPanel.Visible=true;complete=true;install.Text="完成";cancel.Visible=false;
            }
            catch(Exception error){progressText.Text="安装未完成\r\n"+error.Message;bar.Visible=false;cancel.Text="关闭";cancel.Enabled=true;}
            finally{busy=false;install.Enabled=complete;}
        }
    }
    internal static class Program
    {
        [STAThread] static int Main(string[] args)
        {
            if(args.Length==2&&(args[0]=="--verify-package"||args[0]=="--extract-to"))
            {
                try
                {
                    Package package=new Package(Installer.OwnExecutable);package.Verify();
                    if(args[0]=="--verify-package")File.WriteAllText(args[1],new JavaScriptSerializer().Serialize(new{valid=true,payload_bytes=package.Length,sha256=BitConverter.ToString(package.Hash).Replace("-","").ToLowerInvariant()}));
                    else {string destination=System.IO.Path.GetFullPath(args[1]);if(Directory.Exists(destination)&&Directory.EnumerateFileSystemEntries(destination).Any())throw new InvalidOperationException("Extraction target must be empty");Installer.Protect(destination,false);package.Extract(System.IO.Path.Combine(destination,"app"),System.IO.Path.Combine(destination,"dependencies"),new Progress<string>());}
                    return 0;
                }
                catch(Exception error){if(args[0]=="--verify-package")File.WriteAllText(args[1],new JavaScriptSerializer().Serialize(new{valid=false,error=error.Message}));return 1;}
            }
            Application.EnableVisualStyles();Application.SetCompatibleTextRenderingDefault(false);Application.Run(new Wizard());return 0;
        }
    }
}





