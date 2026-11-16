# 整单任务接口 translation-task/1

在现有加固版 Worker 上新增整单接收与分批管理，继续复用原单批 jobs 队列、模型配置、沙箱及
`utf8-absolute-tools-v4-bounded-context` 输入工具。原 `/v1/jobs` 接口保持兼容。

## API

全部接口使用现有 Bearer 令牌鉴权：

- `GET /v1/tasks/capabilities`：能力与服务端分批预算。
- `POST /v1/tasks`：四个文件字段 `source_file`、`prompt_file`、`project_style`、`general_style`，
  以及 `options` JSON；头部 `Idempotency-Key` 为调用方生成的 UUID。
- `GET /v1/tasks/{task_id}`：整单进度、TM 数量、模型处理数量、批次数及活动子 job。
- `GET /v1/tasks/{task_id}/result`：完成后下载原结构候选 JSON；未完成返回 409。

`source_file` 接收业务对象、对象数组，或 `{"project_name":"项目","segments":[...]}`。
每条记录须包含 `source_language: "en"`、`target_language: "zh"` 或 `"zh-CN"`、非空
`source_text`、`tb_list`、`tm_list`。项目包内 `segment_id` 须非空且唯一。
`options` 仅接受原来的每批 `timeout_seconds`。

整单最多 10000 条。服务端按最多 32 条、32000 字符自动分批；超长单条单独成批并记录提示。
字符预算不是模型 token 容量保证。100% TM 在宿主侧采用第一条目标译文并保留原样，
只有剩余条目进入模型。输出仅回填 `target_text`，保持原业务结构、字段和顺序。

## 持久化与运行

- 数据保存到现有数据根下的 `tasks.sqlite3`、`tasks/<task_id>`，临时上传位于 `task_staging`。
- 子批次写入原 jobs 队列，使用稳定幂等键。恢复时复用已接受的子 job，不重复调用状态不明的模型任务。
- 整单完成只表示候选译文完整，`review_status` 固定为 `not_started`；本次没有新增独立语义审校。
- 维护门禁同时阻止新的整单接收和父任务派发；维护或队列满时保留等待状态，恢复后继续。
- 部署及清理必须同时检查 jobs 与 tasks 的活动状态。不得单独删除仍被整单引用的子 job。

同一提交编号携带相同文件与选项会复用整单，不同输入返回 409。主动重新翻译请生成新 UUID。
安装由当前源码增量合并后重建在用 EXE；仅给 Python 环境复制模块不会更新打包程序。
