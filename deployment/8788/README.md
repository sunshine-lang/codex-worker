# 8788：App Server 复用与四路 ChatGPT 订阅版

此目录对应 2026-11-15 已验证并运行的 8788 部署。应用版本仍为 0.4.0，精确内容以 Git commit 和
[源码 SHA-256 清单](source-manifest.json) 为准。[验证摘要](verification.json) 区分先前实机验收与此次发布测试。

## 行为

- `max_concurrent_jobs` 控制全局并发，`task_batch_concurrency` 控制单个整单的批次并发；
  8788 入口要求 `1 <= task_batch_concurrency <= max_concurrent_jobs <= 4`。
- 每条通道拥有独立 App Server 和 Windows Job Object，通信使用私有 stdio。
  每个批次创建独立 thread；第五个批次等待空闲通道，不另开第五路。
- `thread_unload_delay_secs=0`。`thread/unsubscribe` 的确认不等于已经卸载；
  必须收到对应 thread 的 `closed` / `notLoaded`，且 PID 和创建时间集合与启动基线一致，才复用。
  官方打包运行时每通道有两个合法进程，不再以“只能有一个进程”误判。
- 无法确认清理时仅回收所属通道；取消任务不会取消其他通道，也不会自动重放模型请求。
- ChatGPT 登录和 Worker HTTP Token 是独立凭据。订阅模式不注入原 Provider 的环境凭据，
  模型配置为 `gpt-6-astra`、`openai`、`xhigh`。
- 宿主固定程序读取完整指南和批次上下文，模型只返回结构化 JSON；模型 Shell、文件系统、
  网页、插件、Apps、MCP 环境均关闭。仍检查行号、原结构、标记、100% TM 和产物哈希。
- Windows 临时访问/共享冲突采用有界目录重命名重试，不覆盖既有目标。

## 部署布局与依赖

这五个 `.ps1` / `.py` 文件是已部署入口的字节副本，含固定服务器路径，**不是全新机器的一键安装器**。
本次推送没有运行安装、注册、启动或停止命令，也没有更改在线服务。

| 位置 | 用途 |
| --- | --- |
| `C:\CodexRemoteWorker-AppServer-8788` | 本分支的 `src`、五个入口文件及本地 `config.trial.json` |
| `C:\CodexWorkerData-AppServer-8788` | 独立队列、任务、日志和状态目录 |
| `C:\CodexWorkerData-AppServer-8788\private\codex-home` | 8788 专属模型配置和 ChatGPT 登录状态 |
| `C:\CodexRemoteWorker` | 现有基线安装；只读获取运行时路径、服务账户及 HTTP 鉴权配置 |

运行账户为非管理员专用 Windows 账户。该账户须已注册官方 Codex Windows 包；
验证时包版本为 `26.1111.2228.0`，CLI 为 `0.169.0-alpha.11.1`。
需要基线的 Python 3.13 运行时、依赖和文档转换工具，以及 PowerShell 7 启动器环境。
启动脚本使用 PowerShell 7 的 `Start-Process -Environment`，随后通过系统 Windows PowerShell
执行 `Invoke-CommandInDesktopPackage` 获得官方包身份。

`config.trial.example.json` 展示实际部署参数，但不含秘密；应用部署时将它作为本地 `config.trial.json` 使用。
`codex.config.example.toml` 只展示模型设置。登录状态必须在目标环境自行准备，私有目录权限应仅允许
服务账户、Administrators 和 SYSTEM。本仓库不包含登录状态或账户密码。

应用入口 `app_server_service.trial_settings()` 只读取基线配置和凭据，并强制使用独立数据目录和端口 8788；
它不会写回基线。不能通过修改普通 `config.example.json` 然后使用旧 EXE 来启用此分支的四路 App Server。
新机器还需配置本机和云端入站网络，并验证其自身环境；本次没有验证全新机器安装。

## 已配置环境的检查和启动

以下是维护人员在完成上述前提后使用的入口示例；不要在已有监听时重复启动。
启动器检测到 8788 已占用会报错，不会停止占用进程。

```powershell
pwsh -NoProfile -File C:\CodexRemoteWorker-AppServer-8788\start-trial-packaged.ps1 -Mode IdentityProbe
pwsh -NoProfile -File C:\CodexRemoteWorker-AppServer-8788\start-trial-packaged.ps1 -Mode ProtocolProbe
pwsh -NoProfile -File C:\CodexRemoteWorker-AppServer-8788\start-trial-packaged.ps1 -Mode Serve
Invoke-RestMethod http://127.0.0.1:8788/healthz
Get-NetTCPConnection -LocalPort 8788 -State Listen
```

身份/协议探测不发起模型轮次。`Serve` 返回启动进程记录；仍需检查鉴权后的 readiness/capabilities 和真实任务，
不能仅用 `/healthz` 判断模型可调用。Job/task API、查询、取消和下载路径延续已有接口。

## 已有验证与限制

2026-11-15 11:45 UTC 的部署验收记录：

- 常规测试 360 passed、7 skipped、4 deselected；两个原生 Windows 进程通信/隔离测试通过。
- 129 条合成模型翻译加 1 条 100% TM，分五批，观察到四路同时执行；第五批复用原 Server。
  四条通道的 generation 均保持 1，正常批次清理约 5～14 毫秒。
- 另一次四路验收取消其中一项，其余三项完成且保留原进程；取消任务没有输出混入其他任务。
- 实际 ChatGPT 登录调用成功。旧字段 `upstream_model_verified=false` 表示未独立验证提供方内部路由，
  不表示没有执行真实模型调用。
- 当时 8787 的安装与配置基线保持一致；此次 Git 发布不涉及任何服务操作。

这些是合成验收，不代表 1344 条真实业务数据的耗时或语义质量。用户从上游重跑的结果不在本次发布结论内。
整单结果的 `review_status=not_started`，不会自动承诺完成独立语义审校。

## 本地回归

```powershell
python -m pip install -e ".[test]"
python -X utf8 -m pytest -q -m "not windows"
python -X utf8 -m pytest -q tests/test_app_server_adapter.py tests/test_app_server_pool.py -m windows
```

后一条仅执行固定的无模型 RPC/进程测试。部署源码清单不含认证文件哈希；任务、日志、数据库和第三方运行时不发布。
