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
8788 的 `options` 支持 `timeout_seconds`、`tm_policy`、`language_mode`、可选的 `project_id`，以及换稿时使用的 `replaces_task_id`。

## 8788 上同一项目的后续订单

每单把长期固定的项目名称放在 `source_file` 顶层的 `project_name` 中，译文条目放在同层的 `segments` 数组中。8788 在受理首单时为名称分配内部编号，后续同名订单沿用这个编号和工作空间。每笔新订单使用自己的 `Idempotency-Key`，重试同一笔订单时沿用原键。以下是可用的 `options` 示例。

```json
{"tm_policy":"contextual-v2","language_mode":"per-segment","timeout_seconds":600}
```

名称忽略首尾空白，区分大小写，内部空格和标点也参与匹配。两个业务项目需要隔离时，应使用不同名称。裸数组和单条记录沿用旧的无名称处理方式。同一项目各单须使用相同的 TM 策略。兼容旧调用方显式提供 `options.project_id`。历史上一个名称只绑定过一个编号时，按名称提交会接回该编号。旧名称曾关联多个编号时，按名称提交会收到 409 和 `PROJECT_NAME_AMBIGUOUS`，须先核对归属。早期逐单开对话且没有编号的历史订单保留原记录，新单从新的项目对话开始。

8788 依照订单入库顺序关联前单。上一单运行期间，后单状态为 `queued`，阶段为 `waiting_for_project_order`。前单在排队时停止、执行中停止或失败，紧跟它的订单均显示 `waiting_for_project_recovery`。其余后续订单依次等待各自的前单。恢复或补交的前单完成交付后，后单自动取得通道并接回项目原对话。完全由 TM 交付的订单沿用项目已有对话编号。

如果具名项目的前单正在翻译时被服务重启打断，8788 会保留它的位置。已有后单排队，或随后收到该项目的后单，都会触发同一次自动恢复。8788 核实旧执行已结束、原对话和已保存草稿属于该单后，从草稿继续。没有后单时，前单保留中断状态。人工停止、翻译超时、资料错误及无法核实的中断都保留在队首，等待手动处理。

状态字段 `interruption_reason` 的值包括 `manual_stop`、`service_interruption` 和 `translation_failure`。自动恢复记录 `auto_recovery` 显示触发恢复的后单任务号、恢复时已有的草稿句数和处理状态。同一个恢复请求只受理一次。下游继续按原方式提交项目包、轮询任务号，可直接展示 `wait_reason`。

恢复原稿时，调用 `POST /v1/tasks/{task_id}/resume`，使用新的 UUID 作为 `Idempotency-Key`。恢复沿用原任务号，已写入有效检查点的译文继续参与后续翻译和自查。换稿时向 `POST /v1/tasks` 提交完整新稿和资料，在 `options.replaces_task_id` 中指定要替换的任务号，并给本次换稿分配新的提交键。换稿回执中的 `task_id` 用于查询新版进度和结果，`order_position_id` 对应最早的订单位置。旧稿状态保留 `replaced_by_task_id`。网络重试复用同一次操作的 UUID。已经越过旧单开始翻译的历史项目会返回 409。

每笔订单分别提交四份文件，分别查询结果。状态中的 `previous_project_task_id` 标识前一笔受理订单，等待时的 `waiting_on_task_id`、`waiting_on_status_path` 和 `wait_reason` 标识当前正在等哪一单、如何查询和等待原因。`codex_thread_id` 可用于核对实际对话。通道空闲后可承接其他项目。后续同名订单由可用通道加载该项目保存的工作空间和对话。每单的完整译文、进度和自查结果归入该单。

### Langflow 对接

测试流程的提交、查状态和取结果节点都应指向同一 8788 实例。提交节点调用 `POST /v1/tasks`，在 `source_file` 中保留业务传来的原项目名称和 `segments`。订单号、文件名、Flow ID 或会话号都应与项目名称分开。每笔订单保存返回的 `task_id`，用它查该单进度、取该单结果。遇到 `queued` 时继续轮询原单，页面可显示 `wait_reason`。Langflow 不需维护项目编号、通道、排队或 Codex 对话编号。

业务系统按业务顺序发送。收到第一单受理回执即可发送第二单，收到第二单受理回执即可发送第三单。走 8788 的「翻译流程MVP-v2」提供两个操作。

- 恢复原稿。向状态输出节点提交 `{"operation":"resume","task_id":"原任务号","recovery_key":"本次恢复的UUID"}`。
- 原位换稿。向提交输出节点发送完整项目包，顶层添加 `"operation":"replace"`、`"task_id":"被替换的任务号"` 和 `"submission_key":"本次换稿的UUID"`。保存回执中的新任务号，已排队的后单会自动等新版完成。

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
