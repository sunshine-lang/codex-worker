# 项目材料预处理 Worker

把客户的 Word 指南和项目要求提交一次，即可取回一份供下游翻译模型使用的提示词，以及每份指南的原文 Markdown。这个分支 `codex/materials-preprocess-worker` 提供独立的材料服务，Windows 部署端口为 **8787**。调用方可以直接请求 Worker，也可以使用仓库自带的 Langflow 流程。

例如，提交一份英文设备面板的操作规范，要求面向现场操作员翻译。Worker 会将文档中的指定译法、错误码保留规则、适用条件和例外整理到 `translation_prompt`，同时在 `documents_markdown` 中交付原文和术语表。多份指南共用一个任务和一个 Codex 会话。遇到表格或图示歧义时，模型可以查看原件页图。零附件也可提交，提示词将依据业务标签、语向和文字要求生成。

## 快速试用

以下三段在同一个 PowerShell 7.1+ 会话中依次执行。准备一份自己的 `.doc` 或 `.docx`。在 Worker 主机上可用默认的回环地址；从下游机器调用时，先把 `MATERIALS_WORKER_URL` 设为下游可访问的 Worker 基础地址，例如 `http://<Worker 主机>:8787` 或已配置的 HTTPS 代理地址。令牌使用安装时生成的**材料 Worker 专用令牌**，获取方式见下文 Windows 部署一节。

**1. 确认服务身份。** 预期返回 `service = materials`、`ready = true` 和执行位数。

```powershell
$ErrorActionPreference = 'Stop'
$base = if ($env:MATERIALS_WORKER_URL) { $env:MATERIALS_WORKER_URL.TrimEnd('/') } else { 'http://127.0.0.1:8787' }
$token = if ($env:MATERIALS_WORKER_TOKEN) { $env:MATERIALS_WORKER_TOKEN } else { Read-Host '材料 Worker 令牌' -MaskInput }
$headers = @{ Authorization = "Bearer $token" }
Invoke-RestMethod "$base/v1/ready" -Headers $headers -TimeoutSec 15
```

**2. 提交一份 Word。** 将 `客户指南.docx` 换成本地文件路径。保留作业的 `submission_key` 和 `task_id`。提交时若连接中断，可以用原 `$body` 重发最后的 POST。

```powershell
$file = Get-Item -LiteralPath '.\客户指南.docx'
$key = [guid]::NewGuid().ToString()
$body = @{
    submission_key = $key
    project_name = '设备面板操作手册'
    business_tag = '设备操作手册'
    source_language = 'en'
    target_language = 'zh-CN'
    translation_requirements = '面向现场操作员，操作步骤使用祈使句。'
    documents = @(@{
        source_filename = $file.Name
        content_base64 = [Convert]::ToBase64String([IO.File]::ReadAllBytes($file.FullName))
    })
} | ConvertTo-Json -Depth 5 -Compress
"submission_key=$key"
$task = Invoke-RestMethod "$base/v1/material-tasks" -Method Post -Headers $headers -ContentType 'application/json; charset=utf-8' -Body ([Text.Encoding]::UTF8.GetBytes($body)) -TimeoutSec 90
$task | Select-Object task_id, status, documents_total
```

**3. 等待完成并查看两种产物。** 下面的命令也会把完整 JSON 以 UTF-8 保存到当前目录，用任务号命名。已有任务号时，直接将 `$taskUrl` 设为 `"$base/v1/material-tasks/实际任务号"`，从 `do {` 开始继续执行。

```powershell
$taskUrl = "$base/v1/material-tasks/$($task.task_id)"
do {
    $state = Invoke-RestMethod $taskUrl -Headers $headers -TimeoutSec 30
    $state | Select-Object status, stage, current_file, documents_completed, documents_total
    if ($state.status -in @('queued', 'running')) { Start-Sleep -Seconds 3 }
} while ($state.status -in @('queued', 'running'))

if ($state.status -ne 'completed') {
    throw "$($state.task_id) [$($state.status)] $($state.current_file) $($state.error.code) $($state.error.message)"
}
$result = Invoke-RestMethod "$taskUrl/result" -Headers $headers -TimeoutSec 120
$result.translation_prompt
foreach ($item in $result.documents_markdown) {
    $item.source_filename
    $item.markdown
}
$result | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath "$($state.task_id).json" -Encoding utf8NoBOM
```

