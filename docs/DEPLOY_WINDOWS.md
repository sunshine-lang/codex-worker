# Windows 部署与运行

本部署包供**另一台 Windows 服务器**使用。公司 Langflow 服务器不安装 Codex，个人电脑不承担正式任务。以下命令尚未在用户的目标 Windows 上执行。

0.2 起直接沿用 Windows 上已有的 Codex 配置，不再由 Langflow 传模型 ID。已部署 0.1 的用户先阅读根目录 [操作指南的升级步骤](../操作指南.md)。

## 1. 准备独立运行账户和目录

- 核实 Windows 版本、架构及公司管理策略。Windows Server 的具体版本需要实际验证，不能由 Windows 11 支持情况推断。
- 使用专用非管理员运行账户，例如 `DOMAIN\svc_codex`。管理员仅负责安装、服务注册和需要的沙箱预置。
- 示例代码目录：`C:\CodexRemoteWorker`；数据目录：`D:\CodexWorkerData`。数据必须在本地磁盘，不使用 SMB/映射网络盘。
- 为数据目录设置 ACL，仅服务账户、管理员和 SYSTEM 可访问。不要对已有公司共享目录批量修改权限。
- 代码和工具目录只需服务账户读取/执行；任务数据目录需要服务账户写入。

## 2. 安装 Python 应用及工具

将仓库复制到代码目录，用公司允许的 Python 3.12 或更新兼容版本运行：

```powershell
.\scripts\install.ps1 -Python "C:\Python312\python.exe"
```

该脚本创建独立虚拟环境并安装应用；不会自动安装或升级 Codex，也不会调用模型。

准备固定版本的原生 `codex.exe`。Spec 的对照版本为 `0.166.0-alpha.5.1`；若目标 Windows 或网关不支持该版本，记录原因并确认后再调整，不自动换版本。使用官方发布渠道，配置指向真正的 `.exe`，不指向 npm 的 `.cmd` 包装脚本。

安装公司批准的 LibreOffice，记录 `soffice.exe` 的路径。应用会安装 `openpyxl`、`python-docx` 和 XML 保护依赖；Legacy DOC 的实际读取仍须用真实样例验收。

## 3. 部署配置

复制 `config.example.json` 为 `config.local.json`，修改：

| 字段 | 填写内容 |
| --- | --- |
| `data_root` | 独立的本地数据目录 |
| `codex_executable` | 原生 Codex EXE 绝对路径 |
| `codex_home` | 同一服务账户已有的 Codex 配置目录；省略时依次使用进程 `CODEX_HOME`、该账户的 `~/.codex` |
| `expected_codex_version` | 经确认的固定版本 |
| `tool_python` | 本应用虚拟环境的 `Scripts\python.exe` |
| `doc_converter` | LibreOffice `soffice.exe` |
| `tool_read_roots` | 工具所需的 Python 基础安装目录、虚拟环境、Codex 包与 LibreOffice 安装目录 |
| `windows_sandbox` | 经管理员预置并验证的 `elevated` 或 `mxc` |

`tool_read_roots` 不能设置成磁盘根目录或任务数据目录。遗漏工具目录时，沙箱自检会失败；补充所需的具体安装目录，而不是放宽成全磁盘读取。

`codex_home` 只指向已存在的目录，例如 `C:\Users\svc_codex\.codex`。不要把另一个 Windows 用户的配置路径误当作当前服务账户已获得其系统凭据访问权。Worker 不创建空白 Codex 配置，也不会把配置/认证复制进任务目录。

模型、provider、推理强度与搜索由 `config.toml` 决定；认证由 Codex 原有文件、系统凭据存储或 provider 环境变量提供。这里沿用默认配置，不复制桌面某个对话的临时选择。服务仍强制任务工作区、执行时限、审批和 shell 文件访问范围；配置中的 MCP/插件应由服务管理员检查其独立权限。

`run.json` 记录配置来源及哈希，`configured_model` / `configured_provider` 表示文件中的值；不能由这些字段推断已经验证了实际的上游路由。

## 4. 配置凭据

可由公司密钥系统注入进程环境变量：

- `CODEX_WORKER_SERVICE_TOKEN`：至少 32 字符的随机服务访问令牌，供 Langflow 调用。
- Codex provider 引用的环境变量：只有使用环境变量认证时才需要。例如既有 provider 引用 `OPENROUTER_API_KEY`，就让运行 Worker 的同一个账户/进程保留该变量。

后台 Windows 服务还支持一个受保护的凭据 JSON 文件，**必须位于 `data_root\private` 内**，例如 `D:\CodexWorkerData\private\credentials.local.json`。至少填写 `CODEX_WORKER_SERVICE_TOKEN`，需要时再补充 Codex provider 引用的凭据变量；进程中已有的对应变量优先。不再强制填写 OpenRouter Key。不要把密钥写进公开配置、命令行、注册表参数、Git 或 Prompt。

管理员可用受控编辑器或密钥系统填入凭据文件。设置仅服务账户、SYSTEM 和管理员可读的 ACL。文件路径会被登记在服务配置中，实际密钥内容不会写入注册表。

Codex 推理进程会取得所需 provider 凭据，不取得 Worker 服务令牌。shell 工具子进程采用空环境加白名单，排除 provider 凭据；任务命令不能读取 Codex 配置目录。这些措施仍需在实际服务账户下验证，不替代多租户安全设计。

## 5. 不调用模型的自检

首先由管理员按官方文档完成该服务账户的 Windows 沙箱预置，使用 `codex_home` 指定的现有目录。0.2 不再强制把状态放到 `data_root\private\codex_home`。

在**实际的非管理员运行账户**下执行：

