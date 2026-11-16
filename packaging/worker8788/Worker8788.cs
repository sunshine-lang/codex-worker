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
 const string TaskName = "CodexWorker8788-Recovery";
 const string Address = "http://52.62.69.192:8788";
 readonly Label state = new Label(), details = new Label();
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
  Controls.Add(new Label {Text="模型",Location=new Point(24,343),AutoSize=true});
  modelBox.SetBounds(24,368,345,30); modelBox.DropDownStyle=ComboBoxStyle.DropDownList; Controls.Add(modelBox);
  Controls.Add(new Label {Text="思考等级",Location=new Point(385,343),AutoSize=true});
  effortBox.SetBounds(385,368,130,30); effortBox.DropDownStyle=ComboBoxStyle.DropDownList; Controls.Add(effortBox);
  modelBox.SelectedIndexChanged+=delegate {FillEfforts(null);};
  applyButton.Text="应用并重启"; applyButton.SetBounds(535,420,140,34);applyButton.Enabled=false;
  applyButton.Click+=delegate {ApplySettings();}; Controls.Add(applyButton);
  Controls.Add(new Label {Text="同时处理的整单数（1～8）",Location=new Point(24,405),AutoSize=true});
  channelsBox.SetBounds(24,430,130,30);channelsBox.Minimum=1;channelsBox.Maximum=8;channelsBox.ReadOnly=true;Controls.Add(channelsBox);
  Controls.Add(new Label {Text="每份整单使用一个持续的 Codex 会话，完成翻译、回改和整单检查。\n设置仅在没有未完成任务时应用；通道数用于不同整单同时执行。",Location=new Point(24,480),Size=new Size(660,52),ForeColor=Color.DimGray});
  ButtonAt("检查 / 启动",24,545,delegate {StartWorker();});
  ButtonAt("查看日志",175,545,delegate {Process.Start("explorer.exe",Data);});
  ButtonAt("隐藏到托盘",326,545,delegate {Hide();});
  var note=new Label {Text="关闭窗口会收进托盘；退出界面不会停止翻译服务。",Location=new Point(24,595),AutoSize=true,ForeColor=Color.DimGray}; Controls.Add(note);
  tray.Icon=SystemIcons.Application; tray.Text="Worker 8788 · 正在检查"; tray.Visible=true;
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
 Dictionary<string,object> Ready(string secret){var req=(HttpWebRequest)WebRequest.Create("http://127.0.0.1:8788/v1/ready");req.Proxy=null;req.Timeout=4000;req.Headers["Authorization"]="Bearer "+secret;using(var res=req.GetResponse())using(var stream=new StreamReader(res.GetResponseStream()))return json.Deserialize<Dictionary<string,object>>(stream.ReadToEnd());}
 async void RefreshStatus(){if(busy)return;busy=true;try {
  if(token.Length==0){token=LoadToken();tokenBox.Text=token;}
  var r=await Task.Run(()=>Ready(token)); if(IsDisposed)return;
  bool ready=r.ContainsKey("ready") && (bool)r["ready"];
  state.Text=ready?"● 服务就绪 · 8788 正在运行":"● 服务在线，暂未就绪";state.ForeColor=ready?Color.ForestGreen:Color.DarkOrange;
  details.Text=String.Format("模型：{0} / {1}　认证：{2}\n执行通道：{3} / {4} 使用中　Server 复用：{5}\n本机检查：{6:HH:mm:ss} UTC（公网可达性需从上游验证）",r["configured_model"],r["configured_reasoning_effort"],r["authentication_mode"],r["active_execution_slots"],r["max_concurrent_jobs"],r["app_server_reuse"],DateTime.UtcNow);
  if(models.Count==0 && r.ContainsKey("available_models"))LoadModels(r);
  applyButton.Enabled=ready && models.Count>0 && !settingBusy;
  tray.Text=ready?"Worker 8788 · 服务就绪":"Worker 8788 · 暂未就绪";
 }catch{if(!IsDisposed){state.Text="● 服务不可达或鉴权失败";state.ForeColor=Color.Firebrick;details.Text="开机及每分钟检查进程退出并恢复；可点击“检查 / 启动”。\n若持续异常，请查看日志。";tray.Text="Worker 8788 · 检查失败";}}finally{busy=false;}}
 void FillEfforts(string preferred){effortBox.Items.Clear();string name=modelBox.SelectedItem as string;if(name==null || !models.ContainsKey(name))return;effortBox.Items.AddRange(models[name]);if(preferred!=null && effortBox.Items.Contains(preferred))effortBox.SelectedItem=preferred;else if(effortBox.Items.Contains("high"))effortBox.SelectedItem="high";else if(effortBox.Items.Count>0)effortBox.SelectedIndex=0;}
 void LoadModels(Dictionary<string,object> state){
  channelsBox.Value=Convert.ToDecimal(state["max_concurrent_jobs"]);
  foreach(object item in (System.Collections.IEnumerable)state["available_models"]){var m=(Dictionary<string,object>)item;var efforts=new List<string>();foreach(object e in (System.Collections.IEnumerable)m["reasoning_efforts"])efforts.Add((string)e);if(efforts.Count>0)models[(string)m["model"]]=efforts.ToArray();}
  foreach(string name in models.Keys)modelBox.Items.Add(name);
  modelBox.SelectedItem=(string)state["configured_model"];
  if(modelBox.SelectedIndex<0 && modelBox.Items.Count>0)modelBox.SelectedIndex=0;
  FillEfforts((string)state["configured_reasoning_effort"]);
 }
 Dictionary<string,object> InvokeSettings(string model,string effort,int channels){
    var info=new ProcessStartInfo(@"C:\CodexRemoteWorker\.runtime\python313\python.exe",
      "-X utf8 \"C:\\CodexRemoteWorker-AppServer-8788\\desktop\\settings8788.py\" --model \""+model+"\" --effort \""+effort+"\" --channels "+channels.ToString(System.Globalization.CultureInfo.InvariantCulture)) {UseShellExecute=false,CreateNoWindow=true,RedirectStandardOutput=true,RedirectStandardError=true,StandardOutputEncoding=System.Text.Encoding.UTF8,StandardErrorEncoding=System.Text.Encoding.UTF8};
    using(var process=Process.Start(info)){var errorRead=process.StandardError.ReadToEndAsync();string output=process.StandardOutput.ReadToEnd();process.WaitForExit();return new JavaScriptSerializer().Deserialize<Dictionary<string,object>>(output);}
 }
 async void ApplySettings(){
  if(settingBusy || modelBox.SelectedItem==null || effortBox.SelectedItem==null)return;
  string model=(string)modelBox.SelectedItem, effort=(string)effortBox.SelectedItem;
  int channels=(int)channelsBox.Value;
  settingBusy=true;applyButton.Enabled=false;modelBox.Enabled=effortBox.Enabled=channelsBox.Enabled=false;state.Text="正在验证并应用设置，请等待…";
  try {
   var result=await Task.Run(()=>InvokeSettings(model,effort,channels));
   MessageBox.Show((string)result["message"],"Worker 8788 设置",MessageBoxButtons.OK,(bool)result["ok"]?MessageBoxIcon.Information:MessageBoxIcon.Warning);
  }catch {MessageBox.Show("设置未完成，请检查服务和维护日志。", "Worker 8788");}
  finally{settingBusy=false;modelBox.Enabled=effortBox.Enabled=channelsBox.Enabled=true;RefreshStatus();}
 }
 void StartWorker(){var p=new ProcessStartInfo("schtasks.exe","/Run /TN "+TaskName){UseShellExecute=false,CreateNoWindow=true};using(var proc=Process.Start(p)){} state.Text="已请求后台检查 / 启动，稍后自动刷新…";}
 [STAThread] static void Main(string[] args){
  if(Array.IndexOf(args,"--self-test")>=0){try{Application.EnableVisualStyles();activate=new EventWaitHandle(false,EventResetMode.AutoReset);using(var f=new Worker8788(false)){var r=f.Ready(f.LoadToken()); f.LoadModels(r); if(f.modelBox.SelectedItem==null || f.effortBox.SelectedItem==null)throw new Exception("No model settings"); var settingsResult=f.InvokeSettings((string)f.modelBox.SelectedItem,(string)f.effortBox.SelectedItem,(int)f.channelsBox.Value);if(!(bool)settingsResult["ok"])throw new Exception("Settings controller failed");f.CreateControl();f.Show();Application.DoEvents();f.Hide();bool hidden=!f.Visible;f.ShowWindow();Application.DoEvents();File.WriteAllText(Path.Combine(Data,"desktop-self-test.json"),"{\"ready\":"+r["ready"].ToString().ToLowerInvariant()+",\"tray_visible\":"+f.tray.Visible.ToString().ToLowerInvariant()+",\"hide_restore\":"+(hidden&&f.Visible).ToString().ToLowerInvariant()+",\"token_loaded\":"+(f.LoadToken().Length>0).ToString().ToLowerInvariant()+"}");var until=DateTime.UtcNow.AddSeconds(6);while(f.busy && DateTime.UtcNow<until){Application.DoEvents();Thread.Sleep(30);}using(var bitmap=new Bitmap(f.Width,f.Height)){f.DrawToBitmap(bitmap,new Rectangle(0,0,f.Width,f.Height));bitmap.Save(Path.Combine(Data,"desktop-self-test.png"),System.Drawing.Imaging.ImageFormat.Png);}File.WriteAllText(Path.Combine(Data,"desktop-settings-self-test.json"),new JavaScriptSerializer().Serialize(new {model=f.modelBox.SelectedItem,effort=f.effortBox.SelectedItem,model_count=f.models.Count,channels=(int)f.channelsBox.Value,channels_min=(int)f.channelsBox.Minimum,channels_max=(int)f.channelsBox.Maximum,settings_button_enabled=f.applyButton.Enabled,settings_controller_connected=true}));f.exiting=true;f.Close();}return;}catch{Environment.Exit(1);}}
  bool first;single=new Mutex(true,"Local\\CodexWorker8788-Desktop",out first);activate=new EventWaitHandle(false,EventResetMode.AutoReset,"Local\\CodexWorker8788-Show");if(!first){activate.Set();return;}
  Application.EnableVisualStyles();Application.SetCompatibleTextRenderingDefault(false);Application.Run(new Worker8788(Array.IndexOf(args,"--tray")>=0));GC.KeepAlive(single);
 }
}