以下是缩短的结果示例。提示词由模型按本次提交生成；Markdown 由文档转换工具生成，数组顺序与输入一致。附件中的内嵌图片会以 data URI 出现在对应 Markdown 中。零附件时返回 `"documents_markdown": []`。

```json
{
  "translation_prompt": "将英文设备面板操作说明译为简体中文。操作步骤使用祈使句。将 Reset button 统一译为“复位按钮”，错误码 E17 原样保留。",
  "documents_markdown": [
    {
      "source_filename": "客户指南.docx",
      "markdown": "# 面板操作指南\n\n错误码 E17 原样保留。\n\n| 原文 | 指定译法 |\n|---|---|\n| Reset button | 复位按钮 |"
    }
  ]
}
```

## Worker HTTP 接口

所有端点都要求 `Authorization: Bearer <材料 Worker 令牌>`。POST 请求体为 UTF-8 JSON。下表路径均相对于 Worker 基础地址。

| 方法和路径 | 成功响应 | 用途 |
| --- | --- | --- |
| `GET /v1/ready` | `200` | 查看 `service`、`ready`、`execution_slots`、`execution_slot_unit` 和 `document_types`。真实模型及文档转换可用上面的材料任务验收。 |
| `POST /v1/material-tasks` | `202` 新建，`200` 幂等重放 | 返回任务对象，包括 `task_id`、`submission_key` 和 `status`。 |
| `GET /v1/material-tasks/{task_id}` | `200` | 返回任务状态、阶段、当前文件、文档进度、最近动作、更新时间，以及可用的执行位和错误信息。 |
| `GET /v1/material-tasks/{task_id}/result` | `200` | 已完成任务返回 `translation_prompt` 字符串和 `documents_markdown` 数组；其他状态返回 `409`。 |

### 提交字段与限制

| 字段 | 类型与要求 |
| --- | --- |
| `project_name` | 必填非空字符串。项目可以同名，每次独立提交由任务号区分。 |
| `business_tag` | 必填非空字符串，例如 `IM` 或 `设备操作手册`。 |
| `source_language`、`target_language` | 两个必填非空字符串，例如 `en` 和 `zh-CN`。 |
| `translation_requirements` | 可选字符串，默认 `""`。用于补充受众、文体、术语或范围要求。 |
| `submission_key` | 可选 UUID。省略时由服务生成。需要网络重试时，应在首次提交前由调用方生成并保存。 |
| `documents` | 可选数组，默认 `[]`。每项包含 `source_filename`（含扩展名的原文件名）及 `content_base64`（文件原始字节的标准 Base64）。多个附件按期望的交付顺序排列。 |

支持非空的 `.doc` 和 `.docx`，每笔提交的附件解码后合计最多 **32 MiB**。Word 内引用的外部图片应先嵌入原件。服务检查 DOCX 解包大小（256 MiB）和条目数（12,000），交付内容预算为 96 MiB，内嵌图片也计入。解析某份附件失败会使该笔任务失败，可通过状态接口定位文件。

### 进度、重试与错误

- `queued` 等待执行位，`running` 表示已开始。`stage` 可见 `transcribing`、`reading_documents`、`checking_page`、`generating_prompt` 或 `validating_result`。`documents_completed` 统计已转写的文件数，所有文件转写完后模型仍可能在综合提示词。结果以 `status = completed` 为准。
- 收到 `task_id` 后用它查询。相同 `submission_key` 携带相同参数及同一组有序附件会返回原任务；同一个键改动内容返回 `409 IDEMPOTENCY_CONFLICT`。修改材料、重新生成结果或重跑已失败的任务时，使用新的键。
- `failed` 或 `interrupted` 时读取状态对象的 `error.code`、`error.message` 和可用的 `current_file`。后台任务失败也能正常查询到状态（HTTP `200`）。尚在运行时取结果会收到 `409 RESULT_NOT_READY`。
- `401 UNAUTHORIZED` 时检查材料令牌。`404 NOT_FOUND` 时检查任务号和目标实例。请求体过大可返回 `413`，字段、文件名、格式或编码不合规可返回 `422`。`429 QUEUE_FULL` 或 `503 MAINTENANCE` 时稍后用原提交键和原内容重试。

