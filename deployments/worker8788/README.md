# 8788 整单持续 Codex 会话

2026-11-17，控制协议 `ledger/2`。每份需要模型处理的 `/v1/tasks` 整单使用一个 Worker Job、一个执行通道和一个持续的 Codex thread。Codex 自主选择完整材料的读取范围、分页大小、翻译顺序和保存分组，并可在同一轮完成整单翻译与自检。Worker 保存进度并核验交付条件，不再按固定条数派发模型工作。

本次同步的运行配置为官方 GPT 订阅 `gpt-6.1-sol / medium`、一个执行通道、872,000 tokens 上下文和 784,800 tokens 自动压缩阈值。配置示例不含认证信息，不代表其他安装环境已取得相同模型权限。

详见 [持续会话处理与调用说明](CONTINUOUS_SESSION.zh-CN.md)。仓库根目录的基线 Worker 源码不因本次改造改变。

- source/：完整源码、测试、构建资料。
- runtime/：8788运行入口，依赖目标机安装的Codex与Python。
- 仓库根目录packaging/worker8788/：桌面程序源码与配置控制器。
- 配置示例为手工编写的占位符，未复制本机配置。
- 不包含Token、登录缓存、服务账户密码、任务数据或运行时二进制。

- [完整流程与接口边界](CONTINUOUS_SESSION.zh-CN.md)
- [检查点恢复和供应商兼容](RECOVERY.zh-CN.md)
- [验证记录及已知限制](VALIDATION.zh-CN.md)
- [服务配置示例](config.trial.example.json) 与 [Codex 配置示例](codex.config.example.toml)

源码校验：在仓库根目录运行 `python tools/verify_worker8788_snapshot.py`。
测试：进入 `deployments/worker8788/source` 后运行 `python -X utf8 -m pytest -q -m "not windows"`。
需要真实 Windows 管道的专项测试单独运行 `tests/test_app_server_adapter.py`。

`runtime/start-worker.ps1` 对应安装目录的 `desktop/start-worker.ps1`。其余运行脚本保持原部署路径含义；`start-trial.ps1` 为历史直接启动入口，当前服务使用打包账户启动链路。机器路径、官方 Codex 包版本和服务账户依赖均须在新机器上核对。当前上下文配置可在服务空闲时通过仓库工具 `tools/set_worker8788_context.py` 应用；该工具只适用于现有 8788 安装布局。

修改前版本已先推送：提交 `e473faf64f55b44ea309db362c7088608b0ca6b8`，分支 `codex/8788-project-session-20261116`。`snapshot-manifest.json` 记录这个修改前快照的指纹，不是当前源码清单。旧的 `PROJECT_CONTEXT.zh-CN.md` 描述修改前的顺序分批版本。

只向独立的 8788 安装目录部署，保留机器上的配置和凭据；不要覆盖 8787。升级前必须等待整单结束，备份当前 8788 源码及配置。切勿在执行过程中重启或替换文件。
