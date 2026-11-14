using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.Globalization;
using System.IO;
using System.Net;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using System.Web.Script.Serialization;
using System.Windows.Forms;
using Microsoft.Win32;

namespace CodeXWorker
{
    internal static class Backend
    {
        internal static string Root;
        internal static string PublicApi;
        internal static string Scope; internal static int ShowMessage;
        [DllImport("user32.dll", CharSet=CharSet.Unicode)] static extern uint RegisterWindowMessage(string name);
        static readonly JavaScriptSerializer Json = new JavaScriptSerializer();
        internal static void Initialize(string root)
        {
            if (String.IsNullOrWhiteSpace(root))
            {
                using (RegistryKey key = Registry.LocalMachine.OpenSubKey(@"SOFTWARE\CodexRemoteWorker"))
                    if (key != null) root = Convert.ToString(key.GetValue("InstallDir", ""));
            }
            if (String.IsNullOrWhiteSpace(root))
            {
                string candidate = Directory.GetParent(AppDomain.CurrentDomain.BaseDirectory.TrimEnd('\\')).FullName;
                root = File.Exists(Path.Combine(candidate, "config.local.json")) ? candidate : @"C:\CodexRemoteWorker";
            }
            Root = Path.GetFullPath(root).TrimEnd('\\');
            using (System.Security.Cryptography.SHA256 hash = System.Security.Cryptography.SHA256.Create()) Scope = BitConverter.ToString(hash.ComputeHash(Encoding.UTF8.GetBytes(Root.ToUpperInvariant()))).Replace("-", "").Substring(0,16);
            ShowMessage = (int)RegisterWindowMessage("CodeXWorkerShow-" + Scope);
            PublicApi = "http://127.0.0.1:" + Port;
        }
        internal static Dictionary<string, object> Read(string path)
        {
            try { using (FileStream file = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite | FileShare.Delete)) using (StreamReader reader = new StreamReader(file)) return new JavaScriptSerializer().Deserialize<Dictionary<string, object>>(reader.ReadToEnd()); }
            catch (IOException) { return null; } catch (UnauthorizedAccessException) { return null; } catch (ArgumentException) { return null; }
        }
        internal static string Text(Dictionary<string, object> value, string name)
        {
            if (value != null) foreach (KeyValuePair<string, object> item in value) if (String.Equals(item.Key, name, StringComparison.OrdinalIgnoreCase)) return Convert.ToString(item.Value, CultureInfo.InvariantCulture);
            return "";
        }
        internal static Dictionary<string, object> Settings { get { return Read(Path.Combine(Root, "desktop.settings.json")); } }
        internal static string DataRoot { get { string value = Text(Settings, "data_root"); if (String.IsNullOrEmpty(value)) value = Text(Read(Path.Combine(Root, "config.local.json")), "data_root"); return String.IsNullOrEmpty(value) ? Path.Combine(Root, "Data") : value; } }
        internal static int Port { get { int port; string value = Text(Settings, "port"); if (String.IsNullOrEmpty(value)) value = Text(Read(Path.Combine(Root, "config.local.json")), "port"); return Int32.TryParse(value, out port) ? port : 8787; } }
        internal static string StatusPath { get { return Path.Combine(Root, "desktop-status.json"); } }
        internal static string Token()
        {
            string path = Path.Combine(DataRoot, "private", "service-token.txt");
            if (File.Exists(path)) return File.ReadAllText(path).Trim();
            string token = Text(Read(Path.Combine(DataRoot, "private", "credentials.local.json")), "CODEX_WORKER_SERVICE_TOKEN");
            if (String.IsNullOrEmpty(token)) throw new InvalidOperationException("无法读取 Token，请使用安装服务的 Windows 用户打开启动器。");
            return token;
        }
        internal const string ConnectionFileName = "连接信息.txt";
        internal static string ConnectionText(string api)
        {
            return "token：" + Token() + "\r\napi：" + api + "\r\n";
        }
        internal static string SaveConnectionText(string content)
        {
            string path = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.DesktopDirectory), ConnectionFileName);
            File.WriteAllText(path, content, new UTF8Encoding(false));
            return path;
        }
        internal static string SaveConnectionFile(string api)
        {
            return SaveConnectionText(ConnectionText(api));
        }
        [DllImport("kernel32.dll", SetLastError = true)] static extern IntPtr OpenProcess(int access, bool inherit, int processId);
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)] static extern bool QueryFullProcessImageName(IntPtr process, int flags, StringBuilder path, ref int size);
        [DllImport("kernel32.dll", SetLastError = true)] static extern bool GetProcessTimes(IntPtr process, out long creation, out long exit, out long kernel, out long user);
        [DllImport("kernel32.dll")] static extern bool CloseHandle(IntPtr handle);
        internal static bool IsRunning()
        {
            Dictionary<string, object> status = Read(StatusPath);
            Dictionary<string, object> record = Read(Path.Combine(DataRoot, "worker.local.process.json"));
            int pid;
            string pidText = Text(record, "ProcessId"); if (String.IsNullOrEmpty(pidText)) pidText = Text(status, "process_id");
            if (!Int32.TryParse(pidText, out pid)) return false;
            IntPtr handle = OpenProcess(0x1000, false, pid); if (handle == IntPtr.Zero) return false;
            try
            {
                StringBuilder path = new StringBuilder(32768); int size = path.Capacity;
                if (!QueryFullProcessImageName(handle, 0, path, ref size)) return false;
                bool known = String.Equals(path.ToString(), Path.Combine(Root, @"bin\CodexRemoteWorker\CodexRemoteWorker.exe"), StringComparison.OrdinalIgnoreCase) || String.Equals(path.ToString(), Path.Combine(Root, @".runtime\python313\python.exe"), StringComparison.OrdinalIgnoreCase);
                if (!known) return false;
                string expectedTime = Text(record, "StartedAt"); if (String.IsNullOrEmpty(expectedTime)) expectedTime = Text(status, "started_at");
                DateTimeOffset time; long created, ended, kernel, user;
                if (!String.IsNullOrEmpty(expectedTime) && DateTimeOffset.TryParse(expectedTime, CultureInfo.InvariantCulture, DateTimeStyles.None, out time) && GetProcessTimes(handle, out created, out ended, out kernel, out user)) return ended == 0 && DateTime.FromFileTimeUtc(created).Ticks == time.UtcDateTime.Ticks;
                return String.Equals(Text(status, "state"), "ready", StringComparison.OrdinalIgnoreCase);
            }
            finally { CloseHandle(handle); }
        }
        internal static Dictionary<string, object> Readiness()
        {
            try
            {
                HttpWebRequest request = (HttpWebRequest)WebRequest.Create("http://127.0.0.1:" + Port + "/v1/ready");
                request.Proxy = null; request.Timeout = 15000; request.ReadWriteTimeout = 15000;
                request.Headers["Authorization"] = "Bearer " + Token();
                HttpWebResponse response;
                try { response = (HttpWebResponse)request.GetResponse(); }
                catch (WebException error) { response = error.Response as HttpWebResponse; if (response == null) throw; }
                using (response)
                using (StreamReader reader = new StreamReader(response.GetResponseStream()))
                {
                    Dictionary<string, object> result = new JavaScriptSerializer().Deserialize<Dictionary<string, object>>(reader.ReadToEnd());
                    if (result != null)
                    {
                        result["http_status"] = (int)response.StatusCode;
                        if (response.StatusCode != HttpStatusCode.OK) result["ready"] = false;
                        return result;
                    }
                }
            }
            catch (Exception) { }
            return new Dictionary<string, object> { { "ready", false }, { "code", "UNAVAILABLE" }, { "message", "无法完成就绪检查，请打开日志检查服务。" } };
        }
        internal static bool Ready(Dictionary<string, object> result)
        {
            if (!Text(result, "ready").Equals("True", StringComparison.OrdinalIgnoreCase) || Text(result, "http_status") != "200") return false;
            object value;
            if (result == null || !result.TryGetValue("input_contracts", out value)) return false;
            System.Collections.IEnumerable contracts = value as System.Collections.IEnumerable;
            if (contracts == null || value is string) return false;
            HashSet<string> supported = new HashSet<string>(StringComparer.Ordinal);
            foreach (object item in contracts) supported.Add(Convert.ToString(item, CultureInfo.InvariantCulture));
            return supported.Contains("siemens-json/2") && supported.Contains("siemens-json/1") && supported.Contains("table-csv/1");
        }
        static void Trigger(string key, string fallback)
        {
            string task = Text(Settings, key); if (String.IsNullOrEmpty(task)) task = fallback;
            if (task.IndexOf('"') >= 0 || task.IndexOf('\r') >= 0 || task.IndexOf('\n') >= 0) throw new InvalidOperationException("启动任务配置无效。");
            ProcessStartInfo info = new ProcessStartInfo(Path.Combine(Environment.SystemDirectory, "schtasks.exe"), "/Run /TN \"" + task + "\"") { UseShellExecute = false, CreateNoWindow = true };
            using (Process process = Process.Start(info)) if (process.WaitForExit(500) && process.ExitCode != 0) throw new InvalidOperationException("无法运行服务控制任务，请检查安装状态。");
        }
        internal static void Start(IProgress<string> progress)
        {
            Control(false, progress);
        }
        internal static void Repair(IProgress<string> progress)
        {
            Control(true, progress);
        }
        static void Control(bool repair, IProgress<string> progress)
        {
            if (!File.Exists(Path.Combine(Root, "scripts", "desktop-ensure.ps1"))) throw new InvalidOperationException("缺少自动启动检查脚本，请更新安装文件。");
            using (Mutex control = new Mutex(false, @"Local\CodeXWorkerControl-" + Scope))
            {
                bool acquired = false;
                try
                {
                    progress.Report("正在串行检查服务和真实沙箱，请稍候…");
                    try { acquired = control.WaitOne(TimeSpan.FromMinutes(5)); } catch (AbandonedMutexException) { acquired = true; }
                    if (!acquired) throw new InvalidOperationException("已有服务控制操作正在执行，请稍候。");
                    string previousId = Text(Read(StatusPath), "operation_id");
                    string operation = repair ? "repair" : "ensure";
                    Trigger(repair ? "repair_task" : "start_task", repair ? "CodexRemoteWorker-RepairSandbox" : "CodexRemoteWorker-DesktopStart");
                    DateTime deadline = DateTime.UtcNow.AddMinutes(5);
                    while (DateTime.UtcNow < deadline)
                    {
                        Dictionary<string, object> status = Read(StatusPath);
                        string id = Text(status, "operation_id");
                        bool fresh = !String.IsNullOrEmpty(id) && id != previousId && Text(status, "operation") == operation;
                        if (fresh && Text(status, "state") == "ready" && IsRunning() && Ready(Readiness())) return;
                        if (fresh && Text(status, "state") == "error") throw new InvalidOperationException(Text(status, "message"));
                        if (fresh) progress.Report(Text(status, "message"));
                        Thread.Sleep(700);
                    }
                    throw new InvalidOperationException("自动启动检查超时，请查看 desktop-ensure.log。");
                }
                finally { if (acquired) control.ReleaseMutex(); }
            }
        }
        internal static void Stop()
        {
            if (!IsRunning() && Text(Read(StatusPath), "state") != "starting") return;
            DateTimeOffset requested = DateTimeOffset.UtcNow.AddSeconds(-3);
            Trigger("stop_task", "CodexRemoteWorker-DesktopStop");
            DateTime deadline = DateTime.UtcNow.AddSeconds(90);
            while (DateTime.UtcNow < deadline) {
                Dictionary<string, object> status = Read(StatusPath); DateTimeOffset updated;
                if (!IsRunning() && Text(status,"state") == "stopped" && DateTimeOffset.TryParse(Text(status,"updated_at"),out updated) && updated >= requested) return;
                Thread.Sleep(500);
            }
            throw new InvalidOperationException("服务尚未停止，启动器已保留。请查看日志后重试退出。");
        }
        static string Metadata(string path, string token)
        {
            HttpWebRequest request = (HttpWebRequest)WebRequest.Create("http://169.254.169.254/latest/" + path);
            request.Proxy = null; request.Timeout = 1500; request.ReadWriteTimeout = 1500;
            if (token == null) { request.Method = "PUT"; request.Headers["X-aws-ec2-metadata-token-ttl-seconds"] = "60"; request.ContentLength = 0; }
            else request.Headers["X-aws-ec2-metadata-token"] = token;
            using (WebResponse response = request.GetResponse()) using (StreamReader reader = new StreamReader(response.GetResponseStream())) return reader.ReadToEnd().Trim();
        }
        internal static string ResolveAddress()
        {
            if (Text(Settings,"allow_remote").Equals("False",StringComparison.OrdinalIgnoreCase)) return "http://127.0.0.1:" + Port; string fixedAddress = Text(Settings, "public_api_url"); if (!String.IsNullOrWhiteSpace(fixedAddress)) return fixedAddress.TrimEnd('/');
            try { string token = Metadata("api/token", null); string address = Metadata("meta-data/public-ipv4", token); IPAddress parsed; if (IPAddress.TryParse(address, out parsed)) return "http://" + parsed + ":" + Port; } catch (WebException) { } catch (IOException) { }
            Dictionary<string, object> status = Read(StatusPath); string stored = Text(status, "public_api");
            if (!String.IsNullOrEmpty(stored)) return stored;
            foreach (IPAddress address in Dns.GetHostAddresses(Environment.MachineName)) if (address.AddressFamily == System.Net.Sockets.AddressFamily.InterNetwork && !IPAddress.IsLoopback(address)) return "http://" + address + ":" + Port;
            return "http://127.0.0.1:" + Port;
        }
    }
    internal sealed class ConnectionWindow : Form
    {
        internal ConnectionWindow(string api, string token)
        {
            string details = "token：" + token + "\r\napi：" + api + "\r\n";
            Text = "服务连接信息"; ClientSize = new Size(620, 270); StartPosition = FormStartPosition.CenterParent; FormBorderStyle = FormBorderStyle.FixedDialog; MaximizeBox = false; Font = new Font("Microsoft YaHei UI", 10);
            Label apiLabel = new Label { Text = "API 地址", Location = new Point(20, 18), AutoSize = true };
            TextBox apiValue = new TextBox { Text = api, ReadOnly = true, Location = new Point(20, 46), Size = new Size(580, 28) };
            Label tokenLabel = new Label { Text = "访问 Token", Location = new Point(20, 84), AutoSize = true };
            TextBox tokenValue = new TextBox { Text = token, ReadOnly = true, UseSystemPasswordChar = true, Location = new Point(20, 112), Size = new Size(580, 28) };
            CheckBox show = new CheckBox { Text = "显示 Token", Location = new Point(20, 151), AutoSize = true }; show.CheckedChanged += delegate { tokenValue.UseSystemPasswordChar = !show.Checked; };
            Button copy = new Button { Text = "复制连接信息", Location = new Point(20, 202), Size = new Size(150, 34) }; copy.Click += delegate { Clipboard.SetText(details); };
            Button save = new Button { Text = "保存到桌面", Location = new Point(180, 202), Size = new Size(150, 34) }; save.Click += delegate { try { string path = Backend.SaveConnectionText(details); MessageBox.Show(this, "已保存到：\r\n" + path, Text, MessageBoxButtons.OK, MessageBoxIcon.Information); } catch (Exception error) { MessageBox.Show(this, error.Message, Text, MessageBoxButtons.OK, MessageBoxIcon.Information); } };
            Button close = new Button { Text = "关闭", Location = new Point(480, 202), Size = new Size(120, 34) }; close.Click += delegate { Close(); };
            Controls.AddRange(new Control[] { apiLabel, apiValue, tokenLabel, tokenValue, show, copy, save, close });
        }
    }
    internal sealed class Launcher : Form
    {
        Label headline, detail; TextBox address; ProgressBar progress; Button start, repair;
        NotifyIcon tray; ToolStripMenuItem statusItem; System.Windows.Forms.Timer stateTimer, addressTimer;
        bool busy, quitting, exiting, firstHide = true, resolving, checking;
        int automaticAttempts;
        DateTime nextAutomaticAttempt = DateTime.MinValue;
        internal Launcher()
        {
            Text = "CodeX 服务启动器"; ClientSize = new Size(600, 320); StartPosition = FormStartPosition.CenterScreen; FormBorderStyle = FormBorderStyle.FixedDialog; MaximizeBox = false; Font = new Font("Microsoft YaHei UI", 10); BackColor = Color.FromArgb(248,250,252); Icon = SystemIcons.Application;
            headline = new Label { Text = "正在检查服务", Location = new Point(28,24), Size = new Size(544,38), Font = new Font(Font.FontFamily,19,FontStyle.Bold) };
            detail = new Label { Text = "正在读取服务状态…", Location = new Point(30,77), Size = new Size(540,46), ForeColor = Color.FromArgb(71,85,105) };
            progress = new ProgressBar { Location = new Point(30,128), Size = new Size(540,8), Style = ProgressBarStyle.Marquee };
            Label api = new Label { Text = "API 地址", Location = new Point(30,157), AutoSize = true };
            address = new TextBox { Text = Backend.PublicApi, ReadOnly = true, Location = new Point(114,150), Size = new Size(456,28) };
            Label note = new Label { Text = "关闭窗口后保留托盘图标；右键“退出并停止服务”可关闭全部服务。", Location = new Point(30,194), Size = new Size(540,44), ForeColor = Color.FromArgb(100,116,139) };
            start = new Button { Text = "启动 / 刷新", Location = new Point(30,264), Size = new Size(125,34) }; start.Click += delegate { StartService(); };
            Button connection = new Button { Text = "连接信息", Location = new Point(168,264), Size = new Size(125,34) }; connection.Click += delegate { ShowConnection(); };
            repair = new Button { Text = "修复沙箱", Location = new Point(306,264), Size = new Size(115,34) }; repair.Click += delegate { RepairService(); };
            Button hide = new Button { Text = "隐藏到托盘", Location = new Point(435,264), Size = new Size(135,34) }; hide.Click += delegate { HideToTray(); };
            Label version = new Label { Text = "TM 项目归档版 · 0.4.0", Location = new Point(30,238), AutoSize = true, ForeColor = Color.FromArgb(100,116,139), Font = new Font(Font.FontFamily,8) };
            Controls.AddRange(new Control[] { headline,detail,progress,api,address,note,start,connection,repair,hide,version });
            ContextMenuStrip menu = new ContextMenuStrip(); statusItem = new ToolStripMenuItem("状态：正在检查") { Enabled = false }; menu.Items.Add(statusItem);
            menu.Items.Add("打开窗口", null, delegate { ShowMain(); }); menu.Items.Add("查看连接信息", null, delegate { ShowConnection(); }); menu.Items.Add("保存连接信息到桌面", null, delegate { SaveConnectionToDesktop(true); });
            menu.Items.Add("启动 / 刷新", null, delegate { StartService(); }); menu.Items.Add("打开日志", null, delegate { Process.Start(new ProcessStartInfo("explorer.exe", "\"" + Backend.DataRoot + "\"") { UseShellExecute = true }); });
            menu.Items.Add("修复沙箱", null, delegate { RepairService(); });
            menu.Items.Add(new ToolStripSeparator()); menu.Items.Add("退出并停止服务", null, delegate { ExitService(); });
            ContextMenuStrip = menu;
            tray = new NotifyIcon { Icon = SystemIcons.Application, Text = "CodeX 服务", ContextMenuStrip = menu, Visible = true }; tray.DoubleClick += delegate { ShowMain(); };
            FormClosing += delegate(object sender, FormClosingEventArgs e) { if (e.CloseReason == CloseReason.WindowsShutDown) { quitting = true; tray.Visible = false; return; } if (!quitting) { e.Cancel = true; HideToTray(); } };
            stateTimer = new System.Windows.Forms.Timer { Interval = 10000 }; stateTimer.Tick += delegate { if (!busy) UpdateState(); }; stateTimer.Start();
            addressTimer = new System.Windows.Forms.Timer { Interval = 60000 }; addressTimer.Tick += delegate { RefreshAddress(); }; addressTimer.Start();
            KeyPreview = true; KeyDown += delegate(object sender, KeyEventArgs e) { if(e.Control && e.Shift && e.KeyCode == Keys.Q) { e.Handled = true; ExitService(); } };
            Shown += delegate { StartService(); RefreshAddress(); };
        }
        [DllImport("user32.dll")] static extern bool ChangeWindowMessageFilterEx(IntPtr window,int message,int action,IntPtr changeInfo);
        protected override void OnHandleCreated(EventArgs e) { base.OnHandleCreated(e); ChangeWindowMessageFilterEx(Handle,Backend.ShowMessage,1,IntPtr.Zero); }
        protected override void WndProc(ref Message message) { if(message.Msg==Backend.ShowMessage) { ShowMain(); StartService(); return; } base.WndProc(ref message); }
        void ShowMain() { Show(); WindowState = FormWindowState.Normal; Activate(); }
        void HideToTray() { Hide(); if (firstHide) { firstHide = false; tray.ShowBalloonTip(2500, "CodeX 服务", "服务继续运行，可从右下角托盘查看。", ToolTipIcon.Info); } }
        async void UpdateState()
        {
            if (checking) return; checking = true;
            try
            {
                bool running = Backend.IsRunning();
                Dictionary<string, object> ready = running ? await Task.Run(() => Backend.Readiness()) : null;
                if (IsDisposed || busy) return;
                bool healthy = running && Backend.Ready(ready);
                string state = healthy ? "服务已就绪" : (running ? "沙箱 / 服务未就绪" : "服务已停止");
                headline.Text = state;
                headline.ForeColor = healthy ? Color.FromArgb(21,128,61) : (running ? Color.Firebrick : Color.FromArgb(100,116,139));
                detail.Text = healthy ? "已实时验证执行环境，可以接收任务。" : (running ? Backend.Text(ready, "message") + "\r\n" + Backend.Text(ready, "code") : "点击“启动 / 刷新”可启动服务。");
                progress.Visible = false; statusItem.Text = "状态：" + state; tray.Text = "CodeX - " + state; tray.Icon = healthy ? SystemIcons.Information : (running ? SystemIcons.Warning : SystemIcons.Application);
                if (healthy) { automaticAttempts = 0; nextAutomaticAttempt = DateTime.MinValue; }
                else if (running && Backend.Text(ready, "code") == "SANDBOX_STATE_CHANGED" && automaticAttempts < 3 && DateTime.UtcNow >= nextAutomaticAttempt)
                {
                    automaticAttempts++; nextAutomaticAttempt = DateTime.UtcNow.AddMinutes(1);
                    StartService(true);
                }
            }
            finally { checking = false; }
        }
        async void RefreshAddress()
        {
            if (resolving) return; resolving = true;
            try
            {
                string value = await Task.Run(() => Backend.ResolveAddress());
                if (!IsDisposed) { Backend.PublicApi = value; address.Text = value; }
                try { await Task.Run(() => Backend.SaveConnectionFile(value)); } catch { }
            }
            catch { }
            finally { resolving = false; }
        }
        async void StartService(bool automatic = false)
        {
            if (busy) return; busy = true; start.Enabled = false; repair.Enabled = false; progress.Visible = true; headline.Text = "正在启动服务"; statusItem.Text = "状态：启动中";
            if (!automatic) { automaticAttempts = 0; nextAutomaticAttempt = DateTime.MinValue; }
            try { Progress<string> updates = new Progress<string>(value => { if (!IsDisposed) detail.Text = value; }); await Task.Run(() => Backend.Start(updates)); busy = false; UpdateState(); RefreshAddress(); }
            catch (Exception error) { headline.Text = "启动未完成"; headline.ForeColor = Color.Firebrick; detail.Text = error.Message; statusItem.Text = "状态：启动未完成"; ShowMain(); }
            finally { busy = false; start.Enabled = true; repair.Enabled = true; progress.Visible = false; }
        }
        async void RepairService()
        {
            if (busy) return;
            busy = true; start.Enabled = false; repair.Enabled = false; progress.Visible = true;
            headline.Text = "正在修复沙箱"; detail.Text = "先检查是否有活动任务，再验证并重启服务。";
            statusItem.Text = "状态：修复中"; ShowMain();
            try
            {
                Progress<string> updates = new Progress<string>(value => { if (!IsDisposed) detail.Text = value; });
                await Task.Run(() => Backend.Repair(updates));
                busy = false; UpdateState(); RefreshAddress();
            }
            catch (Exception error) { headline.Text = "修复未完成"; headline.ForeColor = Color.Firebrick; detail.Text = error.Message; statusItem.Text = "状态：修复未完成"; }
            finally { busy = false; start.Enabled = true; repair.Enabled = true; progress.Visible = false; }
        }
        void ShowConnection()
        {
            try
            {
                string api = address.Text;
                if (String.IsNullOrWhiteSpace(api)) api = Backend.ResolveAddress();
                using (ConnectionWindow dialog = new ConnectionWindow(api, Backend.Token())) dialog.ShowDialog(this);
            }
            catch (Exception error) { MessageBox.Show(this,error.Message,Text,MessageBoxButtons.OK,MessageBoxIcon.Information); }
        }
        void SaveConnectionToDesktop(bool notify)
        {
            try
            {
                string api = address.Text;
                if (String.IsNullOrWhiteSpace(api)) api = Backend.ResolveAddress();
                string path = Backend.SaveConnectionFile(api);
                if (notify) MessageBox.Show(this, "已保存到：\r\n" + path, Text, MessageBoxButtons.OK, MessageBoxIcon.Information);
            }
            catch (Exception error) { if (notify) MessageBox.Show(this,error.Message,Text,MessageBoxButtons.OK,MessageBoxIcon.Information); }
        }
        async void ExitService()
        {
            if (exiting || busy) return; exiting = true; busy = true; start.Enabled = false; repair.Enabled = false; headline.Text = "正在停止服务"; detail.Text = "正在停止任务并退出，请稍候…"; statusItem.Text = "状态：正在停止"; ShowMain();
            try { await Task.Run(() => Backend.Stop()); quitting = true; stateTimer.Stop(); addressTimer.Stop(); tray.Visible = false; tray.Dispose(); Application.Exit(); }
            catch (Exception error) { exiting = false; busy = false; start.Enabled = true; repair.Enabled = true; detail.Text = error.Message; headline.Text = "尚未退出"; ShowMain(); }
        }
    }
    internal static class Program
    {
        [DllImport("user32.dll", CharSet=CharSet.Unicode)] static extern IntPtr FindWindow(string className,string title);
        [DllImport("user32.dll")] static extern bool ShowWindow(IntPtr window,int command);
        [DllImport("user32.dll")] static extern bool SetForegroundWindow(IntPtr window);
        [DllImport("user32.dll")] static extern bool PostMessage(IntPtr window,int message,IntPtr first,IntPtr second);
        [STAThread] static int Main(string[] args)
        {
            string root = null, report = null, ensureReport = null;
            for (int index=0; index<args.Length; index++) { if (args[index]=="--root" && index+1<args.Length) root=args[++index]; else if(args[index]=="--probe-to" && index+1<args.Length) report=args[++index]; else if(args[index]=="--ensure-to" && index+1<args.Length) ensureReport=args[++index]; }
            Backend.Initialize(root);
            if (ensureReport != null)
            {
                try { Backend.Start(new Progress<string>()); File.WriteAllText(ensureReport, new JavaScriptSerializer().Serialize(Backend.Read(Backend.StatusPath))); return 0; }
                catch (Exception error) { File.WriteAllText(ensureReport, new JavaScriptSerializer().Serialize(new {state="error",message=error.Message})); return 1; }
            }
            if(report!=null) { Dictionary<string, object> ready = Backend.Readiness(); File.WriteAllText(report,new JavaScriptSerializer().Serialize(new { running=Backend.IsRunning(),ready=Backend.Ready(ready),code=Backend.Text(ready,"code"),message=Backend.Text(ready,"message"),root=Backend.Root,port=Backend.Port,api=Backend.ResolveAddress() })); return Backend.Ready(ready) ? 0 : 1; }
            bool created; string mutexName = @"Local\CodeXWorkerTray-" + Backend.Scope;
            using(Mutex mutex=new Mutex(true,mutexName,out created)) { if(!created) { PostMessage(new IntPtr(0xffff),Backend.ShowMessage,IntPtr.Zero,IntPtr.Zero); return 0; } Application.EnableVisualStyles(); Application.SetCompatibleTextRenderingDefault(false); Application.Run(new Launcher()); }
            return 0;
        }
    }
}



