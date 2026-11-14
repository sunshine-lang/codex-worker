using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.Globalization;
using System.IO;
using System.Net;
using System.Runtime.InteropServices;
using System.Threading;
using System.Threading.Tasks;
using System.Web.Script.Serialization;
using System.Windows.Forms;

namespace CodexRemoteWorkerDesktop
{
    static class Backend
    {
        internal const string Root = @"C:\CodexRemoteWorker";
        internal const string TaskName = "CodexRemoteWorker-DesktopStart";
        internal const string WindowTitle = "Codex 服务启动器";
        internal static readonly string StatusPath = Path.Combine(Root, "desktop-status.json");
        internal static Dictionary<string, object> ReadStatus()
        {
            try { return new JavaScriptSerializer().Deserialize<Dictionary<string, object>>(File.ReadAllText(StatusPath)); }
            catch (IOException) { return null; }
            catch (UnauthorizedAccessException) { return null; }
            catch (ArgumentException) { return null; }
        }
        internal static string Text(Dictionary<string, object> status, string key)
        {
            object value;
            return status != null && status.TryGetValue(key, out value) && value != null ? value.ToString() : "";
        }
        static bool HealthIsAlive()
        {
            try
            {
                HttpWebRequest request = (HttpWebRequest)WebRequest.Create("http://127.0.0.1:8787/healthz");
                request.Proxy = null;
                request.Timeout = 1500;
                request.ReadWriteTimeout = 1500;
                using (HttpWebResponse response = (HttpWebResponse)request.GetResponse())
                using (StreamReader reader = new StreamReader(response.GetResponseStream()))
                    return response.StatusCode == HttpStatusCode.OK && reader.ReadToEnd().Contains("alive");
            }
            catch (WebException) { return false; }
        }
        internal static Dictionary<string, object> EnsureStarted(IProgress<string> progress)
        {
            Dictionary<string, object> existingStatus = ReadStatus();
            if (Text(existingStatus, "state") == "ready")
            {
                int existingId;
                if (Int32.TryParse(Text(existingStatus, "process_id"), out existingId))
                {
                    try
                    {
                        using (Process existing = Process.GetProcessById(existingId))
                        {
                            string path = existing.MainModule.FileName;
                            bool knownWorker = String.Equals(path, Path.Combine(Root, @"bin\CodexRemoteWorker\CodexRemoteWorker.exe"), StringComparison.OrdinalIgnoreCase) || String.Equals(path, Path.Combine(Root, @".runtime\python313\python.exe"), StringComparison.OrdinalIgnoreCase);
                            if (!existing.HasExited && knownWorker) return existingStatus;
                        }
                    }
                    catch (ArgumentException) { }
                    catch (System.ComponentModel.Win32Exception) { }
                    catch (InvalidOperationException) { }
                }
            }
            DateTimeOffset requestedAt = DateTimeOffset.UtcNow.AddSeconds(-3);
            if (!File.Exists(Path.Combine(Root, "scripts", "desktop-start.ps1")))
                throw new InvalidOperationException("未找到服务程序，请检查安装目录。");
            progress.Report("正在启动并检查服务，请稍候…");
            ProcessStartInfo start = new ProcessStartInfo(Path.Combine(Environment.SystemDirectory, "schtasks.exe"), "/Run /TN \"" + TaskName + "\"");
            start.UseShellExecute = false;
            start.CreateNoWindow = true;
            start.RedirectStandardOutput = false;
            start.RedirectStandardError = false;
            using (Process process = Process.Start(start))
            {
                if (process.WaitForExit(1000) && process.ExitCode != 0)
                    throw new InvalidOperationException("启动任务未能运行，请打开日志检查安装状态。");
            }
            DateTime deadline = DateTime.UtcNow.AddSeconds(300);
            while (DateTime.UtcNow < deadline)
            {
                Dictionary<string, object> status = ReadStatus();
                DateTimeOffset updatedAt;
                bool fresh = DateTimeOffset.TryParse(Text(status, "updated_at"), CultureInfo.InvariantCulture, DateTimeStyles.None, out updatedAt) && updatedAt >= requestedAt;
                if (fresh && Text(status, "state") == "ready" && HealthIsAlive()) return status;
                if (fresh && Text(status, "state") == "error")
                    throw new InvalidOperationException(Text(status, "message"));
                if (fresh && Text(status, "state") == "starting") progress.Report("正在自检和启动服务，请稍候…");
                Thread.Sleep(700);
            }
            throw new InvalidOperationException("启动检查超时，请打开日志查看结果。");
        }
    }

