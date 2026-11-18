# 8788 整单持续 Codex 会话

## 按句段语言对翻译

`codex/8788-segment-languages-v2` 建立在 `codex/8788-contextual-tm-v2` 上。8788 空闲时停止服务、备份源码，将本分支的 `deployments/worker8788/source/src/codex_remote_worker/` 同步到 8788 安装目录的 `src/codex_remote_worker/`，再按原有方式启动服务。部署成功后，`GET /v1/tasks/capabilities` 的 `language_modes` 包含 `per-segment`。

上游照常提交 `project_name` 和 `segments`。v2 从每个句段读取非空字符串 `source_language` 和 `target_language`，允许同单混合语言，例如一段 `ja → en`、一段 `en → zh-TW`。两项原值都随句段进入 Agent 和交付结果，包括没有地区标签的 `zh`。字段缺失时返回句段编号和字段名。该段的 TB/TM、三份上传资料和已有的回填顺序沿用原契约。资料中适用的术语与风格约束随目标语言使用，方向由该段 JSON 确定。

Langflow v2 自动在 Worker 的 multipart `options` 中设置 `"language_mode":"per-segment"`。直接调用 Worker 时可使用同一选项，也可同时指定 `"tm_policy":"contextual-v2"`；两个选项独立。接单、查询及结果清单返回本单的语言模式。既有客户端和已接收的订单沿用其提交时的模式，同一幂等键不能切换模式。

## 按语境使用 100% TM

新分支 `codex/8788-contextual-tm-v2` 包含动作查询版本。等 8788 空闲后，按原有方式停服务、备份当前源码，将本分支 `deployments/worker8788/source/src/codex_remote_worker/` 同步到 8788 安装目录的 `src/codex_remote_worker/`，再启动服务。相对于动作查询分支，此次更新涉及 `contextual_tm.py`、`translation_tasks.py`、`translation_task_api.py`、`json_delivery.py`、`project_session.py` 和 `app_server_text.py`。运行中的服务通过 `GET /v1/tasks/capabilities` 声明 `tm_policies` 包含 `contextual-v2`。

v2 提交端在现有 multipart `options` 中指定 `{"timeout_seconds":7200,"tm_policy":"contextual-v2"}`。策略绑定到订单及幂等键，状态回执返回 `tm_policy`。未指定策略的调用沿用原契约；既有订单按提交时的策略恢复。

- 程序直用要求有生效 100% 候选、全部候选译文一致、所选原文精确相同、当前和历史上下文相同且具备具体内容、语言对一致，并通过术语、标识、占位符、数值和程序风格检查。缺少使用场景或出现冲突的句段进入同一翻译 Agent。相同分数的参考按稳定编号排序。
- Agent 读取相邻原文、上下文、候选和两份指南，通过原有 `project_write` 保存选择。`reuse` 指定候选编号，由宿主逐字复制；`adapt` 指定候选并提交调整后的完整译文；`retranslate` 用空候选编号提交新译文。自检时可回改，最后使用当前草稿所绑定的选择。
- 原始结果 JSON 只回填各段 `target_text`。完成时的批次清单另附 `tm_decisions`，按从 0 开始的原行号索引，记录 `segment_id`、`actor`、`action`、稳定候选编号、上游编号和 `enabled_tm_index`（原句启用候选列表中的从 0 开始的位置；重新翻译时为空）。`tm_decisions_sha256` 绑定该清单。
- 进度 `tm_reused_segments` 仅统计程序直用。`tm_summary` 分别统计直用、Agent 原样沿用、调整、重译，后三类只统计含生效 100% 候选的句段。Agent 确认复用仍属于已送模型的句段。该策略用于现有整单翻译会话，沿用现有 Worker 自检。

## 查询任务最近完成的动作

翻译流程 MVP-v2 通过以下查询读取 Agent 已经成功完成的操作。任务整体进度仍由 `status` 和 `stage` 表示。

```http
GET /v1/tasks/{task_id}?include_actions=true
```

- `session_progress.recent_actions` 最多包含最近 4 条记录，按顺序返回。`seq` 在同一任务内去重，`at` 是完成时间。两次查询之间若发生更多动作，序号可能跳跃。
- 读取提示词或指南的记录包含实际文件名或资料角色。读取原文、保存及修订译文按句段记录，检查点文件名为 `project-session-state.json`。查询只展示动作摘要与有限数量的句段编号。
- `actor=agent` 表示项目工具已成功执行。`actor=worker` 的 `create_result` 在整单 `result.json` 写出并登记后返回。纯 TM 订单可以只有这一条记录。未启动会话的排队任务仍可返回 `session_progress=null`。
- 省略 `include_actions` 时维持原状态响应。升级前的任务仅能返回已经留存的进度和可核实的结果文件记录。较早检查点恢复后，新工具动作会沿用原有工具计数继续编号。

Windows 更新时，等 8788 任务处理完毕，按现有方式停止服务并备份当前源码。将本分支 `deployments/worker8788/source/src/codex_remote_worker/` 中的 `project_activity.py`、`project_session.py`、`app_server_runner.py`、`translation_tasks.py` 和 `translation_task_api.py` 同步到 8788 安装目录的 `src/codex_remote_worker/`，再启动服务。当前整单会话配置下，`GET /v1/tasks/capabilities` 返回 `task_recent_actions: true` 表示新版已加载。

## 整单持续会话基线

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