## 通过 Langflow 调用

导入仓库的三节点画布。输入节点接收业务 JSON，材料节点调用 Worker，`ChatOutput-Materials` 输出任务响应。仓库附带的调用脚本可读取本地 Word、编码上传，并从 Langflow 外层响应中提取业务 JSON。[画布文件](integrations/materials/flow.json) · [节点源码](integrations/materials/component.py) · [调用脚本](integrations/materials/call.py)

| 配置位置 | 配置项 |
| --- | --- |
| Langflow 服务端 | `MATERIALS_WORKER_URL` 填 Langflow 所在机器可访问的 Worker 基础地址。将材料令牌填入「项目材料 Worker」节点的密钥框，或设置 `MATERIALS_WORKER_TOKEN`。节点中的令牌优先。 |
| 调用脚本所在机器 | `MATERIALS_FLOW_URL` 填完整的 `https://<Langflow 主机>/api/v1/run/<flow_id>`。`LANGFLOW_API_KEY` 用于 Langflow 的 `x-api-key` 请求头，未设置时脚本会提示隐藏输入。 |

从仓库根目录执行。将示例 URL、附件路径和任务号替换为实际值。可重复使用 `--document` 上传多份文件，省略该参数即可验证零附件场景。

```powershell
$env:MATERIALS_FLOW_URL = 'https://<Langflow 主机>/api/v1/run/<flow_id>'
$key = [guid]::NewGuid().ToString()
python integrations/materials/call.py submit --submission-key $key --project-name '设备面板操作手册' --business-tag IM --source-language en --target-language zh-CN --requirements '面向现场操作员' --document '.\客户指南.docx'
$id = Read-Host '上一步返回的 task_id'
python integrations/materials/call.py status $id
# 查询到 completed 后取回
python integrations/materials/call.py result $id --output result.json
```

自行调用画布时，用 `POST /api/v1/run/<flow_id>`，设置 `input_type` 和 `output_type` 为 `chat`、`output_component` 为 `ChatOutput-Materials`。`input_value` 是序列化后的业务 JSON **字符串**。提交使用 `operation: "submit"` 加上提交字段；查询和取回分别发送 `{"operation":"status","task_id":"…"}` 和 `{"operation":"result","task_id":"…"}`。从输出节点的 `results.message.text` 解析 JSON，并检查 `error.code`；Langflow 外层 HTTP 200 也可能包含业务错误。

若文件已经上传到 Langflow，可在 `documents` 中以 `file_path` 引用当前调用者或流程有权读取的已上传文件，也可使用聊天附件。每次选择一种附件传递方式。直连 Worker 的请求使用 Base64。

## Windows 部署与运维

安装器用于已有 8789 或 8788 Worker 的 Windows 主机。它优先读取 8789，其次读取 8788 的运行配置，复用已注册桌面包的 Windows 服务账号、模型配置和登录。主机需要管理员 PowerShell 7、原 Worker 的内置 Python 3.13 和 LibreOffice。LibreOffice 自定义安装位置可用 `MATERIALS_SOFFICE` 指向 `soffice.com`。安装器将 Pandoc、PDF 页图渲染等依赖装到材料实例自己的 `vendor` 目录。[部署说明](packaging/materials/README.md) · [安装脚本](packaging/materials/install.ps1) · [启动脚本](packaging/materials/start.ps1)

