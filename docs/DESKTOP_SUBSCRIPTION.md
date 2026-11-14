# 桌面官方订阅版

Codex Worker 0.3.0 桌面官方订阅版
本版替代需要填写 OpenRouter 模型名称、地址和 API Key 的迁移方案。

一、使用方式
1. 目标电脑先安装 Codex 桌面端，并使用有 Codex 权限的官方 ChatGPT 账号登录。
2. 在同一个 Windows 用户的普通桌面会话中，双击 CodexWorkerSetup-0.3.0-Desktop.exe。
   不要右键“以管理员身份运行”，也不要从管理员终端启动。
3. 选择本用户安装目录（默认 LocalAppData 下的 Codex Worker）和端口，默认 8787。
4. Codex 配置目录会自动指向该用户的 CODEX_HOME 或 .codex；有自定义目录时请选择桌面端实际使用的目录。
5. 可填写内网穿透访问地址，仅用于展示和复制；本机服务固定监听 127.0.0.1，不开放局域网端口。
6. 安装器只读检查该用户现有的官方登录，复用原登录缓存/系统凭据存储。不复制 auth.json、不另建登录、不要求重新授权。
7. 验证已有沙箱、个人目录隔离与 JSON 工具后启动服务。登录或沙箱不通过会明确提示，可处理后重试。
8. 用新生成的 Worker Token 配置调用方。首次先提交一条真实翻译并下载结果，确认目标电脑的订阅权限和额度可用。

二、模型与登录
没有模型名称、模型地址和模型 API Key 输入框，不写入自定义提供商或强制 DeepSeek。
模型遵循此用户的 Codex 全局配置；未指定时使用包内 CLI 默认选择，不等同于镜像桌面端某个会话的临时模型选择。
调用消耗当前账号的 Codex 订阅额度，受官方套餐、组织权限、用量和速率限制；不是无限额度的 API 服务。
如果原配置仍指向第三方提供商/API Key，或者包内 CLI 无法识别当前桌面登录，会停止而不是偷偷改配置或改用 API Key。
只运行 codex login status 检查，不执行 login/logout，不改桌面端 config.toml。
桌面端 Codex 不必一直开着窗口，但需要该 Windows 用户保持登录、电脑开机且网络可用。

三、安全和侵入性
本版按当前用户安装运行，不创建 Windows 运行账户，不注册管理员计划任务，不修改防火墙或 WFP 规则，不自动初始化或重置共享沙箱。
复用桌面端已有沙箱模式；模式不兼容或隔离验证失败时停止，不通过关闭沙箱或提升 Worker 权限来绕过。
程序会写本用户安装目录、桌面快捷方式和 HKCU 卸载信息；必要的沙箱验证会创建并删除临时测试文件。
Worker 工具显式禁止读取个人用户目录及常见个人文件夹，并单独保护 Codex 登录目录和其他任务目录；这不等于虚拟机级隔离。
Worker 进程显式关闭继承到的个人 MCP、应用和插件集成，不改桌面端这些集成的配置。组织强制策略若不允许关闭，可能导致检查不通过。
Worker Token 必须保留，它不是模型 API Key，而是保护内网穿透任务入口的访问凭据；勿分享给不可信用户或作为公开代理服务。
只给你自己可信的调用方使用，穿透入口应启用 HTTPS、鉴权和来源限制。连接信息不再自动保存到桌面，可按需手动保存。
不会把本机、目标电脑或他人的登录凭据打进安装包。卸载只移除本用户 Worker，保留 Data，不删除原 Codex 配置、登录或沙箱。

四、运行组件和兼容性
仍包含 Worker、Python、PowerShell、固定 Codex CLI 0.167.0-alpha.3.2 和 LibreOffice，无需再填模型接口。
翻译输入输出沿用 0.3.0 的 JSON/CSV/XLSX 协议，并增加本版的官方登录检查及个人目录保护；该 EXE 因接入逻辑更新不与旧服务器 EXE 逐字节相同。
建议 Windows x64 桌面环境。在普通会话中安装；不支持在始终提权的内置 Administrator 会话中绕过非管理员检查。
本版不会静默安装或升级系统组件。若系统缺少必需的微软运行库，需按组织规则安装官方运行库后重试。
请勿使用之前那个需要模型 API Key 的服务器版安装器来覆盖本版。

五、检查和日志
完成后 /v1/ready（带 Worker Token）应返回 ready=true、service_version=0.3.0 和 siemens-json/1、table-csv/1。
Token：安装目录\Data\private\service-token.txt；配置：安装目录\config.local.json。
认证检查：Data\worker.local.profile-check.stdout.log，只记录成功/失败类型，不记录 CLI 原始凭据输出。
其他日志：Data\install.log、worker.local.doctor.stderr.log、worker.local.json-probe.stderr.log、desktop-ensure.log。
没有另一台带你的官方订阅的目标电脑可实测，因此安装包不会宣称已验证你的账号、实际模型权限或额度；以目标机检查和真实调用为准。
原服务器仍运行原先配置，本次没有切换其模型或替换其服务。
