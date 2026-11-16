Codex Worker 0.4.0 免安装迁移版
=============================

一、最快使用
1. 把整个 CodexWorker-Portable 文件夹复制到目标 Windows x64 电脑的本地磁盘，或完整解压 ZIP。
   建议放在 C:\CodexWorker-Portable 或 D:\CodexWorker-Portable；不要直接在压缩包中运行。
2. 使用目标机已登录 Codex 的同一个 Windows 用户，先确认 Codex 能正常执行一个带工具的本地任务。
3. 普通双击 CodexWorker.exe（也可双击 start.cmd 或“启动服务.cmd”），不要以管理员身份运行。
4. 等待显示“服务已就绪”。首次会检测本机现有 Codex、只读检查登录，并验证沙箱和 JSON 工具。
5. 打开 Data\private\connection.txt，取得新生成的 Worker Token 和本机检查地址。
6. 调用方改用目标电脑的 IP/域名/穿透地址和这个新 Token，先提交一条小翻译并下载结果。

本包不需要安装器、pip 或另装 Python/LibreOffice，不附带另一套 Codex。
若系统缺少微软 VC++ 运行库并提示 msvcp140/vcruntime 等 DLL 缺失，备用官方程序位于
prerequisites\vc_redist.x64.exe（已检查 Microsoft 数字签名）；仅按需手动安装/修复，不会自动运行。
只识别并调用目标机的原生 Codex CLI。只安装桌面 GUI 但没有可调用 CLI 时会明确提示。
服务就绪检查不调用模型，因此不能代替一次真实翻译；实际模型权限、订阅额度、延迟以目标机为准。

二、对外访问：只用 TCP 8787 一个端口
默认监听 0.0.0.0:8787，允许通过目标电脑的实际 IP 访问；本机检查地址为 http://127.0.0.1:8787。
0.0.0.0 是监听配置，不是调用方可填写的地址；127.0.0.1 也不能供另一台电脑访问。

已有内网穿透：转发到 127.0.0.1:8787，调用方使用穿透的访问地址。
如果只通过同机穿透客户端访问，可在 worker-settings.json 把 listen_address 改为 127.0.0.1。
如果直接通过 IP 访问，需要目标机 Windows 防火墙、服务器安全组/NAT 放行 TCP 8787。
本包不自动修改防火墙、路由、穿透配置或系统服务。公网请使用 HTTPS 入口并限制可信调用来源。

Worker Token 必须保留，HTTP 请求头为 Authorization: Bearer <Worker Token>。
这个 Token 保护翻译服务入口，不是模型 API Key，不要公开分享 connection.txt。
配置 public_api_url 只影响连接信息显示，不负责创建穿透或启用 TLS。

三、文件入口
CodexWorker.exe：本目录双击启动入口，显示启动检查结果；需要保留同目录的所有依赖。
source\CodexWorker-Source.zip：含源码的交付包会附带此文件，用于维护，不需要先安装源码依赖。
start.cmd / 启动服务.cmd：检查环境后后台启动，重复点击复用已有实例。
status.cmd / 查看状态.cmd：带鉴权检查 /v1/ready，不只检查进程或 /healthz。
stop.cmd / 停止服务.cmd：只停止本目录、本用户且身份核实的 Worker；有排队或执行中任务时拒绝停止。
check.cmd / 环境检查.cmd：验证登录、沙箱及工具，但不启动服务、不调用模型。
test-translation.cmd / 运行一条测试翻译.cmd：输入 YES 后才提交一条真实测试并下载结果，使用订阅额度。
examples：不含业务秘密的一条 JSON 翻译、Prompt 和两份风格指南。测试结果保存到 Data\test-result.json。
worker-settings.json：可选修改端口、监听地址、Codex CLI/配置目录、显示用公网地址。
Data\private\connection.txt：首次检查成功后生成的 API/Token 连接信息。
Data\logs：启动、自检及服务日志；失败时把错误代码和相关日志发来，勿发送 Token 或个人登录文件。

Codex 路径和配置目录通常不用填写。自动识别依次尝试 PATH、用户桌面端 CLI 缓存、
当前用户的桌面端包及 npm 原生 CLI；识别不到时再手工填写。
codex_executable 必须指向原生 CLI 的 codex.exe，而不是桌面 GUI 的 Codex.exe、codex.cmd 或快捷方式。
桌面端包中 CLI 通常在 app\resources\codex.exe；以目标机实际路径为准。
codex_home 留空时使用当前环境 CODEX_HOME 或当前用户 .codex。
Windows JSON 路径中的反斜线要写成双反斜线，或使用正斜线。

