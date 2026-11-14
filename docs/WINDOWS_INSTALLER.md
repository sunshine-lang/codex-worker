# Windows 新机迁移安装包（0.3.0 加固版）

Codex Worker 0.3.0 Windows 新机安装说明
基准：2026-11-12 本机实际运行的 0.3.0 JSON 加固版。

一、复制什么
只需将 CodexWorkerSetup-0.3.0.exe 复制到目标 Windows 服务器。
安装 EXE 已包含 Worker、Codex CLI 0.167.0-alpha.3.2、Python、PowerShell、LibreOffice、C++ 运行库安装文件和桌面启动器。
无需另外安装 Codex 桌面版或先手工安装 Python/LibreOffice。
安装包不携带旧服务器的模型 API Key、访问 Token、Windows 密码、沙箱状态或历史 job。

二、目标环境
Windows x64，带桌面环境；优先使用与基准机相同的 Windows Server 2025。
需要管理员安装权限、足够的本地磁盘空间（建议至少 6 GB，另为业务文件留空间），以及连接模型服务的网络。
其他 Windows x64 的兼容性以目标机器的实际初始化和完整自检为准；不能保证所有旧版 Windows 或受限域策略均可使用。
请在以后用于双击启动器的 Windows 用户下安装。当前是登录后按需双击启动，不是无人登录开机自动启动的 Windows Service。

三、安装
1. 双击安装 EXE，在 Windows UAC 中允许管理员权限。
2. 默认安装目录为 Program Files 下的 CodeX Worker；可选新的受保护本地目录，不能覆盖已有部署。
3. 默认 API 端口 8787。允许远程访问时会创建本机防火墙规则；云平台安全组仍需自行配置，建议限制为调用方 IP/私网并使用 HTTPS 入口。
4. 模型 API 地址默认 https://openrouter.ai/api/v1；模型默认 deepseek/deepseek-v4.1-flash-1017，推理强度 high。
5. 在新机填写你有权限使用的模型 API Key。这不是 Windows/RDP/3389 密码，也不是 Worker 的访问 Token。
   Key 可留空先验证本机环境，但未配置正确 Key 前不能完成真实翻译。
6. 点击安装。程序自动创建本机专用非管理员账户、初始化沙箱、设置网络隔离，执行完整沙箱检查和 JSON 工具检查；不会调用翻译模型。
7. 自检通过后生成全新的 Worker Token，并启动 API。完成页可复制连接信息或打开桌面启动器。

四、日常启动
安装后使用桌面“CodeX 服务启动器”入口。
每次双击会检查服务和沙箱；正常时复用已有进程，空闲沙箱失效时尝试自动恢复。有运行中任务时不会为修复强制停止任务。
关闭窗口会隐藏到托盘；托盘“退出并停止服务”才会真正停止。
日常无需输入专用运行账户密码，账户和权限由安装器建立。不要把旧服务器的账户密码或 preflight.json 复制过来。
同一机器的其他 Codex 初始化仍可能重置共享沙箱账户；建议专用主机/虚拟机，保留本版检测与恢复机制。

五、确认可用
/v1/ready（带新 Token）应返回 ready=true、service_version=0.3.0，以及 siemens-json/1、table-csv/1。
先完成一条真实翻译和结果下载测试，再将上游服务地址与 Token 切换到新机。
当前 JSON 输入输出、CSV/XLSX 兼容、审计、指南缓存、沙箱修复均随现用版本保留；Langflow 的翻译/审校编排仍留在调用方，不在此安装包内。
旧服务器无需先卸载。切换前暂停旧入口提交并等待运行中 job 结束，避免两台同时重复执行同一业务请求。
本包新建空任务数据库，不自动迁移历史 job；如需历史记录，应另行做一致性备份和数据迁移。

六、凭据和故障定位
新 Token：安装目录\Data\private\service-token.txt。
新模型 Key、专用账户凭据均存入受保护的 private 目录，不写入分发包或命令行。
日志目录：安装目录\Data；重点看 install.log、sandbox-setup.stderr.log、worker.local.doctor.stderr.log、worker.local.json-probe.stderr.log、desktop-ensure.log。
失败时不会跳过沙箱或伪造 ready。先保留日志，不要关闭 UAC/沙箱或修改系统账户来强行启动。
Windows 可能对未签名的安装器显示未知发布者提示；请核对 SHA256SUMS.txt 和文件来源，并按组织的应用安装策略处理。
若检测已有 Worker，安装器会拒绝覆盖；不要在当前生产服务器上用此新机安装包重复安装。

七、验证边界
已在本机做回归测试、安装器完整性/损坏包/非法路径检查，并验证新目录（中文与空格路径）中的解包和运行组件。
没有另一台干净 Windows 服务器可做完整安装实测；目标环境是否支持实际沙箱，必须以安装时的完整自检及你的真实翻译调用为准。
本次制作安装包未停止、替换或修改当前正在运行的 Worker。
