using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Net;
using System.Linq;
using System.Threading;
using System.Threading.Tasks;
using System.Web.Script.Serialization;
using System.Windows.Forms;

internal sealed class Worker8787 : Form
{
    const string DesktopDir = @"C:\CodexRemoteWorker-AppServer-8790\desktop";
    const string DataDir = @"C:\CodexWorkerData-AppServer-8790";
    const string ReadyUrl = "http://127.0.0.1:8787/v1/ready?include_channels=true";

    readonly Label state = new Label(), details = new Label();
    readonly ListView channelList = new ListView();
    readonly NotifyIcon tray = new NotifyIcon();
    readonly System.Windows.Forms.Timer timer = new System.Windows.Forms.Timer();
    readonly EventWaitHandle activation;
    readonly bool startHidden;
    readonly Button startButton, refreshButton;
    RegisteredWaitHandle activationWatch;
    Task starting = Task.FromResult(0);
    bool checking, stopping, allowClose;
    int generation;

    static Dictionary<string, object> ReadJson(string path)
    {
        return new JavaScriptSerializer().Deserialize<Dictionary<string, object>>(File.ReadAllText(path));
    }

    static string Token()
    {
        return (string)ReadJson(Path.Combine(DataDir, @"private\credentials.local.json"))["CODEX_WORKER_SERVICE_TOKEN"];
    }

    sealed class Channel
    {
        public int execution_slot { get; set; }
        public string status { get; set; }
        public string task_id { get; set; }
        public string project_name { get; set; }
        public string recent_action { get; set; }
        public string current_file { get; set; }
        public int documents_completed { get; set; }
        public int documents_total { get; set; }
    }

    sealed class ServiceStatus
    {
        public bool ready { get; set; }
        public string service { get; set; }
        public int execution_slots { get; set; }
        public string[] document_types { get; set; }
        public Channel[] channels { get; set; }
    }

    static ServiceStatus Ready()
    {
        var request = (HttpWebRequest)WebRequest.Create(ReadyUrl);
        request.Proxy = null;
        request.AllowAutoRedirect = false;
        request.Timeout = request.ReadWriteTimeout = 3500;
        request.Headers["Authorization"] = "Bearer " + Token();
        using (var response = request.GetResponse())
        using (var reader = new StreamReader(response.GetResponseStream()))
        {
            var status = new JavaScriptSerializer().Deserialize<ServiceStatus>(reader.ReadToEnd());
            if (status == null || status.service != "materials" || !status.ready)
                throw new InvalidOperationException("8787 尚未返回材料服务的就绪状态。");
            return status;
        }
    }

    static void RunScript(string script, string action)
    {
        string shell = (string)ReadJson(Path.Combine(DesktopDir, "runtime.json"))["pwsh"];
        string path = Path.Combine(DesktopDir, script);
        var info = new ProcessStartInfo(shell, "-NoLogo -NoProfile -NonInteractive -File \"" + path + "\"") {
            WorkingDirectory = DesktopDir, UseShellExecute = false, CreateNoWindow = true
        };
        using (var process = Process.Start(info))
        {
            process.WaitForExit();
            if (process.ExitCode != 0)
                throw new InvalidOperationException(action + "失败，请检查材料服务日志与启动回执。");
        }
    }

    static string ErrorText(Exception error)
    {
        var web = error as WebException;
        if (web != null)
        {
            using (var response = web.Response as HttpWebResponse)
            {
                if (response != null) return "就绪接口返回 HTTP " + (int)response.StatusCode + "。";
            }
            if (web.Status == WebExceptionStatus.ConnectFailure) return "本机 8787 没有响应。";
        }
        return error.Message;
    }

