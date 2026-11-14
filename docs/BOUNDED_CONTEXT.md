# 有预算的输入读取

运行时 `utf8-absolute-tools-v4-bounded-context` 在非模型预处理阶段生成
`.worker/context/index.json` 和编号 JSON 页，将任务元数据、两份完整指南、全部待模型处理的源记录放在一起。
纯 TM 任务继续跳过模型；锁定的 `tm_reuse` 记录不进入模型输入包。

每页最多 **8000 UTF-8 字节，包含 JSON 元数据和末尾换行**。长指南按字符范围拆分；
单条超长源记录以 `source_fragment` 保存完整 JSON 文本的连续片段，字段不删减。
短源记录直接作为 `source` 部分返回，保留原始全局 `row_id` 与业务字段。

模型启动命令在预处理完成之后构建，环境说明直接告知页数。首轮模型应以每批最多 12 个独立
`exec_command` 调用并行读取所有页；页数较多时继续下一批。不要用单个 shell 命令拼接所有页，
因为 CLI 对单次工具输出存在独立截断上限。完整输入包替代常规的 inspect → 指南 → source 读取链。

```powershell
& '<tool_python>' -X utf8 '<work>/.worker/task_tools.py' context --page 0
```

`context` 页有页号、总页数、任务信息和部分列表。指南和超长源记录片段携带
`offset`、`end_offset`、`total_chars`；完整消费所有页后，按角色或 `row_index` 重组连续片段。
页文件按大小与 SHA-256 验证后读取。上传的用户 Prompt 字节、模型参数和交付 JSON 合约不变。

兼容的定向补读入口现在返回有游标的 JSON，而非无分页全文或裸数组：

```powershell
& '<tool_python>' -X utf8 '<helper>' read general_style --offset 150 --limit 330
& '<tool_python>' -X utf8 '<helper>' source --offset 0 --limit 20
```

- `read` 的 `offset/limit` 单位是行；`source` 的单位是记录。
- `--max-output-bytes` 可设为 512～8000，默认 8000。
- 使用返回的 `next_offset` 和 `next_char_offset` 分别作为下一次的 `--offset` 与 `--char-offset`。
- `has_more=false` 表示全部结束；有超长单行或单条记录时，字符游标保证继续前进。
- Python 内部的 `TaskTools.source()` 仍返回原始记录列表；REST API 不变。

代码和运行时版本更新会使原有指南缓存失效，首次任务可能重新转换 DOC。
分块覆盖、字节预算和命令顺序可离线验证；实际模型响应轮数、质量与耗时仍需真实任务验证。