    sealed class LauncherForm : Form
    {
        Label headline;
        Label detail;
        TextBox apiAddress;
        ProgressBar progressBar;
        Button retryButton;
        bool busy;
        public LauncherForm()
        {
            Text = Backend.WindowTitle;
            ClientSize = new Size(586, 294);
            StartPosition = FormStartPosition.CenterScreen;
            FormBorderStyle = FormBorderStyle.FixedDialog;
            MaximizeBox = false;
            BackColor = Color.FromArgb(248, 250, 252);
            Font = new Font("Microsoft YaHei UI", 10F);
            AutoScaleMode = AutoScaleMode.Dpi;
            Icon = SystemIcons.Application;
            headline = new Label { Text = "正在启动服务", Location = new Point(28, 25), Size = new Size(530, 35), Font = new Font(Font.FontFamily, 19F, FontStyle.Bold), ForeColor = Color.FromArgb(30, 64, 175) };
            detail = new Label { Text = "正在检查服务状态，请稍候…", Location = new Point(30, 74), Size = new Size(526, 48), ForeColor = Color.FromArgb(71, 85, 105) };
            progressBar = new ProgressBar { Location = new Point(30, 128), Size = new Size(526, 8), Style = ProgressBarStyle.Marquee, MarqueeAnimationSpeed = 22 };
            Label apiLabel = new Label { Text = "API 地址", Location = new Point(30, 153), Size = new Size(82, 26) };
            apiAddress = new TextBox { Text = "启动后显示", Location = new Point(113, 149), Size = new Size(443, 29), ReadOnly = true, BorderStyle = BorderStyle.FixedSingle, BackColor = Color.White };
            Label note = new Label { Text = "关闭此窗口后，服务会继续在后台运行。", Location = new Point(30, 190), Size = new Size(526, 25), ForeColor = Color.FromArgb(100, 116, 139) };
            retryButton = new Button { Text = "重新检查", Location = new Point(30, 239), Size = new Size(112, 34), Enabled = false };
            Button logsButton = new Button { Text = "打开日志", Location = new Point(154, 239), Size = new Size(112, 34) };
            Button closeButton = new Button { Text = "关闭窗口", Location = new Point(444, 239), Size = new Size(112, 34) };
            retryButton.Click += delegate { StartOrCheck(); };
            logsButton.Click += delegate { try { Process.Start(new ProcessStartInfo("explorer.exe", @"C:\CodexWorkerData") { UseShellExecute = true }); } catch { MessageBox.Show(this, "请打开 C:\\CodexWorkerData 查看日志。", Backend.WindowTitle); } };
            closeButton.Click += delegate { Close(); };
            Controls.AddRange(new Control[] { headline, detail, progressBar, apiLabel, apiAddress, note, retryButton, logsButton, closeButton });
            Shown += delegate { StartOrCheck(); };
        }
        async void StartOrCheck()
        {
            if (busy) return;
            busy = true;
            retryButton.Enabled = false;
            headline.Text = "正在启动服务";
            headline.ForeColor = Color.FromArgb(30, 64, 175);
            progressBar.Visible = true;
            try
            {
                Progress<string> progress = new Progress<string>(message => { if (!IsDisposed) detail.Text = message; });
                Dictionary<string, object> status = await Task.Run(() => Backend.EnsureStarted(progress));
                if (IsDisposed) return;
                headline.Text = "服务运行中";
                headline.ForeColor = Color.FromArgb(21, 128, 61);
                detail.Text = "服务已在后台运行。\n现在可以关闭此窗口。";
                apiAddress.Text = Backend.Text(status, "public_api");
                retryButton.Text = "重新检查";
            }
            catch (Exception error)
            {
                if (IsDisposed) return;
                headline.Text = "启动未完成";
                headline.ForeColor = Color.FromArgb(185, 28, 28);
                detail.Text = error.Message;
                retryButton.Text = "重试启动";
            }
            finally
            {
                busy = false;
                if (!IsDisposed) { retryButton.Enabled = true; progressBar.Visible = false; }
            }
        }
    }

    static class Program
    {
        [DllImport("user32.dll", CharSet = CharSet.Unicode)] static extern IntPtr FindWindow(string className, string windowName);
        [DllImport("user32.dll")] static extern bool SetForegroundWindow(IntPtr window);
        [DllImport("user32.dll")] static extern bool ShowWindow(IntPtr window, int command);
        [STAThread]
        static int Main(string[] args)
        {
            if (args.Length == 2 && args[0] == "--check-to")
            {
                try
                {
                    Dictionary<string, object> status = Backend.EnsureStarted(new Progress<string>());
                    File.WriteAllText(args[1], new JavaScriptSerializer().Serialize(status));
                    return 0;
                }
                catch (Exception error) { File.WriteAllText(args[1], new JavaScriptSerializer().Serialize(new { state = "error", message = error.Message })); return 1; }
            }
            bool created;
            using (Mutex mutex = new Mutex(true, @"Local\CodexRemoteWorkerDesktopLauncher", out created))
            {
                if (!created)
                {
                    IntPtr existing = FindWindow(null, Backend.WindowTitle);
                    if (existing != IntPtr.Zero) { ShowWindow(existing, 9); SetForegroundWindow(existing); }
                    return 0;
                }
                Application.EnableVisualStyles();
                Application.SetCompatibleTextRenderingDefault(false);
                Application.Run(new LauncherForm());
            }
            return 0;
        }
    }
}





