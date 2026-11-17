> 本目录是 2026-11-17 部署中的 8788 完整应用源码。当前行为、配置与验证以 [8788 说明](../README.md) 为准；下面保留的是基线项目说明。

# Codex Remote Worker

> 目标机已有 Codex 官方订阅时，请用 **桌面当前用户版**：`dist/desktop-subscription-0.3.0/CodexWorkerSetup-0.3.0-Desktop.exe`。不填写模型/API Key；普通双击安装，不要管理员运行。见 [桌面订阅版说明](docs/DESKTOP_SUBSCRIPTION.md)。原服务器配置不变。

> 当前实际部署基准：**0.3.0 JSON 加固版**（2026-11-12）。支持 JSON 与 CSV/XLSX，保留本机沙箱恢复、运行工具、缓存及审计修复。
> 新 Windows 机器安装包：`dist/migration-0.3.0/CodexWorkerSetup-0.3.0.exe`；参见 [新机安装说明](docs/WINDOWS_INSTALLER.md) 和 [JSON 协议](docs/JSON_DELIVERY.md)。下文 0.2.x 信息为历史说明。

供公司 Langflow 调用的 Windows Codex 翻译任务服务。

当前版本 **0.2.1**：修复 Windows 中文数据快照读取编码，增加宿主错误诊断与已有译文的 `recheck` 交付恢复。Langflow 只提交资料和用户 Prompt；直接沿用 Windows 上 Codex 的既有配置，**不需要填写模型 ID**，不强制 OpenRouter provider。

如果 Codex 已正常完成但 Worker 显示“执行器发生异常”，先看 [0.2.1 恢复步骤](操作指南.md)，可重检并发布原文件，无需再次调用模型。0.2.1 补丁尚未在用户 Windows 实机验收；Langflow 0.2 节点无需重新导入。

0.2 已完成本地模拟验证。用户已升级 Windows Worker，本机 Langflow 原远程节点也已通过 MCP 更新；实际检查确认网络、令牌鉴权、执行环境就绪，远端返回 `service_version=0.2.0`、`configuration_source=server_codex`。本次未执行真实翻译，未操作公司服务器上的 Langflow。

## 从这里开始

- [操作指南](操作指南.md)：开头是 0.1 → 0.2 的升级步骤；后续为安装、试跑和第 12 节的 EXE 打包命令。
- [Windows 部署说明](docs/DEPLOY_WINDOWS.md)：安装、服务账户、密钥、沙箱自检与后台运行。
- [Langflow 接入说明](docs/LANGFLOW.md)：导入流程、配置、提交、查询和取消。
- [可导入流程](integrations/langflow/Codex_Remote_Translation.json)：三个节点，无模型输入；服务地址、令牌和文件绑定为空。
- [验证记录与待验收项](docs/VALIDATION.md)：区分本地通过项和 Windows/OpenRouter 待验证项。
- [SPEC.md](SPEC.md)：已确认的范围与验收标准。

预期部署：

```text
公司服务器上的 Langflow
    ↓ 提交文件和用户 Prompt / 查询 / 下载 / 取消
另一台 Windows 服务器上的任务服务
    ↓
官方 Codex CLI（codex exec）
    ↔ Windows 上已有的模型与认证配置
```

公司 Langflow 服务器不安装 Codex；个人电脑不参与正式任务执行。

使用现有 Siemens 英中翻译案例验证：四份输入资料，返回译文 CSV、检查报告和可导出的执行记录。用户上传的 Prompt 原文传入 Codex。

## 当前仓库

- `src/codex_remote_worker/`：HTTP 接口、SQLite、Windows Job Object、运行器和检查逻辑。
- `integrations/langflow/`：节点源码、可导入 JSON 和自包含代码。
- `scripts/`：Windows 安装/启动/服务注册脚本，以及离线 Langflow 构建工具。
- `config.example.json`：无密钥部署配置；`codex_home` 指向服务账户已经可用的 Codex 目录。
- `tests/`：模拟执行和接口测试，以及仅在 Windows 运行的进程树测试。

## 核心行为

Langflow 默认自动运行，上传四份资料后直接点击运行即可。已有任务会续查或取回结果；重新翻译、取消和单独连接检查移到高级设置。新模式不需要更新 Windows Worker。

- 相同提交编号的网络重试复用任务；主动重新试跑使用新编号。
- Prompt 原始 UTF-8 字节直接传入 Codex，不附加翻译指令。
- 运行环境通过独立的文件操作说明提供绝对路径、UTF-8 工具、已转换的风格指南和固定 CSV 构建器；不改变用户 Prompt 原文或模型配置。
- 指南在受控沙箱内预处理并按输入/转换器/工具版本缓存；只有 `zh-CN` 列由译文更新，CSV 非目标单元格保留原始字节。
- `http-access.jsonl` 记录请求时间、来源 IP、路由模板、状态和耗时；不记录 Token、请求正文、查询参数或任意原始 URL，日志轮转上限约 15 MiB。
- 公司服务器通过 HTTP 传送四份资料，Windows 工作区自主执行并发布文件。
- 任务状态落盘；取消回收进程树，服务重启不自动重跑模型。
- `completed` 只表示程序交付检查通过；语言质量仍需独立审校。
- 客户端不能覆盖模型或 provider，不提供任意命令、任意 CLI 参数或任意路径读取接口。

升级时必须同时更新 Windows Worker 和 Langflow 流程。旧 `models` 字段不再使用；无需卸载或升级服务器已有 Codex，只需配置正确的目录、版本并重新自检。新版会保留原来的任务记录，拒绝把旧模型参数任务悄悄改成新的默认模型。

## 开发验证

```bash
python -m pip install -e ".[test]"
python -m pytest -q
```

普通测试使用模拟 Codex，不发起模型请求。非 Windows 主机跳过原生进程测试，并且正式执行器会报告未就绪。`scripts/build_langflow.py` 需要 Langflow/LFX 1.12.6 开发环境，只编译模板，不执行流程。

运行数据、服务令牌和 OpenRouter 密钥不得提交 Git。仓库未配置远端，不包含用户上传的真实翻译资料。
