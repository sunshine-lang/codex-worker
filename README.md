# Codex Remote Worker

本分支提供独立的项目材料 Worker 和三节点 Langflow 流程。Windows 部署、进度查询和调用示例见 [材料预处理部署说明](packaging/materials/README.md)。

本分支支持一份 JSON 内逐句指定翻译语言对，包含原有的 TM 决策和状态查询。Windows 更新方法见 [按句段语言对翻译](deployments/worker8788/README.md#按句段语言对翻译)。

本分支增加了 v2 订单的按语境复用 TM 策略，并包含已完成动作查询。部署与接口见 [按语境使用 100% TM](deployments/worker8788/README.md#按语境使用-100-tm)。任务状态字段见 [已完成动作查询](deployments/worker8788/README.md#查询任务最近完成的动作)。

**本分支最新同步：2026-11-17 的 8788 整单自主翻译版本。** 当前部署代码位于
[deployments/worker8788/source](deployments/worker8788/source)，桌面程序位于
[packaging/worker8788](packaging/worker8788)。从 [8788 说明](deployments/worker8788/README.md)
进入；该版本包含官方订阅/OpenRouter 配置兼容、同会话自主翻译与自检、版本化进度保存和检查点恢复。
每轮固定条数、工具固定条数和强制分页已取消，读取范围由 Codex 决定。

根目录 `src/` 与下面的说明保留 2026-11-14 基线；不要把它与独立部署的 8788 源码混用。
当前 8788 源码指纹见 [release-manifest.json](deployments/worker8788/release-manifest.json)，
可运行 `python tools/verify_worker8788_snapshot.py` 校验。

**本仓库发布的是 2026-11-14 当前服务器实际使用的 0.4.0 源码，不是之前的 0.3.0 或早期 0.4.0 迁移 ZIP。**

当前版本号仍为 0.4.0；该号下包含后续整单任务接口、输入上下文分页等增量。具体代码以 Git commit 和
[部署校验清单](deployment/current-runtime/source-manifest.json) 为准，而不只看版本号。

## 已核对的当前能力

- `/v1/jobs`：原四文件单批任务、鉴权、幂等键、查询、取消与产物下载。
- `siemens-json/2`：项目标识、100% TM 锁定复用、纯 TM 零模型调用及安全归档；兼容 `/1`、CSV/XLSX。
- `/v1/tasks`：整单接收、服务端预算分批、子 job 管理、恢复和原结构结果下载（`translation-task/1`）。
- `utf8-absolute-tools-v4-bounded-context`：按字节预算分页读取完整指南及待处理记录，保留原全局行号。
- Windows 进程树清理、文件/网络隔离自检、共享沙箱状态检测、缓存、日志脱敏和访问审计。
- 原服务器专用账户部署、桌面当前用户订阅版、免安装启动器，以及构建/维护脚本。

整单接口当前生成完整候选译文，`review_status=not_started`，不自动承诺完成独立语义审校。
单批 `translate/review` 及上游业务编排保持各自边界；程序交付检查不等于语言质量保证。

## 这是当前在用的代码吗？

发布前对比了工作区、已安装 src、工具 Python 包：23 个应用模块 SHA-256 全部一致。
还直接读取已安装的运行 EXE：其中 22 个应用模块和 worker_entry 的标准化 Python 代码对象均与源码一致。
`service.py` 是未被当前桌面启动型 EXE 使用的备用 Windows Service 入口，也完整保留。
`deployment/current-runtime/` 另存实际部署脚本的字节副本。

运行 EXE SHA-256：

```text
d19488c6dfdcfebb69bdbb8c938b3fa4cbbad8697a7199af8ffd57ceadb25f99
```

## 目录

- `src/codex_remote_worker/`：完整应用源码。
- `tests/`：接口、任务恢复、TM、归档、上下文、启动器和安全测试。
- `packaging/`：所有启动器 C# 源码、manifest、安装/便携/沙箱相关脚本。
- `scripts/`：构建、配置、安装、部署及维护脚本。
- `integrations/langflow/`：项目现有 Langflow 集成源码与流程导出。
- `deployment/current-runtime/`：实际部署脚本、模块和运行 EXE 的校验清单。
- `docs/`：当前功能说明与历史部署资料。
- `third_party/`、`NOTICE.md`：相关第三方声明。

`*.local.py/ps1` 与 deployment 中的脚本包含特定服务器路径，有些会清理任务或修改沙箱；**请先审查，不要直接执行**。
历史文档可能写有 0.2.x/0.3.0 或旧交付路径，不代表当前部署版本；原 README 保留于 docs/history。

## 文档入口

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
