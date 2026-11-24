using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Net;
using System.Threading;
using System.Threading.Tasks;
using System.Web.Script.Serialization;
using System.Windows.Forms;

class Worker8788 : Form {
 const string Data = @"C:\CodexWorkerData-AppServer-8788";
 const string Address = "http://52.62.69.192:8788";
 readonly Label state = new Label(), details = new Label(), providerLabel = new Label(), modelLabel = new Label(), effortLabel = new Label(), channelsLabel = new Label();
 readonly TextBox tokenBox = new TextBox();
 readonly NotifyIcon tray = new NotifyIcon();
 readonly System.Windows.Forms.Timer timer = new System.Windows.Forms.Timer();
 readonly JavaScriptSerializer json = new JavaScriptSerializer();
 string token = ""; bool starting, exiting;
 Task refreshTask = Task.FromResult(0);
 static EventWaitHandle activate;
 static Mutex single;
 Dictionary<string,object> Read(string path) {return json.Deserialize<Dictionary<string,object>>(File.ReadAllText(path));}
 public Worker8788(bool hidden) {
  Text="Codex Worker · 8788"; ClientSize=new Size(710,635); Font=new Font("Microsoft YaHei UI",10); StartPosition=FormStartPosition.CenterScreen; FormBorderStyle=FormBorderStyle.FixedSingle; MaximizeBox=false;
  var title=new Label {Text="翻译 Worker 8788",Font=new Font(Font.FontFamily,18,FontStyle.Bold),Location=new Point(24,20),AutoSize=true}; Controls.Add(title);
  state.SetBounds(24,65,655,32); state.Text="正在检查服务…"; Controls.Add(state);
  details.SetBounds(24,100,655,65); Controls.Add(details);
  Controls.Add(new Label {Text="公网调用地址（服务器配置）",Location=new Point(24,175),AutoSize=true});
  var url=new TextBox {Text=Address,ReadOnly=true,Location=new Point(24,200),Width=510}; Controls.Add(url);
  ButtonAt("复制地址",550,198,delegate {Clipboard.SetText(Address);});
  Controls.Add(new Label {Text="API Token",Location=new Point(24,240),AutoSize=true});
  tokenBox.SetBounds(24,266,510,28); tokenBox.ReadOnly=true; tokenBox.UseSystemPasswordChar=true; Controls.Add(tokenBox);
  ButtonAt("复制 Token",550,264,delegate {if(token.Length>0)Clipboard.SetText(token);});
  var reveal=new CheckBox {Text="显示 Token",Location=new Point(24,303),AutoSize=true}; reveal.CheckedChanged+=delegate {tokenBox.UseSystemPasswordChar=!reveal.Checked;}; Controls.Add(reveal);
  Controls.Add(new Label {Text="服务实际配置（只读）",Location=new Point(24,343),AutoSize=true,ForeColor=Color.DimGray});
  providerLabel.SetBounds(24,370,660,24); providerLabel.Text="供应商：正在读取服务…"; Controls.Add(providerLabel);
  modelLabel.SetBounds(24,398,660,24); modelLabel.Text="模型：正在读取服务…"; Controls.Add(modelLabel);
  effortLabel.SetBounds(24,426,660,24); effortLabel.Text="思考等级：正在读取服务…"; Controls.Add(effortLabel);
  channelsLabel.SetBounds(24,454,660,24); channelsLabel.Text="执行通道：正在读取服务…"; Controls.Add(channelsLabel);
  Controls.Add(new Label {Text="本窗口只显示 8788 服务当前生效的配置，不读取或应用桌面 Codex 的模型设置。",Location=new Point(24,487),Size=new Size(660,40),ForeColor=Color.DimGray});
  ButtonAt("检查 / 启动服务",24,545,delegate {StartWorker();});
  ButtonAt("查看日志",175,545,delegate {Process.Start("explorer.exe",Data);});
  ButtonAt("隐藏到托盘",326,545,delegate {Hide();});
  ButtonAt("刷新状态",477,545,delegate {RefreshStatus();});
  var note=new Label {Text="关闭窗口会收进托盘；退出界面不会停止翻译服务。",Location=new Point(24,595),AutoSize=true,ForeColor=Color.DimGray}; Controls.Add(note);
  Icon=System.Drawing.Icon.ExtractAssociatedIcon(typeof(Worker8788).Assembly.Location);
  tray.Icon=Icon; tray.Text="Worker 8788 · 正在检查"; tray.Visible=true;
  var menu=new ContextMenuStrip(); menu.Items.Add("打开控制台",null,delegate {ShowWindow();}); menu.Items.Add("检查 / 启动服务",null,delegate {StartWorker();}); menu.Items.Add("复制调用地址",null,delegate {Clipboard.SetText(Address);}); menu.Items.Add("退出控制台（服务继续运行）",null,delegate {exiting=true; Close();}); tray.ContextMenuStrip=menu; tray.DoubleClick+=delegate {ShowWindow();};
  FormClosing+=delegate(object sender,FormClosingEventArgs e) {if(!exiting && e.CloseReason==CloseReason.UserClosing){e.Cancel=true;Hide();}};
  Resize+=delegate {if(WindowState==FormWindowState.Minimized)Hide();};
  FormClosed+=delegate {timer.Stop();tray.Dispose();};
  timer.Interval=5000; timer.Tick+=delegate {RefreshStatus();}; timer.Start();
  Shown+=delegate {if(hidden)Hide(); RefreshStatus();};
  ThreadPool.RegisterWaitForSingleObject(activate,delegate {if(IsHandleCreated&&!IsDisposed)BeginInvoke((Action)ShowWindow);},null,-1,false);
 }
 void ButtonAt(string text,int x,int y,Action action) {var b=new Button {Text=text,Location=new Point(x,y),Size=new Size(132,32)}; b.Click+=delegate {try{action();}catch(Exception){MessageBox.Show("操作失败，请检查权限或服务日志。");}}; Controls.Add(b);}
 void ShowWindow(){Show();WindowState=FormWindowState.Normal;Activate();}
 string LoadToken(){var cfg=Read(@"C:\CodexRemoteWorker\config.local.json");var secrets=Read(Path.Combine((string)cfg["data_root"],@"private\credentials.local.json"));return (string)secrets["CODEX_WORKER_SERVICE_TOKEN"];}
 Dictionary<string,object> Ready(string secret){
  var req=(HttpWebRequest)WebRequest.Create("http://127.0.0.1:8788/v1/ready");req.Proxy=null;req.Timeout=4000;req.Headers["Authorization"]="Bearer "+secret;
  WebResponse response;
  try { response=req.GetResponse(); }
  catch(WebException exc){
   var http=exc.Response as HttpWebResponse;
   if(http==null && exc.Status==WebExceptionStatus.ConnectFailure)return null;
   if(http==null)throw;
   if(http.StatusCode!=HttpStatusCode.ServiceUnavailable){
    using(http)throw new InvalidOperationException("8788 状态接口返回 HTTP "+(int)http.StatusCode+"。");
   }
   response=http;
  }
  using(response)using(var stream=new StreamReader(response.GetResponseStream()))
   return json.Deserialize<Dictionary<string,object>>(stream.ReadToEnd());
 }
 void ClearServiceState(string reason){
  providerLabel.Text="供应商：未读取";modelLabel.Text="模型："+reason;effortLabel.Text="思考等级：—";channelsLabel.Text="执行通道：—";
 }
 void SetServiceState(Dictionary<string,object> value){
  providerLabel.Text="供应商："+Value(value,"configured_provider");
  modelLabel.Text="模型："+Value(value,"configured_model");
  effortLabel.Text="思考等级："+Value(value,"configured_reasoning_effort");
  channelsLabel.Text=String.Format("执行通道：{0} / {1} 使用中　Server 复用：{2}",Value(value,"active_execution_slots"),Value(value,"max_concurrent_jobs"),Value(value,"app_server_reuse"));
 }
 string Value(Dictionary<string,object> value,string name){object item;return value.TryGetValue(name,out item)&&item!=null?item.ToString():"未知";}
 void DisplayStatus(Dictionary<string,object> r){
  if(r==null){ClearServiceState("服务未启动");state.Text="● 服务未启动，无法读取实际配置";state.ForeColor=Color.Firebrick;details.Text="Worker 只显示 8788 服务自身的配置。\n点击“检查 / 启动服务”后再检查。";tray.Text="Worker 8788 · 服务未启动";return;}
  SetServiceState(r);
  bool ready=r.ContainsKey("ready") && (bool)r["ready"];
  state.Text=ready?"● 服务就绪 · 8788 正在运行":"● 服务在线，暂未就绪";state.ForeColor=ready?Color.ForestGreen:Color.DarkOrange;
  details.Text=String.Format("以上模型和思考等级来自 8788 服务实际配置。\n本机检查：{0:HH:mm:ss} UTC",DateTime.UtcNow);
  tray.Text=ready?"Worker 8788 · 服务就绪":"Worker 8788 · 暂未就绪";
 }
 Task<Dictionary<string,object>> ReadStatus(){
  if(token.Length==0){token=LoadToken();tokenBox.Text=token;}
  return Task.Run(()=>Ready(token));
 }
 void RefreshStatus(){
  if(!starting && refreshTask.IsCompleted)refreshTask=RefreshStatusAsync();
 }
 async Task RefreshStatusAsync(){
  try {
   var value=await ReadStatus();
   if(!IsDisposed && !starting)DisplayStatus(value);
  }catch(Exception exc){
   if(IsDisposed || starting)return;
   ClearServiceState("无法读取");state.Text="● 无法读取服务状态";state.ForeColor=Color.Firebrick;
   details.Text=exc.Message;tray.Text="Worker 8788 · 检查失败";
  }
 }
 static void RequestServiceStart(){
  var info=new ProcessStartInfo(Path.Combine(Environment.SystemDirectory,"schtasks.exe"),"/Run /TN CodexWorker8788-Recovery") {UseShellExecute=false,CreateNoWindow=true};
  using(var process=Process.Start(info)){
   process.WaitForExit();
   if(process.ExitCode!=0)throw new InvalidOperationException("无法运行计划任务 CodexWorker8788-Recovery，返回码 "+process.ExitCode+"。");
  }
 }
 async void StartWorker(){
  if(starting)return;
  starting=true;
  try{
   state.Text="正在检查 8788 服务…";state.ForeColor=Color.DarkOrange;
   await refreshTask;
   var current=await ReadStatus();
   if(IsDisposed)return;
   if(current==null){
    state.Text="正在启动 8788 服务…";details.Text="正在等待 8788 的状态接口。";
    await Task.Run((Action)RequestServiceStart);
    var until=DateTime.UtcNow.AddSeconds(150);
    while(current==null && DateTime.UtcNow<until){
     await Task.Delay(1000);
     if(IsDisposed)return;
     current=await ReadStatus();
    }
    if(current==null)throw new TimeoutException("8788 启动超时。请查看日志目录中的 watchdog.log 和 serve.stderr.log。");
   }
   if(!IsDisposed)DisplayStatus(current);
  }catch(Exception exc){if(!IsDisposed)MessageBox.Show(exc.Message,"Worker 8788 启动",MessageBoxButtons.OK,MessageBoxIcon.Warning);}
  finally{starting=false;}
 }
 [STAThread] static void Main(string[] args){
  if(Array.IndexOf(args,"--self-test")>=0){try{Application.EnableVisualStyles();activate=new EventWaitHandle(false,EventResetMode.AutoReset);using(var f=new Worker8788(false)){var sample=new Dictionary<string,object>{{"configured_provider","test-provider"},{"configured_model","test-service-model"},{"configured_reasoning_effort","test-effort"},{"active_execution_slots",1},{"max_concurrent_jobs",2},{"app_server_reuse",true},{"ready",true}};f.SetServiceState(sample);f.CreateControl();f.Show();Application.DoEvents();f.Hide();bool hidden=!f.Visible;f.ShowWindow();Application.DoEvents();File.WriteAllText(Path.Combine(Data,"desktop-self-test.json"),"{\"tray_visible\":"+f.tray.Visible.ToString().ToLowerInvariant()+",\"hide_restore\":"+(hidden&&f.Visible).ToString().ToLowerInvariant()+",\"token_loaded\":"+(f.LoadToken().Length>0).ToString().ToLowerInvariant()+"}");using(var bitmap=new Bitmap(f.Width,f.Height)){f.DrawToBitmap(bitmap,new Rectangle(0,0,f.Width,f.Height));bitmap.Save(Path.Combine(Data,"desktop-self-test.png"),System.Drawing.Imaging.ImageFormat.Png);}File.WriteAllText(Path.Combine(Data,"desktop-service-self-test.json"),new JavaScriptSerializer().Serialize(new {provider=f.providerLabel.Text,model=f.modelLabel.Text,effort=f.effortLabel.Text,channels=f.channelsLabel.Text,service_config_only=true}));f.exiting=true;f.Close();}return;}catch{Environment.Exit(1);}}
  bool first;single=new Mutex(true,"Local\\CodexWorker8788-Desktop",out first);activate=new EventWaitHandle(false,EventResetMode.AutoReset,"Local\\CodexWorker8788-Show");if(!first){activate.Set();return;}
  Application.EnableVisualStyles();Application.SetCompatibleTextRenderingDefault(false);Application.Run(new Worker8788(Array.IndexOf(args,"--tray")>=0));GC.KeepAlive(single);
 }
}