四、运行条件和边界
- 使用已登录官方 ChatGPT/Codex 的同一 Windows 用户；只读执行 login status，不 login/logout、不复制 auth.json。
- 本版沿用此前要求的官方订阅登录；如果现有 Codex 选择的是第三方/API 提供商，会停止并提示，不静默改模型。
- 不填写模型名称、模型 API 地址或模型 Key；沿用本用户 Codex 全局/所选 profile 的模型配置。
  这不等于跟随桌面端某个聊天临时选中的模型。
- Windows 用户须保持登录，电脑开机、不休眠、网络可用。不是退出登录后仍运行的 Windows 系统服务。
- 需要非提权桌面会话。以管理员身份运行、始终提权的内置 Administrator 会话不受支持。
- 只复用已有沙箱，不初始化/重置共享沙箱账户，不以关闭沙箱、关闭 UAC 或提升 Worker 权限绕过失败。
- 自动记录目标机检测到的 CLI 版本并进行真实原生工具探测，不宣称兼容任意 Codex 版本。
  旧版本不支持必要参数、组织策略不允许隔离或沙箱失效时会拒绝启动，请先在目标 Codex 中解决。
- 不创建 Windows 用户、管理员计划任务、HKCU 卸载项、系统服务或桌面快捷方式。
- Worker 专用配置、Token、任务和日志保存在本便携目录；沙箱自检会在 Codex 配置目录和用户目录
  创建并清理临时边界测试文件。被调用的现有 Codex 仍可能按正常行为更新自身登录缓存及会话文件；
  本启动脚本不直接覆盖个人 config.toml，不执行 login/logout，也不搬运登录文件。
- 模型工具受文件/网络沙箱约束，个人 MCP/应用/插件仅在 Worker 子进程内关闭，不修改个人配置。
  这不等于虚拟机级隔离；请仅供你自己可信的调用方使用。
- LibreOffice 只作为 .doc 指南转换工具，已随包携带，不注册系统安装、不负责翻译。
- 不要删掉 .runtime、bin 内的 _internal 或 tools 目录；看到完整“服务已就绪”才开始调用。

五、版本和业务兼容
在原加固版本上增量升级至 0.4.0：保留 JSON/CSV/XLSX、字段/行 ID 校验、异步任务、状态查询、结果下载。
四份 multipart 输入、Idempotency-Key、options 和原有 /v1/jobs 等协议保留；只改服务地址和 Token 即可尝试。
新增 siemens-json/2、100% TM 程序复用和 Data/projects 项目归档，保留 /1 和原表格协议。
这是适配本机现有官方登录的免安装发行方式，不是旧服务器 EXE 的逐字节副本。
不带旧服务器 DeepSeek/OpenRouter 配置、密钥、运行账户密码、个人登录、历史任务或旧 Token。
本机原来运行的 Worker 不会被这个文件夹替换或停止。

六、常见问题
USER_CONTEXT_REQUIRED：普通双击；换回已登录 Codex 的同一用户的非提权桌面会话。
CODEX_CLI_NOT_FOUND：在 worker-settings.json 中指定 CLI 完整路径，注意不是桌面 GUI。
CHATGPT_LOGIN_REQUIRED：先在同一 Windows 用户下确认官方登录；本包不会要求模型 API Key。
CHATGPT_PROVIDER_REQUIRED：现有 Codex 配置选择第三方提供商，请你自行确认配置；本包不会覆盖它。
PORT_IN_USE：修改 worker-settings.json 的 port，穿透和调用方端口同步调整；不会抢占其他服务。
沙箱探测失败/超时：保存错误和 Data\logs，确认桌面端原生沙箱能运行后重试，不要关闭沙箱绕过。
PREFLIGHT_REQUIRED / SANDBOX_STATE_CHANGED：空闲时先 stop.cmd 再 start.cmd；若仍失败请反馈日志。
CONTROLLER_BUSY：另一个启动/停止/检查窗口还在运行，等待它结束。
JOBS_ACTIVE：任务未完成，先等待或通过原接口取消后再停止。

七、再次迁移与删除
全新机器应使用这里附带的原始 ZIP，或复制不含 Data 的便携目录；不要把已使用的 Data 发给其他人。
Data 含本机路径、新 Worker Token、任务输入输出；不要公开上传整个目录。
先停止服务，再移动便携文件夹；换 Windows 用户或机器不要沿用进程记录和旧配置。
删除本目录前先 stop.cmd；不会卸载或删除系统已有的 Codex、登录或沙箱。

官方认证参考：https://learn.chatgpt.com/docs/auth
真实目标电脑登录、原生沙箱与翻译尚需现场测试；本包的本机验证记录见 verification.json。
