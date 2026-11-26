# 8789 开发 Worker

基线为 Git 提交 `c3e9fe82877dc445f90e9640b3278d08d7ab1ad7`。8789 使用自己的安装目录、数据、凭据和恢复计划。更新程序从本 Git 工作树组装代码，文件在 `order_materials/runtime_base.json` 中逐项校验。开发版新增 `order-materials/1` 接口。

## 更新已安装的 8789

在服务器上进入 **8789 专用的 Git 工作树**，使用管理员 PowerShell 7 执行以下命令。

```powershell
if ((git branch --show-current).Trim() -ne 'codex/8789-dev') { throw '请进入 8789 开发工作树' }
git pull --ff-only origin codex/8789-dev
if ($LASTEXITCODE -ne 0) { throw '开发分支拉取失败' }
.\packaging\worker8789\update.ps1
```

更新前让现有开发订单结束，并与调用方约定切换窗口。脚本先生成和校验待安装代码，再检查开发实例空闲，原子切换 `C:\CodexRemoteWorker-AppServer-8789\src\codex_remote_worker` 并启动开发恢复计划。成功时输出 `8789 ready: project FIFO, same thread, resume/replace, order-materials/1` 以及原版本备份路径。新版本无法就绪时会尝试在空闲条件下恢复原代码。安装、数据库和认证仍归本机的 8789 实例所有。

## 发布同一开发 Flow

Worker 就绪后，维护方在含本提交的工作树运行 Python 和 Git。以下命令只识别 IM 项目里的开发 Flow `9f26a456-0a79-4b62-9cfe-438006c339b3`，并校验提交、查询节点均绑定开发 Worker。

```bash
python3 packaging/worker8789/langflow_dev.py
python3 packaging/worker8789/langflow_dev.py --apply
```

第一条只预演。第二条检查 8789 能力、核对待替换节点、写入并读回开发 Flow。可通过 `LANGFLOW_API_KEY` 或终端隐藏输入传入开发 Flow 的密钥。Windows 可将 `python3` 换成仓库环境的 Python。

## 提交、查询和取稿

原订单继续用 `project_name` 和 `segments`。01 节点接受新参数 `tweaks.CustomComponent-MVPInputs.materials_json`，它是下面对象序列化后的 JSON 字符串。每个新单和修订稿都需要一个非空 UTF-8 Markdown Prompt，参考文件按传入顺序排列，可为空数组。文件名在本单内唯一。Prompt 上限 1 MiB，参考文件每份上限 32 MiB，参考文件最多 128 份。Worker 按实际配置检查总上传字节数（默认 64 MiB，包含订单）。

```json
{
  "prompt_file": {"filename":"翻译要求.md","content":"把设备说明翻译成简体中文，按[术语表](术语表.md)执行。"},
  "reference_files": [{"filename":"术语表.md","content":"Reset 按钮统一译为“复位”。"}]
}
```

现有 advanced 外层继续用 `inputs[0].input_value` 传序列化的业务订单，以 `ChatOutput-MVPResult` 取提交回执。新单使用独立的 `submission_key`。8789 内部把订单、Prompt 和参考文件作为 multipart 的 `source_file`、`prompt_file` 和重复的 `reference_files` 发送，按订单固定文件正文与文件名。回执和定稿返回 `materials_sha256` 与有序文件清单；译文在 `result` 中。

本地标准库调用器示例：

```bash
python3 packaging/worker8789/order_materials/call_mvp_v2.py --input order.json --prompt-file 翻译要求.md --reference-file 客户指南.md --reference-file 术语表.md --out build/8789-runs/order-A
python3 packaging/worker8789/order_materials/call_mvp_v2.py --status TASK_ID
python3 packaging/worker8789/order_materials/call_mvp_v2.py --resume build/8789-runs/order-A
python3 packaging/worker8789/order_materials/call_mvp_v2.py --stop TASK_ID
```

`--resume 目录` 恢复本地查询；若提交回执曾丢失，沿用已保存的提交键和完整资料重试。也可在脚本旁放一行本机专用的 `langflow_api_key.txt`。每笔单的结果目录和密钥文件应仅保存在自己的调用环境。

同项目按 Worker 受理顺序执行。后单等待时可见 `waiting_on_task_id`，取得执行位后续用原 `codex_thread_id`。已入队后单的材料在其开始执行时生效。不同项目可占用不同执行位。对新资料单，100% TM 也进入项目会话核对，可沿用、调整或重译。历史已受理订单继续用原任务号查询。原位恢复用 `operation=resume`、原 `task_id`、独立的 `recovery_key`，输出节点为 `ChatOutput-MVPStatus`。原位换稿用 `operation=replace`、原 `task_id`、新 `submission_key`、完整同项目订单和本次材料，输出节点为 `ChatOutput-MVPResult`。人工停单、服务中断的前单及修复顺位沿用基线行为。

## 更新后的真实双单验收

在存放真实专利材料的本地工作环境运行以下命令。传入 HRYY 四份 Markdown 对应的预处理结果 JSON。此文件用作普通测试输入，业务运行不依赖预处理服务。

```bash
python3 packaging/worker8789/order_materials/verify_two_orders.py --materials-result /实际目录/02-专利/result.json --out build/8789-runs/双单-01
```

脚本用真实的“具体实施方式”和“附图说明”创建两笔同项目订单，原材料在本地保留。A、B 使用相同的业务输入、Prompt、前 3 份文件和一条旧 100% TM，仅在第 4 份材料测试副本开头把该标题的指定译法从 `Detailed Description of the Embodiments` 换成 `Modes for Carrying Out the Invention`。取得 A 回执就发 B。验收检查不同任务号、前单关系、同一非空线程号、模型实际打开第 4 份文件、两份译文、定稿哈希、自检和 TM 决策，写入 `acceptance.json`。网络中断可用同一个 `--out` 继续。加 `--prepare-only` 只生成本地用例。

## 本地回归与首次安装

在仓库测试环境运行 `python -m pytest packaging/worker8789/order_materials/tests`。测试先在临时目录组装实际开发运行包，以 ASGI 和模拟模型 RPC 验证收件、顺序、会话恢复、换稿、定稿以及文件读取。可用 `langflow_dev.py --export-nodes <新目录>` 导出 3 个已核对的节点代码，把 `LF8789_NODE_DIR` 指向该目录运行节点到 Worker 的离线集成测试。若已在本地生成真实双单用例，可再把 `LF8789_REAL_CASE` 指向该用例目录。

首次安装独立实例时使用 `packaging/worker8789/build.ps1` 和 `install.ps1`；已有实例使用上文的 `update.ps1`。开发控制台为 `Worker8789.exe`，服务地址为 `http://52.62.69.192:8789`。
