# Windows 桌面 EXE

本机桌面入口：`C:\Users\Administrator\Desktop\Codex 服务启动器.exe`。

登录服务器桌面后双击，窗口显示“服务运行中”即表示已完成带令牌的就绪检查。关闭窗口后服务继续后台运行。重复打开会检查已有实例。

## 2026-11-11 沙箱失效检测与修复

运行工具修复补充：固定的 `.worker/task_tools.py` 负责绝对路径文件读取、UTF-8、指南转换和 CSV 构建。模型只需生成翻译 JSON，不再临时实现 CSV 引号、编码和写出逻辑。用户 Prompt 字节及模型配置保持原样；环境说明通过独立的 `developer_instructions` 提供。

指南预处理运行在与正式任务相同的文件/网络沙箱中，缓存位于受保护的 `data_root/cache/guide-text`。缓存键绑定输入、转换器和工具代码哈希；读取时验证内容哈希，损坏时重新生成。`preparation.json` 记录准备耗时及命中情况。

原生自检新增绝对路径助手、中文输出和 CSV 保真探针；候选术语中的有空格斜线被识别为候选项，普通 `输入/输出` 不被拆开。运行文件哈希按文件身份及时间/大小缓存，文件变化后仍会重新计算并使旧自检回执失效。

HTTP 访问审计从本次部署后开始记录：`http-access.jsonl`，单文件 5 MiB、保留 2 个备份。只保存时间、方法、路由模板、来源 IP、状态、耗时及合法 job ID，不保存鉴权头、正文、查询参数或未知原始路径。

后续更新为“自动验证与恢复版”：

- 每次双击都触发同一条串行检查链路；托盘已存在时也会重新检查，检查中重复双击不会启动第二个 Worker。
- 健康且空闲时，以服务账户执行 `probe` 的真实沙箱检查，验证文件/网络隔离与子进程清理，同时保留现有 Worker PID。探针不调用模型、不改写历史自检回执。
- 沙箱失效时自动执行空闲修复，不必再手动输入管理员密码；最多重试两次。托盘运行中发现账户被重置时也会尝试自动恢复，失败后按一分钟间隔重试，避免无限循环。
- 有活动任务时不停止服务；健康时复用，需修复时明确显示“已延后”，保留任务。
- 仅排队、尚未执行的任务会保留在数据库中，修复后继续派发；不会因排队而永远无法修复。
- `desktop-ensure.log` 和每次独立的 `operation_id` 用于确认本轮检查完成，避免刚双击就误用上一轮的“ready”结果。
- `--ensure-to <报告路径>` 执行与双击相同的检查/恢复；`--probe-to` 仍是只读的就绪检查。

- 托盘启动器现在实时调用带鉴权的 `/v1/ready`。仅进程存在或 `/healthz` 存活不再显示“已就绪”。
- 完整自检回执增加沙箱 marker 哈希、账户 SID 和由公开 password-age 推算的密码设置时间；不保存密码。账户重置、重建或 marker 变化时立即返回 `SANDBOX_STATE_CHANGED`，拒绝接受新任务。
- “修复沙箱”按钮触发 `CodexRemoteWorker-RepairSandbox` 管理任务。先阻止新提交并检查活动任务；有排队/执行中任务就拒绝修复，空闲时才停止已验证身份的 Worker，重新预置沙箱，验证文件/网络隔离和子进程回收，再启动 API。
- 管理任务使用安装时授权的管理员身份；Worker 本身仍由非管理员专用账户运行。没有关闭 UAC 或沙箱，也没有更改模型配置。
- `desktop-repair.log` 记录修复结果。`--probe-to <报告绝对路径>` 输出 `running`、`ready` 和 `code`，不输出 Token。

该版本 Codex 的 legacy elevated 沙箱在整台机器共享 `CodexSandboxOffline/Online`。另一个 Windows 账户重新预置 Codex 仍可能重置这些账户；本补丁提供及时检测和空闲修复，不能宣称消除了上游共享账户限制。长期无人值守部署应让 Worker 独占一台 Windows 主机/虚拟机，或迁移到已验证支持 MXC 的环境。本机 Windows Server 2025 的原生 MXC 探针失败，不能直接切换。

后台服务已使用 PyInstaller 打包到 `C:\CodexRemoteWorker\bin\CodexRemoteWorker\CodexRemoteWorker.exe`。整个目录及 `_internal` 依赖目录需要保留。
桌面启动器使用 .NET Framework Windows Forms 构建，通过按需任务 `CodexRemoteWorker-DesktopStart` 启动本机服务。该任务没有开机触发器，符合登录后手动双击启动的方式。

## 当前服务器安装文件

- `C:\CodexRemoteWorker\scripts\desktop-start.ps1`：启动、自检和 API 就绪确认。
- `C:\CodexRemoteWorker\.runtime\start-worker.local.ps1`：专用账户、沙箱初始化、单实例保护及后台 EXE 启动。
- `C:\CodexRemoteWorker\.runtime\powershell`：独立的 PowerShell 运行时。
- `C:\CodexRemoteWorker\desktop-status.json`：供启动器读取的脱敏状态。
- `C:\CodexWorkerData\desktop-start.log`：启动日志。
- `C:\CodexWorkerData\worker.local.doctor.stdout.log`：原生沙箱自检结果。
- `C:\CodexWorkerData\worker.local.serve.stderr.log`：后台服务日志。

本机原有 Codex、任务工具 Python、LibreOffice、配置和受保护凭据继续按部署目录使用。EXE 不包含密钥。本次交付针对当前服务器，不是复制单个桌面文件即可在另一台电脑部署的便携安装包。

## 重新构建

在 Windows 项目根目录执行：

```powershell
& .\.runtime\python313\python.exe -m pip install 'pyinstaller==6.22.3'
& .\scripts\build-desktop.ps1
```

输出为 `dist\windows-exe\CodexWorkerLauncher.exe` 和 `dist\windows-exe\CodexRemoteWorker\`。
`packaging\worker_entry.py` 在冻结环境下恢复标准 DLL 搜索，防止干扰外部 Codex、Python 和 LibreOffice。
`--startup-trace` 可用于命令行入口的脱敏异常诊断，默认关闭。

配置、private 目录、运行数据和日志均不能放入分发包。

## 2026-11-09 实机验证

- 已生成并部署真正的后台 EXE，正式运行账户为 CodexWorkerSvc。
- 打包版在独立目录和正式目录的原生自检均通过；未绕过文件、网络及子进程回收检查。
- 桌面启动链路已从停止状态启动后台 EXE；服务监听 8787，公网健康检查曾返回 200，带令牌 ready 返回 true。
- 最终桌面界面已实际检查，显示“服务运行中”和 API 地址。
- 当前已有进程时的桌面检查实测约 2 秒；重复打开退出码为 0，后台进程身份保持不变。
- 实测过程中发现已有外部提交的翻译任务，因此保留正在工作的后台进程，后续只更新启动器。
- 启动请求采用异步触发，避免同步等待 schtasks 输出；本机就绪检查直连并使用较长超时，防止短间隔请求排队。
