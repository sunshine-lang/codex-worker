using System;
using System.Diagnostics;
using System.IO;
using System.Security.AccessControl;
using System.Security.Principal;
using System.Windows.Forms;
using Microsoft.Win32;
namespace CodeXMaintenance
{
    static class Program
    {
        static string Quote(string value){if(value.Contains("\"")||value.Contains("\r")||value.Contains("\n"))throw new ArgumentException();return "\""+value.TrimEnd('\\')+"\"";}
        [STAThread] static int Main(string[] args)
        {
            Application.EnableVisualStyles();string root=null;bool temporary=false;
            for(int i=0;i<args.Length;i++){if(args[i]=="--root"&&i+1<args.Length)root=args[++i];else if(args[i]=="--temporary")temporary=true;}
            try
            {
                if(root==null){using(RegistryKey key=Registry.CurrentUser.OpenSubKey(@"SOFTWARE\CodexRemoteWorker\Desktop"))if(key!=null)root=Convert.ToString(key.GetValue("InstallDir",""));}
                if(String.IsNullOrWhiteSpace(root))throw new InvalidOperationException("未找到安装目录。");root=Path.GetFullPath(root).TrimEnd('\\');
                if(!temporary)
                {
                    string stage=Path.Combine(Path.GetTempPath(),"CodeXUninstall-"+Guid.NewGuid().ToString("N"));Directory.CreateDirectory(stage);DirectorySecurity acl=new DirectorySecurity();acl.SetAccessRuleProtection(true,false);foreach(string sid in new[]{WindowsIdentity.GetCurrent().User.Value,"S-1-5-32-544","S-1-5-18"})acl.AddAccessRule(new FileSystemAccessRule(new SecurityIdentifier(sid),FileSystemRights.FullControl,InheritanceFlags.ContainerInherit|InheritanceFlags.ObjectInherit,PropagationFlags.None,AccessControlType.Allow));new DirectoryInfo(stage).SetAccessControl(acl);
                    string exe=Path.Combine(stage,"Uninstall.exe");File.Copy(Process.GetCurrentProcess().MainModule.FileName,exe);File.Copy(Path.Combine(root,@"installer\uninstall.ps1"),Path.Combine(stage,"uninstall.ps1"));Process.Start(new ProcessStartInfo(exe,"--temporary --root "+Quote(root)){UseShellExecute=false,WorkingDirectory=stage});return 0;
                }
                if(MessageBox.Show("卸载将停止 CodeX 服务。历史任务和数据将保留。是否继续？","卸载 CodeX 任务服务",MessageBoxButtons.YesNo,MessageBoxIcon.Question)!=DialogResult.Yes)return 0;
                string script=Path.Combine(AppDomain.CurrentDomain.BaseDirectory,"uninstall.ps1");string windowsPowerShell=Path.Combine(Environment.SystemDirectory,@"WindowsPowerShell\v1.0\powershell.exe");
                using(Process process=Process.Start(new ProcessStartInfo(windowsPowerShell,"-NoLogo -NoProfile -NonInteractive -File "+Quote(script)+" -InstallRoot "+Quote(root)){UseShellExecute=false,CreateNoWindow=true})) {process.WaitForExit();if(process.ExitCode!=0)throw new InvalidOperationException("卸载未完成，请检查服务是否已停止。");}
                MessageBox.Show("已卸载程序，Data 目录已保留。","CodeX 任务服务",MessageBoxButtons.OK,MessageBoxIcon.Information);return 0;
            }
            catch(Exception error){MessageBox.Show(error.Message,"CodeX 任务服务",MessageBoxButtons.OK,MessageBoxIcon.Error);return 1;}
        }
    }
}
