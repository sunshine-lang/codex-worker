# 8788 逐批结果接口增量

基于 `a26fcba` 的 ChatGPT / App Server 四路版本。只增加读取已完成批次的接口和整单内容哈希，
不修改模型、思考强度、登录状态、并发上限、启动器、翻译 Prompt、TM 分流或批次划分。

## 部署

确认没有在途订单后，更新 8788 部署中的以下两个源码文件，并按原方式重启 8788：

- `src/codex_remote_worker/translation_task_api.py`
- `src/codex_remote_worker/translation_tasks.py`

本分支部署布局为 `C:\CodexRemoteWorker-AppServer-8788\src\codex_remote_worker`。
保留本机 `config.trial.json` 和 `C:\CodexWorkerData-AppServer-8788\private\codex-home`；
不要用示例配置覆盖现有 high、四路翻译或 ChatGPT 登录状态。8787 无需启动或更新。

`GET /v1/tasks/capabilities` 应新增：

```json
{"batch_results":true,"batch_result_contract":"translation-task-batches/1"}
```

Langflow 将通过该标志启用流水线。未升级时保留原有整单翻译后审校的兼容路径；
兼容路径不作为流水线验收通过。

## 接口

所有新增接口沿用原 Bearer Token。

`GET /v1/tasks/{task_id}/batches` 返回持久批次清单：原始上传 JSON 的 SHA-256、
100% TM 的原始行号、全部模型批次行号、每批是否可取、批次文件 SHA-256、下载路径，
以及同一次数据库读取的任务状态。已完成批次不会因后续轮询而消失，允许乱序完成和重复读取。

`GET /v1/tasks/{task_id}/batches/{batch_index}` 返回该批的紧凑 `rows` JSON。
内容与清单哈希绑定；未完成返回 409，不存在返回 404，文件被篡改返回 409。
客户端必须校验任务、行号集合和哈希，不能只凭 HTTP 200 交付。

任务最终完成后，清单提供 `result_content_sha256`：
`SHA256(JSON(ensure_ascii=False, sort_keys=True, separators=(",",":")))`。
Langflow 将原单和逐批译文合并后核对这个值，不必再下载包含全部 TB/TM 的整单文件。
历史已完成任务可能没有此新增字段，客户端可读取旧整单结果进行一致性核对。

## 验证范围

本地 87 项相关测试通过，2 项原生 Windows 测试因平台条件未运行。
新增测试覆盖：乱序批次提前可取、遗漏轮询后补取、重复读取、原始输入绑定、
未完成/越界批次、鉴权、哈希篡改和纯 TM 零模型调用。
没有在本地调用真实翻译模型。Windows 更新后的 64 条与 1344 条联调另行记录。

`deployment/8788/source-manifest.json` 是原 `a26fcba` 部署的历史快照，
本增量以当前 Git commit 及交付文件哈希为准，不改写历史 Windows 验收记录。
