# 8788 项目上下文版

本文记录早期按批次查询项目上下文的版本。当前 8788 使用按项目名称续译的整单接口，见 [整单任务接口](source/docs/WHOLE_TASK_API.md)。

## 旧版翻译方式

- 每次 `/v1/tasks` 整单提交是一份独立上下文。该整单内按原始顺序逐批处理，上一批发布并校验后才提交下一批。
- 相同非空 `project_name` 的未完成整单按接收顺序排队；不同项目可使用现有 8 个全局通道。
- 不自动混入其他请求、其他项目或已清理历史任务的译文。同名项目跨请求共享翻译记忆尚未实现。
- Codex 只获得当前批次完整输入和规范；通过只读 `project_context` 工具主动查询整份项目、前后文以及当前整单此前已完成译文。
- 查询支持 overview、按零基行号 read、关键词/相似措辞 search。检索是词汇匹配，不是向量语义搜索；模型可换关键词、查询相关条目来补充语境。
- 长结果通过 char_offset 完整分页，不静默截断。每批最多 80 次查询；超限或未调用 overview 时不发布译文。
- 上传数据中的原有 target_text 不算已确认译文；历史模型译文标记为未审校候选。相同语境可以沿用，不同含义需重新判断。
- 未增加自动全文语义审校；原有上游 04 审校环节仍需保留。此版本不承诺翻译质量等同人工审核。

## 隔离与复用

- 宿主将任务绑定到指定 Job；工具不接受文件路径或其他任务编号。所有历史批次结果均核验哈希，未完成的前一批禁止读取并继续。
- 不开放命令、文件系统、浏览器、个人 MCP 或插件。动态工具请求仅路由到已绑定会话，并核验 turnId。
- Codex 延迟创建的官方 code-mode 工具宿主，仅在完整卸载会话后，通过已知路径、启动前文件哈希、父进程和精确 PID/创建时间核验后纳入运行时复用基线。其他额外进程仍触发清理。

## 桌面设置

- 桌面 `Worker8788.exe` 保留服务状态、地址/Token 复制、托盘功能，新增模型和思考等级选择。
- 下拉选项来自当前 Codex 的 model/list。应用仅修改 8788 独立 Codex home 的 model 与 model_reasoning_effort。
- 执行通道控件支持全局 1～8 路，应用同时更新 8788 的 max_concurrent_jobs；task_batch_concurrency 始终为 1，同一整单仍按顺序处理。通道单独变化不会改动模型配置。
- 模型配置和通道配置一起备份，启动失败一起回滚。通道版本实测 8→1→8，证据为 desktop-channels-final-verification.json。
- 有未完成任务时拒绝切换；空闲时备份、重启、检查 ready；失败尝试恢复原配置。没有添加公网配置接口。
- 启动就绪检查不能代替每个模型的真实翻译质量评估。当前交付恢复为 gpt-6-astra / high。

## 构建与证据

- EXE：`packaging/worker8788/build.ps1`；本机安装：`packaging/worker8788/install.ps1`。
- 实际项目模块位于独立快照 `build/app-server-8788/source/src/codex_remote_worker`，部署到 `C:\CodexRemoteWorker-AppServer-8788`。
- 部署及历史数据备份位置：`project-context-deployment.json`。
- 测试：`context-release-tests.xml`、`context-runtime-tests.xml`、`project-context-live-final.json`、`project-context-live-different-sense.json`。
- 真实 5000 条跨度测试只有 33 条调用模型，其余 4967 条走已匹配 TM，属于机制验收，不是用户业务全量质量验收。
