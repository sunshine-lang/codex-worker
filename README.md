# Codex Remote Worker

**本分支发布 2026-11-15 已部署到 8788 的 0.4.0 源码：App Server 复用、ChatGPT 订阅登录、最多四路并发。**

当前版本号仍为 0.4.0；该号下包含后续整单任务接口、输入上下文分页等增量。具体代码以 Git commit 和
[8788 部署校验清单](deployment/8788/source-manifest.json) 为准，而不只看版本号。

分支：`codex/worker-8788-chatgpt-4way`，基于远端 `main` 的 `207e1b4`。
部署入口、配置示例和验证范围见 [8788 部署说明](deployment/8788/README.md)。
本次发布只整理并推送源码，没有重启或修改服务器上的 8787、8788。

## 已核对的当前能力

- `/v1/jobs`：原四文件单批任务、鉴权、幂等键、查询、取消与产物下载。
- `siemens-json/2`：项目标识、100% TM 锁定复用、纯 TM 零模型调用及安全归档；兼容 `/1`、CSV/XLSX。
- `/v1/tasks`：整单接收、服务端预算分批、子 job 管理、恢复和原结构结果下载（`translation-task/1`）。
- `utf8-absolute-tools-v4-bounded-context`：按字节预算分页读取完整指南及待处理记录，保留原全局行号。
- Windows 进程树清理、文件/网络隔离自检、共享沙箱状态检测、缓存、日志脱敏和访问审计。
- 原服务器专用账户部署、桌面当前用户订阅版、免安装启动器，以及构建/维护脚本。
- 8788 使用四个独立常驻 App Server，通过私有 stdio 通信；全局 job 和同一整单批次均支持 1～4 路。
- 每批独立 thread，确认卸载和运行时进程身份一致后复用 Server；异常回收及取消只作用于所属通道。
- 8788 使用独立的 ChatGPT 登录目录，配置为 `gpt-6-astra / xhigh`；模型仅接收完整文本上下文并返回结构化 JSON。

整单接口当前生成完整候选译文，`review_status=not_started`，不自动承诺完成独立语义审校。
单批 `translate/review` 及上游业务编排保持各自边界；程序交付检查不等于语言质量保证。

## 这是当前在用的代码吗？

发布时，`src/codex_remote_worker/` 的 28 个 Python 模块与 8788 已安装源码逐文件 SHA-256 一致。
`deployment/8788/` 保存该服务的五个入口/启动脚本字节副本、无凭据配置示例和验证摘要。
当前 8788 通过 Python 源码启动，不使用旧桌面 EXE 作为 Worker 入口。

`deployment/current-runtime/` 保留的是 2026-11-14 基线部署资料，其旧源码/EXE 清单不代表本分支的 8788。

## 目录

- `src/codex_remote_worker/`：完整应用源码。
- `tests/`：接口、任务恢复、TM、归档、上下文、启动器和安全测试。
- `packaging/`：所有启动器 C# 源码、manifest、安装/便携/沙箱相关脚本。
- `scripts/`：构建、配置、安装、部署及维护脚本。
- `integrations/langflow/`：项目现有 Langflow 集成源码与流程导出。
- `deployment/8788/`：当前分支对应的 8788 部署入口、配置示例和源码校验清单。
- `deployment/current-runtime/`：2026-11-14 基线部署资料，供历史核对。
- `docs/`：当前功能说明与历史部署资料。
- `third_party/`、`NOTICE.md`：相关第三方声明。

`*.local.py/ps1` 与 deployment 中的脚本包含特定服务器路径，有些会清理任务或修改沙箱；**请先审查，不要直接执行**。
历史文档可能写有 0.2.x/0.3.0 或旧交付路径，不代表当前部署版本；原 README 保留于 docs/history。

## 文档入口

- [8788 四路订阅版部署与验证](deployment/8788/README.md)
- [整单接口](docs/WHOLE_TASK_API.md)
- [有预算的完整输入读取](docs/BOUNDED_CONTEXT.md)
- [TM 与项目归档](docs/TM_PROJECT_DELIVERY.md)
- [免安装运行条件](packaging/portable/README.zh-CN.txt)
- [桌面订阅版](docs/DESKTOP_SUBSCRIPTION.md)
- [Windows 部署](docs/DEPLOY_WINDOWS.md)

## 开发测试与构建

```powershell
python -m pip install -e ".[test]"
python -X utf8 -m pytest -q -m "not windows"
```

普通测试用模拟 runner，不需要真实模型。带 windows 标记的原生进程测试需在相应 Windows 测试环境另行运行。
后台 EXE 构建脚本为 scripts/build-desktop.ps1，需要准备 Python、PyInstaller 和微软 C# 编译器。
构建便携包/安装器还需要合法取得的离线运行组件和构建 stage；它们不是本仓库源码的一部分。

## 明确未上传

不上传任何生产 Token、API Key、Windows 密码、GitHub/AWS 凭据、Codex 登录或沙箱秘密状态，
也不上传真实任务数据、数据库、日志、预处理缓存和第三方二进制运行时（Python/PowerShell/Codex/LibreOffice）。
这里只发布完整的本项目自有代码、示例配置、构建文件及部署校验资料，不会通过推送重启或替换现有服务。
