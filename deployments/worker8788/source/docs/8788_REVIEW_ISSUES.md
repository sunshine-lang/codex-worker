# 8788 整单审校问题清单

整单完成后，查询 `GET /v1/tasks/{task_id}`。如果响应有 `review_issues_path`，
用同一个 Worker 凭据请求这个路径。响应是名为 `review-issues.json` 的附件。
`result_path` 仍返回原结构译文 JSON，可独立下载。

清单沿用 v1 样本的结构：

- `review_status`：有待处理问题时是 `needs_review`，没有待处理问题时是 `passed`。
  这是清单状态，任务本身仍可为 `completed`，且译文可下载。
- `issues`：每项有 `row_id`、`row_number`、`segment_id`、`category`、
  `code`、`severity`、`message`。行号分别从 0 和 1 开始。
  有原始 `segment_id` 时使用它的字符串表示，否则使用 1 起始行号的字符串。
  规则问题来自最终译文的必用术语与受保护内容检查。语义问题来自同一会话对最终版本的自审；
  已回改的问题不会作为当前待处理问题交付。
- `notes`：每项有 `row_id`、`segment_id`、`code`、`message`。
  包含锁定 TM 的多条匹配、原文差异、与必用术语冲突等备注。

全 TM 自动复用的任务执行规则检查并生成清单，语义自审仍按服务原行为跳过。
旧任务如果当时未保存结构化明细，状态响应没有 `review_issues_path`，
请求清单返回 `404 REVIEW_ISSUES_NOT_AVAILABLE`。未完成的任务返回 `409 TASK_NOT_COMPLETED`。
清单与译文均保存后才将整单标记为完成。