    public Worker8787(EventWaitHandle showEvent, bool hidden)
    {
        activation = showEvent;
        startHidden = hidden;
        Text = "Codex Worker · 8787 项目材料";
        Font = new Font("Microsoft YaHei UI", 10);
        ClientSize = new Size(780, 538);
        StartPosition = FormStartPosition.CenterScreen;
        FormBorderStyle = FormBorderStyle.FixedSingle;
        MaximizeBox = false;

        Controls.Add(new Label { Text = "项目材料预处理 Worker 8787", AutoSize = true,
            Location = new Point(24, 18), Font = new Font(Font.FontFamily, 17, FontStyle.Bold) });
        state.SetBounds(24, 64, 732, 28);
        details.SetBounds(24, 94, 732, 29);
        Controls.Add(state);
        Controls.Add(details);

        channelList.SetBounds(24, 130, 732, 216);
        channelList.View = View.Details;
        channelList.FullRowSelect = true;
        channelList.MultiSelect = false;
        channelList.ShowItemToolTips = true;
        channelList.HeaderStyle = ColumnHeaderStyle.Nonclickable;
        channelList.AccessibleName = "全部执行通道";
        channelList.Columns.Add("通道", 52);
        channelList.Columns.Add("状态", 65);
        channelList.Columns.Add("项目", 154);
        channelList.Columns.Add("当前动作", 211);
        channelList.Columns.Add("当前文件", 153);
        channelList.Columns.Add("已转写 / 总数", 88);
        Controls.Add(channelList);

        string address = "http://127.0.0.1:8787";
        try { address = (string)ReadJson(Path.Combine(DesktopDir, "runtime.json"))["base_url"]; }
        catch (Exception) { } // The status panel reports installation errors on startup.
        Controls.Add(new Label { Text = "调用地址", AutoSize = true, Location = new Point(24, 366) });
        var url = new TextBox { Text = address, ReadOnly = true };
        url.SetBounds(24, 394, 454, 28);
        Controls.Add(url);
        ButtonAt("复制地址", 492, 391, 124, delegate { Clipboard.SetText(url.Text); });
        ButtonAt("复制 Token", 628, 391, 128, delegate { Clipboard.SetText(Token()); });
        startButton = ButtonAt("检查 / 启动服务", 24, 449, 146, StartWorker);
        refreshButton = ButtonAt("刷新状态", 182, 449, 128, RefreshStatus);
        ButtonAt("查看日志", 322, 449, 128, delegate { Process.Start("explorer.exe", DataDir); });
        ButtonAt("隐藏到托盘", 462, 449, 162, Hide);
        Controls.Add(new Label { Text = "关闭窗口或从托盘退出会停止 8787 服务。最小化可让服务在托盘继续运行。",
            Location = new Point(24, 500), Size = new Size(732, 32), ForeColor = Color.DimGray });

        var menu = new ContextMenuStrip();
        menu.Items.Add("打开控制台", null, delegate { ShowWindow(); });
        menu.Items.Add("刷新状态", null, delegate { RefreshStatus(); });
        menu.Items.Add(new ToolStripSeparator());
        menu.Items.Add("退出并停止服务", null, delegate { StopAndExit(); });
        tray.ContextMenuStrip = menu;
        Icon = System.Drawing.Icon.ExtractAssociatedIcon(typeof(Worker8787).Assembly.Location);
        tray.Icon = Icon;
        tray.DoubleClick += delegate { ShowWindow(); };
        SetState("正在检查", Color.DimGray, "");
        tray.Visible = true;
        timer.Interval = 5000;
        timer.Tick += delegate { RefreshStatus(); };
        Resize += delegate { if (WindowState == FormWindowState.Minimized) Hide(); };
    }

    Button ButtonAt(string text, int x, int y, int width, Action action)
    {
        var button = new Button { Text = text, Location = new Point(x, y), Size = new Size(width, 34) };
        button.Click += delegate {
            try { action(); }
            catch (Exception ex) { MessageBox.Show(this, ErrorText(ex), Text, MessageBoxButtons.OK, MessageBoxIcon.Error); }
        };
        Controls.Add(button);
        return button;
    }

    protected override void OnShown(EventArgs e)
    {
        base.OnShown(e);
        activationWatch = ThreadPool.RegisterWaitForSingleObject(activation, delegate {
            try { BeginInvoke((Action)delegate { ShowWindow(); StartWorker(); }); }
            catch (InvalidOperationException) { } // The form can close while a second launch is signalled.
        }, null, -1, false);
        if (startHidden) Hide();
        StartWorker();
        timer.Start();
    }

    void ShowWindow()
    {
        Show();
        WindowState = FormWindowState.Normal;
        Activate();
    }

    void SetState(string summary, Color color, string detail, bool channelsAvailable = false)
    {
        state.Text = "● " + summary;
        state.ForeColor = color;
        details.Text = detail;
        tray.Text = "材料 Worker 8787 · " + summary;
        if (!channelsAvailable) channelList.Items.Clear();
    }

