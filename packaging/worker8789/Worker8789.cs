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

class Worker8789 : Form {
 const string Data = @"C:\CodexWorkerData-AppServer-8789";
 const string TaskName = "CodexWorker8789-Recovery";
 const string Address = "http://127.0.0.1:8789";
 readonly Label state = new Label(), details = new Label(), providerLabel = new Label();
 string sourceSignature="";
 readonly TextBox tokenBox = new TextBox();
 readonly ComboBox modelBox=new ComboBox(), effortBox=new ComboBox();
 readonly Button applyButton=new Button();
 readonly NumericUpDown channelsBox=new NumericUpDown();
 readonly Dictionary<string,string[]> models=new Dictionary<string,string[]>();
 bool settingBusy;
 readonly NotifyIcon tray = new NotifyIcon();
 readonly System.Windows.Forms.Timer timer = new System.Windows.Forms.Timer();
 readonly JavaScriptSerializer json = new JavaScriptSerializer();
 string token = ""; bool busy, exiting;
 static EventWaitHandle activate;
 static Mutex single;
 Dictionary<string,object> Read(string path) {return json.Deserialize<Dictionary<string,object>>(File.ReadAllText(path));}
 public Worker8789(bool hidden) {
  Text="Codex Worker · 8789"; ClientSize=new Size(710,635); Font=new Font("Microsoft YaHei UI",10); StartPosition=FormStartPosition.CenterScreen; FormBorderStyle=FormBorderStyle.FixedSingle; MaximizeBox=false;
  var title=new Label {Text="翻译 Worker 8789",Font=new Font(Font.FontFamily,18,FontStyle.Bold),Location=new Point(24,20),AutoSize=true}; Controls.Add(title);
  state.SetBounds(24,65,655,32); state.Text="正在检查服务…"; Controls.Add(state);
  details.SetBounds(24,100,655,65); Controls.Add(details);
  Controls.Add(new Label {Text="本机测试地址",Location=new Point(24,175),AutoSize=true});
  var url=new TextBox {Text=Address,ReadOnly=true,Location=new Point(24,200),Width=510}; Controls.Add(url);
  ButtonAt("复制地址",550,198,delegate {Clipboard.SetText(Address);});
  Controls.Add(new Label {Text="API Token",Location=new Point(24,240),AutoSize=true});
  tokenBox.SetBounds(24,266,510,28); tokenBox.ReadOnly=true; tokenBox.UseSystemPasswordChar=true; Controls.Add(tokenBox);
  ButtonAt("复制 Token",550,264,delegate {if(token.Length>0)Clipboard.SetText(token);});
  var reveal=new CheckBox {Text="显示 Token",Location=new Point(24,303),AutoSize=true}; reveal.CheckedChanged+=delegate {tokenBox.UseSystemPasswordChar=!reveal.Checked;}; Controls.Add(reveal);
  Controls.Add(new Label {Text="模型",Location=new Point(24,343),AutoSize=true});
  providerLabel.SetBounds(155,339,510,27);providerLabel.Text="读取当前 Codex 供应商…";Controls.Add(providerLabel);
  modelBox.SetBounds(24,368,345,30); modelBox.DropDownStyle=ComboBoxStyle.DropDownList; Controls.Add(modelBox);
  Controls.Add(new Label {Text="思考等级",Location=new Point(385,343),AutoSize=true});
  effortBox.SetBounds(385,368,130,30); effortBox.DropDownStyle=ComboBoxStyle.DropDownList; Controls.Add(effortBox);
  modelBox.SelectedIndexChanged+=delegate {FillEfforts(null);};
  applyButton.Text="应用并重启"; applyButton.SetBounds(535,420,140,34);applyButton.Enabled=false;
  applyButton.Click+=delegate {ApplySettings();}; Controls.Add(applyButton);
  Controls.Add(new Label {Text="执行通道（全局 1～8 路）",Location=new Point(24,405),AutoSize=true});
  channelsBox.SetBounds(24,430,130,30);channelsBox.Minimum=1;channelsBox.Maximum=8;channelsBox.ReadOnly=true;Controls.Add(channelsBox);
  Controls.Add(new Label {Text="用于不同项目同时执行；同一整单仍按顺序翻译。\n模型、思考等级与通道设置仅在没有未完成任务时应用。",Location=new Point(24,480),Size=new Size(660,52),ForeColor=Color.DimGray});
  ButtonAt("检查 / 启动",24,545,delegate {StartWorker();});
  ButtonAt("查看日志",160,545,delegate {Process.Start("explorer.exe",Data);});
  ButtonAt("隐藏到托盘",296,545,delegate {Hide();});
  ButtonAt("刷新 Codex",432,545,delegate {sourceSignature="";RefreshStatus();});
  ButtonAt("登录 ChatGPT",568,545,delegate {
   var info = new ProcessStartInfo(Read(@"C:\CodexRemoteWorker-AppServer-8789\desktop\runtime.json")["pwsh"].ToString(),
      "-NoProfile -File \"C:\\CodexRemoteWorker-AppServer-8789\\desktop\\login.ps1\"") {UseShellExecute=true};
   Process.Start(info);
  });
  var note=new Label {Text="关闭窗口会收进托盘；退出界面不会停止翻译服务。",Location=new Point(24,595),AutoSize=true,ForeColor=Color.DimGray}; Controls.Add(note);
  tray.Icon=SystemIcons.Application; tray.Text="Worker 8789 · 正在检查"; tray.Visible=true;
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
 string LoadToken(){return (string)Read(Path.Combine(Data,@"private\credentials.local.json"))["CODEX_WORKER_SERVICE_TOKEN"];}
 Dictionary<string,object> Ready(string secret){
  var req=(HttpWebRequest)WebRequest.Create(Address+"/v1/ready");req.Proxy=null;req.Timeout=4000;req.Headers["Authorization"]="Bearer "+secret;
  WebResponse response;
  try{response=req.GetResponse();}catch(WebException error){
   var http=error.Response as HttpWebResponse;
   if(http==null || http.StatusCode!=HttpStatusCode.ServiceUnavailable)throw;
   response=http;
  }
  using(response)using(var stream=new StreamReader(response.GetResponseStream()))return json.Deserialize<Dictionary<string,object>>(stream.ReadToEnd());
 }
 async void RefreshStatus(){if(busy || settingBusy)return;busy=true;try {
  if(token.Length==0){token=LoadToken();tokenBox.Text=token;}
  var current=await Task.Run(()=>InvokeController("--describe"));if(IsDisposed)return;
  if(!(bool)current["ok"])throw new Exception((string)current["message"]);
  string signature=(string)current["source_signature"];
  if(signature!=sourceSignature){LoadModels(current);sourceSignature=signature;}
  providerLabel.Text="Codex 供应商："+(string)current["provider_name"];
  applyButton.Enabled=models.Count>0 && !settingBusy;
  Dictionary<string,object> r=null;
  try{r=await Task.Run(()=>Ready(token));}catch{}
  if(IsDisposed)return;
  if(r==null){state.Text="● 服务未启动，可应用当前 Codex 配置恢复";state.ForeColor=Color.Firebrick;details.Text="已读取当前 Codex 的供应商和模型目录。\n选择模型、思考等级后点击“应用并重启”。";tray.Text="Worker 8789 · 等待恢复";return;}
  bool ready=r.ContainsKey("ready") && (bool)r["ready"];
  if(!ready){state.Text="● 8789 服务在线，等待模型就绪";state.ForeColor=Color.DarkOrange;details.Text=r.ContainsKey("message")?(string)r["message"]:"正在连接模型服务。";applyButton.Enabled=false;tray.Text="Worker 8789 · 等待就绪";return;}
  state.Text=ready?"● 服务就绪 · 8789 正在运行":"● 服务在线，暂未就绪";state.ForeColor=ready?Color.ForestGreen:Color.DarkOrange;
  details.Text=String.Format("服务供应商：{0}　模型：{1} / {2}\n执行通道：{3} / {4} 使用中　Server 复用：{5}\n本机检查：{6:HH:mm:ss} UTC",r["configured_provider"],r["configured_model"],r["configured_reasoning_effort"],r["active_execution_slots"],r["max_concurrent_jobs"],r["app_server_reuse"],DateTime.UtcNow);
  tray.Text=ready?"Worker 8789 · 服务就绪":"Worker 8789 · 暂未就绪";
 }catch{if(!IsDisposed){applyButton.Enabled=false;state.Text="● 无法读取 Codex 设置";state.ForeColor=Color.Firebrick;details.Text="请检查当前 Codex 配置、认证和模型目录后刷新。";tray.Text="Worker 8789 · 检查失败";}}finally{busy=false;}}
 void FillEfforts(string preferred){effortBox.Items.Clear();string name=modelBox.SelectedItem as string;if(name==null || !models.ContainsKey(name))return;effortBox.Items.AddRange(models[name]);if(preferred!=null && effortBox.Items.Contains(preferred))effortBox.SelectedItem=preferred;else if(effortBox.Items.Contains("high"))effortBox.SelectedItem="high";else if(effortBox.Items.Count>0)effortBox.SelectedIndex=0;}
 void LoadModels(Dictionary<string,object> state){
  models.Clear();modelBox.Items.Clear();
  channelsBox.Value=Convert.ToDecimal(state["max_concurrent_jobs"]);
  foreach(object item in (System.Collections.IEnumerable)state["available_models"]){var m=(Dictionary<string,object>)item;var efforts=new List<string>();foreach(object e in (System.Collections.IEnumerable)m["reasoning_efforts"])efforts.Add((string)e);if(efforts.Count>0)models[(string)m["model"]]=efforts.ToArray();}
  foreach(string name in models.Keys)modelBox.Items.Add(name);
  modelBox.SelectedItem=(string)state["configured_model"];
  if(modelBox.SelectedIndex<0 && modelBox.Items.Count>0)modelBox.SelectedIndex=0;
  FillEfforts((string)state["configured_reasoning_effort"]);
 }
 Dictionary<string,object> InvokeController(string arguments){
    var info=new ProcessStartInfo(@"C:\CodexRemoteWorker\.runtime\python313\python.exe",
      "-X utf8 \"C:\\CodexRemoteWorker-AppServer-8789\\desktop\\settings8789.py\" "+arguments) {UseShellExecute=false,CreateNoWindow=true,RedirectStandardOutput=true,RedirectStandardError=true,StandardOutputEncoding=System.Text.Encoding.UTF8,StandardErrorEncoding=System.Text.Encoding.UTF8};
    using(var process=Process.Start(info)){var errorRead=process.StandardError.ReadToEndAsync();string output=process.StandardOutput.ReadToEnd();process.WaitForExit();return new JavaScriptSerializer().Deserialize<Dictionary<string,object>>(output);}
 }
 Dictionary<string,object> InvokeSettings(string model,string effort,int channels){return InvokeController("--model \""+model+"\" --effort \""+effort+"\" --channels "+channels.ToString(System.Globalization.CultureInfo.InvariantCulture)+" --source-signature \""+sourceSignature+"\"");}
 async void ApplySettings(){
  if(settingBusy || busy || modelBox.SelectedItem==null || effortBox.SelectedItem==null)return;
  string model=(string)modelBox.SelectedItem, effort=(string)effortBox.SelectedItem;
  int channels=(int)channelsBox.Value;
  settingBusy=true;applyButton.Enabled=false;modelBox.Enabled=effortBox.Enabled=channelsBox.Enabled=false;state.Text="正在验证并应用设置，请等待…";
  try {
   var result=await Task.Run(()=>InvokeSettings(model,effort,channels));
   MessageBox.Show((string)result["message"],"Worker 8789 设置",MessageBoxButtons.OK,(bool)result["ok"]?MessageBoxIcon.Information:MessageBoxIcon.Warning);
  }catch {MessageBox.Show("设置未完成，请检查服务和维护日志。", "Worker 8789");}
  finally{settingBusy=false;modelBox.Enabled=effortBox.Enabled=channelsBox.Enabled=true;RefreshStatus();}
 }
 async void StartWorker(){
  try{
   await Task.Run(()=>{try{Ready(LoadToken());return;}catch(WebException){}
    using(var p=Process.Start(new ProcessStartInfo("schtasks.exe","/Run /TN "+TaskName){UseShellExecute=false,CreateNoWindow=true})){
     p.WaitForExit();if(p.ExitCode!=0)throw new Exception();
    }
   });
   RefreshStatus();
  }catch{MessageBox.Show("无法启动 8789，请检查恢复任务和日志。");}
 }
 [STAThread] static void Main(string[] args){
  if(Array.IndexOf(args,"--self-test")>=0){
   try{
    Application.EnableVisualStyles();
    activate=new EventWaitHandle(false,EventResetMode.AutoReset);
    using(var f=new Worker8789(false)){
     var state=f.Ready(f.LoadToken());
     var catalog=f.InvokeController("--describe");
     if(!(bool)catalog["ok"] || !state.ContainsKey("ready"))throw new InvalidOperationException();
     f.LoadModels(catalog);f.CreateControl();f.Show();Application.DoEvents();
     f.Hide();bool hidden=!f.Visible;f.ShowWindow();Application.DoEvents();
     bool ok=f.modelBox.SelectedItem!=null && f.effortBox.SelectedItem!=null && f.tray.Visible && hidden && f.Visible;
     File.WriteAllText(Path.Combine(Data,"desktop-self-test.json"),new JavaScriptSerializer().Serialize(new {
      passed=ok,ready=state["ready"],model=f.modelBox.SelectedItem,channels=(int)f.channelsBox.Value,
      model_count=f.models.Count,tray_visible=f.tray.Visible,hide_restore=hidden&&f.Visible}));
     f.exiting=true;f.Close();if(!ok)Environment.ExitCode=1;
    }
   }catch{Environment.ExitCode=1;}
   return;
  }
  bool first;single=new Mutex(true,"Local\\CodexWorker8789-Desktop",out first);activate=new EventWaitHandle(false,EventResetMode.AutoReset,"Local\\CodexWorker8789-Show");if(!first){activate.Set();return;}
  Application.EnableVisualStyles();Application.SetCompatibleTextRenderingDefault(false);Application.Run(new Worker8789(Array.IndexOf(args,"--tray")>=0));GC.KeepAlive(single);
 }
}