```powershell
git clone --single-branch --branch codex/materials-preprocess-worker https://github.com/sunshine-lang/codex-worker.git codex-worker-materials
Set-Location codex-worker-materials
.\packaging\materials\install.ps1 -Slots 5
.\packaging\materials\start.ps1
```

- 端口为 **TCP 8787**，监听地址为 `0.0.0.0`。安装前应腾出该端口。`-Slots` 支持 1 至 8，默认 5。每个执行位处理一整笔提交。
- 安装目录为 `C:\CodexRemoteWorker-AppServer-8790`，数据目录为 `C:\CodexWorkerData-AppServer-8790`。目录名沿用 `8790` 是为了兼容已有安装。任务保存在 `materials.sqlite`，每笔提交的文件位于 `tasks\<task_id>\`，启动错误记录在数据目录的 `serve.stderr.log`。
- 首次为 Langflow 开放访问时，可为安装脚本提供 `-LangflowAddress`，值为 Langflow 的实际出口 IP 或网段。使用云安全组或反向代理时也要配置到 8787 的相应访问路径。
- 首装生成专用令牌；升级时沿用现有令牌和任务库。管理员可在服务器运行下方命令，将令牌复制到 Langflow 密钥框或调用端的密钥配置中。

```powershell
$privateConfig = 'C:\CodexWorkerData-AppServer-8790\private\credentials.local.json'
(Get-Content -LiteralPath $privateConfig -Raw | ConvertFrom-Json).CODEX_WORKER_SERVICE_TOKEN | Set-Clipboard
```

主机重启后可从分支目录重新运行 `.\packaging\materials\start.ps1`。服务会继续调度排队任务，恢复已经写出的结果；运行途中中断且没有已发布结果的任务标为 `interrupted`。升级前等已接受的任务完成，停止材料实例，再运行安装和启动脚本。

若首次启动日志显示 `pypandoc` 缺少 `get_pandoc_path`，检查服务账号对 `vendor\pypandoc\__init__.py` 和 `vendor\pypandoc\files\pandoc.exe` 的读取和执行权限。本机部署曾因依赖子目录没有继承 `vendor` 的访问权限而出现该错误。修复该实例依赖目录的权限继承后，重新运行启动脚本。

## 开发、测试与源码

材料服务的 Python 工程位于 `deployments/worker8788/source`。在该目录的 Python 3.12+ 虚拟环境中运行专项测试。安装 LibreOffice 后，可同时覆盖旧版 Word 转写和原件页图。

```powershell
Set-Location deployments/worker8788/source
python -m pip install -e ".[test,materials]"
python -X utf8 -m pytest -q -rs tests/test_materials.py
```

测试覆盖真实 Word 转换、表格和图片交付、同会话读取多个文件、幂等重试、并发排队、零附件、损坏文件和重启恢复。测试使用模拟模型响应。验收实际模型时，用带有一条指定译法和一个例外条件的 Word 走一次快速试用，核对提示词是否包含两项要求，并检查 Markdown 是否保留原文和表格。[专项测试](deployments/worker8788/source/tests/test_materials.py)

| 入口 | 内容 |
| --- | --- |
| [材料 HTTP 服务](deployments/worker8788/source/src/codex_remote_worker/material_service.py) | 请求校验、鉴权、调度、任务状态和结果交付。 |
| [模型会话](deployments/worker8788/source/src/codex_remote_worker/material_agent.py) | 逐份读取、原页查看、提示词生成和交付校验。 |
| [Word 转换](deployments/worker8788/source/src/codex_remote_worker/material_documents.py) | DOC/DOCX 转写、图片内嵌、页图生成和大小限制。 |
| [8788 整单翻译 Worker](deployments/worker8788/README.md) | 同仓库中的翻译订单接口、逐句语言对、上下文 TM、审校与恢复。 |
| [早期 Worker 基线](deployment/current-runtime/README.md) · [整单接口历史说明](docs/WHOLE_TASK_API.md) · [第三方声明](NOTICE.md) | 根目录 `src/`、早期运行快照和历史接口的参考资料。 |