    void DisplayReady(ServiceStatus status)
    {
        if (status.channels == null) {
            SetState("服务就绪", Color.ForestGreen, "执行位 " + status.execution_slots + "　·　当前服务版本未提供通道详情");
            return;
        }
        int running = status.channels.Count(c => c.status == "running");
        string usage = string.Format("运行中 {0} / {1}", running, status.execution_slots);
        SetState("服务就绪", Color.ForestGreen, string.Format("{0} 个通道　·　输入格式 {1}　·　上次检查 {2:HH:mm:ss}",
            usage, string.Join(" / ", status.document_types), DateTime.Now), true);
        tray.Text += " · " + usage;

        channelList.BeginUpdate();
        try {
            for (int i = 0; i < status.channels.Length; i++) {
                var channel = status.channels[i];
                bool busy = channel.status == "running";
                var cells = new[] { channel.execution_slot.ToString(), busy ? "运行中" : "空闲",
                    channel.project_name ?? "", channel.recent_action ?? "", channel.current_file ?? "",
                    busy ? string.Format("{0} / {1}", channel.documents_completed, channel.documents_total) : "" };
                ListViewItem row;
                if (i == channelList.Items.Count) {
                    row = channelList.Items.Add(new ListViewItem(cells));
                } else {
                    row = channelList.Items[i];
                    for (int col = 0; col < cells.Length; col++) row.SubItems[col].Text = cells[col];
                }
                row.ForeColor = busy ? Color.DarkGreen : SystemColors.GrayText;
                row.ToolTipText = busy ? string.Join("\n", new[] { channel.project_name, channel.task_id,
                    channel.recent_action, channel.current_file }.Where(value => !string.IsNullOrEmpty(value))) : "";
            }
            while (channelList.Items.Count > status.channels.Length) channelList.Items.RemoveAt(channelList.Items.Count - 1);
        } finally { channelList.EndUpdate(); }
    }

    async void RefreshStatus()
    {
        if (checking || stopping || !starting.IsCompleted) return;
        checking = true;
        int revision = generation;
        try {
            var status = await Task.Run((Func<ServiceStatus>)Ready);
            if (revision == generation && !IsDisposed) DisplayReady(status);
        }
        catch (Exception ex) {
            if (revision == generation && !IsDisposed) SetState("服务未就绪", Color.Firebrick, ErrorText(ex));
        }
        finally { checking = false; }
    }

    void StartWorker()
    {
        if (stopping || !starting.IsCompleted) return;
        generation++;
        starting = EnsureStarted();
    }

    async Task EnsureStarted()
    {
        startButton.Enabled = false;
        SetState("正在检查 / 启动", Color.DarkOrange, "");
        try {
            var status = await Task.Run(delegate {
                try { return Ready(); }
                catch (WebException ex) {
                    if (ex.Status != WebExceptionStatus.ConnectFailure) throw;
                }
                RunScript("start-materials.ps1", "启动");
                return Ready();
            });
            if (!stopping && !IsDisposed) DisplayReady(status);
        }
        catch (Exception ex) {
            if (!stopping && !IsDisposed) SetState("启动检查失败", Color.Firebrick, ErrorText(ex));
        }
        finally { if (!stopping && !IsDisposed) startButton.Enabled = true; }
    }

    async void StopAndExit()
    {
        if (stopping) return;
        stopping = true;
        generation++;
        timer.Stop();
        startButton.Enabled = refreshButton.Enabled = false;
        SetState("正在停止服务", Color.DarkOrange, "正在关闭此次材料服务进程。");
        try {
            await starting;
            await Task.Run(delegate { RunScript("stop-materials.ps1", "停止"); });
            allowClose = true;
            Close();
        }
        catch (Exception ex) {
            stopping = false;
            startButton.Enabled = refreshButton.Enabled = true;
            SetState("停止失败", Color.Firebrick, ErrorText(ex));
            ShowWindow();
            MessageBox.Show(this, details.Text, Text, MessageBoxButtons.OK, MessageBoxIcon.Error);
            timer.Start();
        }
    }

    protected override void OnFormClosing(FormClosingEventArgs e)
    {
        if (e.CloseReason == CloseReason.UserClosing && !allowClose) {
            e.Cancel = true;
            if (!stopping) BeginInvoke((Action)StopAndExit);
        }
        base.OnFormClosing(e);
    }

    protected override void Dispose(bool disposing)
    {
        if (disposing) {
            if (activationWatch != null) activationWatch.Unregister(null);
            timer.Dispose();
            tray.Visible = false;
            if (tray.ContextMenuStrip != null) tray.ContextMenuStrip.Dispose();
            tray.Dispose();
        }
        base.Dispose(disposing);
    }

    [STAThread]
    static void Main(string[] args)
    {
        bool first;
        using (var showEvent = new EventWaitHandle(false, EventResetMode.AutoReset, @"Local\CodexMaterials8787-Show"))
        using (var single = new Mutex(false, @"Local\CodexMaterials8787-Desktop", out first))
        {
            if (!first) { showEvent.Set(); return; }
            Application.EnableVisualStyles();
            Application.SetCompatibleTextRenderingDefault(false);
            Application.Run(new Worker8787(showEvent, Array.IndexOf(args, "--tray") >= 0));
        }
    }
}