```powershell
.\.venv\Scripts\python.exe -m codex_remote_worker doctor `
  --config .\config.local.json `
  --credentials-file D:\CodexWorkerData\private\credentials.local.json `
  --verify-sandbox
```

自检使用固定程序，检查：CLI 版本、文档处理依赖、工作区写入、禁止读取/改写控制区、相邻文件和 Codex 配置目录中的合成探测文件、禁止命令网络访问，以及取消后的沙箱子进程回收。探测文件自动清理，不改写真实的 Codex 配置。它不发送翻译任务、不调用模型。

进程管理使用服务生命周期 Job Object 和每任务 Job Object 两层所有权：任务进程先暂停创建、登记后再运行；服务进程意外退出时，外层容器覆盖创建和登记之间的崩溃窗口。此机制与目标机器已有的 Job Object/沙箱约束能否共同运行，必须通过 Windows 专用测试确认。

成功后写入 `data_root\preflight.json`。记录绑定账户 SID、OS、运行文件哈希、Worker 配置和 Codex `config.toml` 哈希；这些内容变化后应重新自检。已提交任务的 Codex 配置发生变化时，该任务明确失败，不静默使用新的模型配置。

生产运行使用名为 `translation` 的权限配置，语义为“工作区可写、必要工具可读、其他路径不可读、命令网络关闭”。**不同时传 `--sandbox`**，因为旧参数会覆盖细粒度权限配置。模型推理和供应商提供的搜索不等同于任务命令的网络访问。

若自检失败，不会通过自动关闭沙箱绕过。先核实 Windows 版本、服务账户、Codex 版本和工具路径。当前版本不支持目标环境时应记录为部署未通过。

## 6. 前台验证与后台常驻

先以前台方式验证相同账户的运行：

```powershell
.\scripts\start-worker.ps1 `
  -Config .\config.local.json `
  -CredentialsFile D:\CodexWorkerData\private\credentials.local.json
```

`GET /healthz` 只说明 HTTP 进程存活。带服务令牌访问 `GET /v1/ready`，才能检查本地配置和自检记录；`ready=true` 仍不证明 OpenRouter 模型已通过实跑。

需要开机后台运行时，由管理员注册服务：

```powershell
.\scripts\install-service.ps1 `
  -Config .\config.local.json `
  -CredentialsFile D:\CodexWorkerData\private\credentials.local.json
```

脚本只注册，不自动启动。打开 `services.msc`，找到 `Codex Remote Translation Worker`，把“登录”账户设为上述非管理员账户并配置公司要求的服务登录权限。不要以 LocalSystem/管理员账户运行翻译。

确认该账户的自检通过后再启动服务。检查 `ready`、事件日志、停止服务和重启后的行为。pywin32 服务宿主和 DLL 加载属于原生部署验收项；若宿主无法加载，不把前台成功当作后台已成功。

服务配置只允许一个调度实例。数据目录锁会拒绝第二个实例；不要用多个 Uvicorn worker 共享同一数据库。

## 7. 公司网络接入

默认地址是 `127.0.0.1:8787`。通过公司允许的 TLS 反向代理或受保护私网连接提供入口，限制调用方为公司 Langflow。不要把无鉴权端口直接暴露到公网。

反向代理需要保留 `Authorization`、`Idempotency-Key` 和 multipart 文件内容。普通提交接口仅完成资料保存并返回任务号，不保持整个翻译时长的 HTTP 请求。

## 8. 首次真实试跑

从 Langflow 提交 Siemens 四份资料，无需再填写模型。确认远端实际沿用了既有 Codex 配置，检查工具调用、CSV 128 行覆盖、原字段、QA 和执行记录。第一次先使用非敏感样例，尤其要补验证 DOC 读取、权限和脱敏。

## 9. 运维与故障

| 现象 | 检查/处理 |
| --- | --- |
| `NOT_READY` / `PREFLIGHT_REQUIRED` | 在正确账户下重新自检；核对配置和 EXE 哈希变化 |
| `CODEX_HOME_MISSING` / `CODEX_CONFIG_INVALID` | 检查 `codex_home` 及同一服务账户的配置访问权限 |
| `CODEX_CONFIG_CHANGED` | 任务提交后配置变化；确认配置、自检后用新版流程重新提交 |
| `LEGACY_TASK` | 旧版任务带有模型参数，不会自动换成服务端默认模型；用新版流程重新提交 |
| `EXCESSIVE_PRIVILEGE` | 改为专用非管理员运行账户 |
| `PROCESS_CLEANUP_FAILED` | 派发已停止；核查受控 Job Object/进程，不按一个旧 PID 随意杀进程 |
| `TIMEOUT` | 查看已保存的 Trace 与最终状态；重新翻译必须使用新提交编号 |
| `IDEMPOTENCY_CONFLICT` | 原编号对应的文件或参数不同；恢复应使用原输入，主动新任务才换编号 |
| 任务完成但下载失败 | 继续查询并重新下载，不重新翻译 |
| 服务重启 | 排队任务可恢复；运行中断任务不自动重跑；已完成文件继续可取回 |

首版不自动清理任务数据。停服务并备份 SQLite 和任务目录后，由管理员按公司保留规则处理终态任务；不要删除运行/排队任务。删除整个任务记录会同时失去该编号的幂等保护，不应作为普通“清理文件”操作。

官方参考：[Codex 非交互模式](https://learn.chatgpt.com/docs/non-interactive-mode)、[权限配置](https://learn.chatgpt.com/docs/permissions)、[Windows 沙箱](https://learn.chatgpt.com/docs/windows/windows-sandbox)、[OpenRouter 接入](https://openrouter.ai/blog/tutorials/codex-cli-openrouter/)。
