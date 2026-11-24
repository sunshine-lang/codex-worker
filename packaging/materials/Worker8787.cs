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
    const string ReadyUrl = "http://127.0.0.1:8787/v1/ready";

    readonly Label state = new Label(), details = new Label();
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

    static Dictionary<string, object> Ready()
    {
        var request = (HttpWebRequest)WebRequest.Create(ReadyUrl);
        request.Proxy = null;
        request.AllowAutoRedirect = false;
        request.Timeout = request.ReadWriteTimeout = 3500;
        request.Headers["Authorization"] = "Bearer " + Token();
        using (var response = request.GetResponse())
        using (var reader = new StreamReader(response.GetResponseStream()))
        {
            var status = new JavaScriptSerializer().Deserialize<Dictionary<string, object>>(reader.ReadToEnd());
            if (!object.Equals(status["service"], "materials") || !object.Equals(status["ready"], true))
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
        ClientSize = new Size(650, 332);
        StartPosition = FormStartPosition.CenterScreen;
        FormBorderStyle = FormBorderStyle.FixedSingle;
        MaximizeBox = false;

        Controls.Add(new Label { Text = "项目材料预处理 Worker 8787", AutoSize = true,
            Location = new Point(24, 18), Font = new Font(Font.FontFamily, 17, FontStyle.Bold) });
        state.SetBounds(24, 68, 600, 28);
        details.SetBounds(24, 100, 600, 44);
        Controls.Add(state);
        Controls.Add(details);

        string address = "http://127.0.0.1:8787";
        try { address = (string)ReadJson(Path.Combine(DesktopDir, "runtime.json"))["base_url"]; }
        catch (Exception) { } // The status panel reports installation errors on startup.
        Controls.Add(new Label { Text = "调用地址", AutoSize = true, Location = new Point(24, 150) });
        var url = new TextBox { Text = address, ReadOnly = true };
        url.SetBounds(24, 178, 338, 28);
        Controls.Add(url);
        ButtonAt("复制地址", 376, 175, 108, delegate { Clipboard.SetText(url.Text); });
        ButtonAt("复制 Token", 496, 175, 128, delegate { Clipboard.SetText(Token()); });
        startButton = ButtonAt("检查 / 启动服务", 24, 235, 146, StartWorker);
        refreshButton = ButtonAt("刷新状态", 182, 235, 128, RefreshStatus);
        ButtonAt("查看日志", 322, 235, 128, delegate { Process.Start("explorer.exe", DataDir); });
        ButtonAt("隐藏到托盘", 462, 235, 162, Hide);
        Controls.Add(new Label { Text = "关闭窗口或最小化会隐藏到托盘。要停止服务，请右击托盘图标并选择“退出并停止服务”。",
            Location = new Point(24, 284), Size = new Size(603, 42), ForeColor = Color.DimGray });

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

    void SetState(string summary, Color color, string detail)
    {
        state.Text = "● " + summary;
        state.ForeColor = color;
        details.Text = detail;
        tray.Text = "材料 Worker 8787 · " + summary;
    }

    void DisplayReady(Dictionary<string, object> status)
    {
        SetState("服务就绪", Color.ForestGreen, string.Format("执行位 {0}　·　输入格式 {1}\n上次检查 {2:HH:mm:ss}（本机时间）",
            status["execution_slots"], string.Join(" / ", ((System.Collections.IEnumerable)status["document_types"]).Cast<object>()), DateTime.Now));
    }

    async void RefreshStatus()
    {
        if (checking || stopping || !starting.IsCompleted) return;
        checking = true;
        int revision = generation;
        try {
            var status = await Task.Run((Func<Dictionary<string, object>>)Ready);
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
        if (e.CloseReason == CloseReason.UserClosing && !allowClose) { e.Cancel = true; Hide(); }
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
