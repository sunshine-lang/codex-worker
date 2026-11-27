# 项目材料预处理 Worker

把项目指南、工作簿、CSV、PDF、TXT 等原件和项目要求一并提交，即可取回一份供下游翻译模型使用的提示词，以及每份指南的原文 Markdown。这个分支 `codex/materials-preprocess-worker` 提供独立的材料服务，Windows 部署端口为 **8787**。调用方可以直接请求 Worker，也可以使用仓库自带的 Langflow 流程。

例如，提交一份英文设备面板的操作规范，要求面向现场操作员翻译。Worker 会将文档中的指定译法、错误码保留规则、适用条件和例外整理到 `translation_prompt`，同时在 `documents_markdown` 中交付原文和术语表。Worker 先将本单全部附件转写并保存为可交付的 Markdown，再启动一个 Codex 会话提炼项目规则。文件名由 Worker 按提交顺序分配。遇到表格或图示歧义时，模型可以查看原件页图。零附件也可提交，提示词将依据业务标签、语向和文字要求生成。

## 快速试用

以下三段在同一个 PowerShell 7.1+ 会话中依次执行。准备一份自己的项目材料，例如 Word、Excel、CSV、PDF 或 TXT。在 Worker 主机上可用默认的回环地址；从下游机器调用时，先把 `MATERIALS_WORKER_URL` 设为下游可访问的 Worker 基础地址，例如 `http://<Worker 主机>:8787` 或已配置的 HTTPS 代理地址。令牌使用安装时生成的**材料 Worker 专用令牌**，获取方式见下文 Windows 部署一节。

**1. 确认服务身份。** 预期返回 `service = materials`、`ready = true` 和执行位数，`format_detection` 为 `content`。支持交付文件名的新版本同时返回 `markdown_filenames: true`。`document_types` 列出常见可读格式 `doc`、`docx`、`xlsx`、`pdf`、`csv`、`txt`。

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
    $item.markdown_filename
    $item.markdown
}
$result | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath "$($state.task_id).json" -Encoding utf8NoBOM
```

每项附件的 `source_filename` 用来追溯原件，`markdown_filename` 是下游实际保存或挂载的文件名，`markdown` 是正文。将 `翻译Prompt.md` 与附件 Markdown 放在同一级目录，保留返回名，Prompt 中的相对链接即可打开。文件名按提交顺序生成，例如 `说明.docx`、`说明.pdf` 分别返回 `01-说明.md`、`02-说明.md`。超长多字节文件名只截短中间的原名部分，使整个交付名不超过 255 个 UTF-8 字节。

历史已完成任务保留原结果与旧 Prompt。新版 Python 示例在历史结果缺少 `markdown_filename` 时沿用旧编号命名。若调用方严格校验字段，请允许新的附件字段；输入及最外层的两个结果字段沿用原约定。

以下是缩短的结果示例。`translation_prompt` 是可直接保存为 Markdown 的字符串，按原 Prompt 的六个业务一级标题组织。本次输入若明确给出下游翻译契约，可在最前面增加「上游契约与模型任务字段」。零附件时仍保留外部指南一节，说明本次依据的业务标签、语向和文字要求。`documents_markdown` 按附件顺序交付原文和内嵌图片，零附件时返回 `[]`。

```json
{
  "translation_prompt": "# 角色设定\n\n你是设备面板英译简中的译员。\n\n# 任务目标\n\n为现场操作员翻译操作步骤，使用祈使句。\n\n# 外部指南文档挂载与优先级说明（KNOWLEDGE BASE GUIDELINES）\n\n依据本次 [`01-客户指南.md`](01-%E5%AE%A2%E6%88%B7%E6%8C%87%E5%8D%97.md) 中的术语表和错误码要求。\n\n# 翻译核心准则（CORE TRANSLATION RULES）\n\n面板操作说明中的 Reset button 译为“复位按钮”。使用 TM 时，即使匹配率为 100%，也应核对当前语境和客户修订。\n\n# 格式与避坑强化提醒（FORMATTING DOs & DON'Ts）\n\n错误码 E17 的大小写和数字原样保留。\n\n# 标签保留与交付格式 (TAGS & DELIVERABLES)\n\n保留原文标签和占位符，按调用方约定交付简体中文译文。",
  "documents_markdown": [
    {
      "source_filename": "客户指南.docx",
      "markdown_filename": "01-客户指南.md",
      "markdown": "# 面板操作指南\n\n错误码 E17 原样保留。\n\n| 原文 | 指定译法 |\n|---|---|\n| Reset button | 复位按钮 |"
    }
  ]
}
```

## Worker HTTP 接口

所有端点都要求 `Authorization: Bearer <材料 Worker 令牌>`。POST 请求体为 UTF-8 JSON。下表路径均相对于 Worker 基础地址。

| 方法和路径 | 成功响应 | 用途 |
| --- | --- | --- |
| `GET /v1/ready` | `200` | 查看 `service`、`ready`、`execution_slots`、`execution_slot_unit` 和 `document_types`。附加 `?include_channels=true` 可返回按编号排列的 `channels` 数组；运行中的通道附带任务号、项目、当前文件、最近动作和文档进度。 |
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
| `documents` | 可选数组，默认 `[]`。每项包含 `source_filename`（原文件名，可无扩展名）及 `content_base64`（文件原始字节的标准 Base64）。多个附件按期望的交付顺序排列。 |

附件入口接受不同后缀或无后缀的非空文件。Worker 根据原件内容选择读取器，保留原文件名及输入顺序。每笔提交的全部附件按原始文件大小相加，最多 **128 MiB**。Word 内引用的外部图片应先嵌入原件。服务检查 DOCX 和 XLSX 解包大小（256 MiB）和条目数（12,000），交付内容预算为 96 MiB，内嵌图片也计入。解析某份附件失败会使该笔任务失败，可通过状态接口定位文件。

**CSV 和 TXT。** 两种原件都可直接放进 `documents`，每份各返回一份 Markdown，并参与生成同一个项目 Prompt。CSV 按逻辑记录和原始物理行号转写，保留分隔符、引号、前导零、空格、标签和单元格内换行。普通文本以代码块保留原有段落和空行。文本支持 UTF-8、带 BOM 的 UTF-16/32 及 GB18030，换行统一为 LF。项目备注也可单独放在 `translation_requirements`，附件里的说明无需再复制进去。

**内容识别。** Worker 检查 PDF 标识、旧 Word 容器及 Word/Excel 的内部文档结构。其他可解码文本按可识别的 CSV 或普通文本处理。后缀用于区分容易混淆的纯文本和 CSV。结构损坏的文档、无法读取的二进制或不支持的封装会在任务状态中标明失败文件和原因。一个项目的全部附件都读取并转写后才发布结果。`/v1/ready` 的 `document_types` 说明已有读取能力，上游可直接提交原始附件。

**PDF 原件。** 直接上传可以打开的 PDF。每个文件返回一份同名来源的 Markdown，按原页码保存可复制的文字并内嵌每页的 PNG 页图。Worker 将带图片、矢量图形或批注的页，以及缺少可读文字的页列给 Codex。Codex 须逐页查看这些页图后才能交付 Prompt。纯扫描页以页图交付，可复制的正文取自 PDF 的文字层。需要打开密码、文件损坏或结果超出交付大小时，可在任务状态中看到原因。

**XLSX 行与截图。** 上传原始工作簿。Worker 在任务目录内按工作表、原行号、单元格地址及合并范围转写，将一个或两个单元格锚点定位的内嵌截图放入对应的行；同一行可有多张图，裁剪的图片采用可见区域。模型可查看逐行转写稿的页图。结果保留输入的 `.xlsx` 文件名。公式使用已保存的计算值，缺少缓存时显示可读公式。宏、外链图片、无行锚点的图形和 DISPIMG 单元格图片不在此次支持范围内。

### 进度、重试与错误

- `queued` 等待执行位，`running` 表示已开始。`stage` 可见 `transcribing`、`reading_documents`、`checking_page`、`generating_prompt` 或 `validating_result`。`documents_completed` 统计已准备好可交付 Markdown 的附件数，所有文件转写完后模型仍可能在综合提示词。结果以 `status = completed` 为准。
- 收到 `task_id` 后用它查询。相同 `submission_key` 携带相同参数及同一组有序附件会返回原任务；同一个键改动内容返回 `409 IDEMPOTENCY_CONFLICT`。修改材料、重新生成结果或重跑已失败的任务时，使用新的键。
- `failed` 或 `interrupted` 时读取状态对象的 `error.code`、`error.message` 和可用的 `current_file`。后台任务失败也能正常查询到状态（HTTP `200`）。尚在运行时取结果会收到 `409 RESULT_NOT_READY`。
- `401 UNAUTHORIZED` 时检查材料令牌。`404 NOT_FOUND` 时检查任务号和目标实例。请求体过大可返回 `413`，字段、文件名或 Base64 不合规可返回 `422`。已受理的附件若内容不支持或损坏，在任务状态中返回失败文件名和原因。`429 QUEUE_FULL` 或 `503 MAINTENANCE` 时稍后用原提交键和原内容重试。

在生成项目 Prompt 之前，任何一份附件的转写或内嵌资源处理失败都会在该附件上结束任务。模型按编号分块读取同一次转写的正文视图，所需页图另行读取。Worker 将本单材料引用解析为指向交付文件的相对链接，并在发布前校验指南目录覆盖所有本单附件。未随单提供的外部手册和客户逐字示例仍以原文说明。

## 通过 Langflow 调用

### 专利业务：先定稿，再生成 Prompt

新建专利任务沿用 `operation="submit"` 和 `business_tag="专利"`。直连 8787 仍使用 `POST /v1/material-tasks`。如果待译 CSV 只用于后续翻译，提交客户指南和参考 PDF 即可。机器上的 `GET /v1/ready` 返回 `patent_reading_prompt: "patent-reading-prompt/1"` 时，该 Worker 已支持此路径。

Worker 先转写原件。Word 的删除／插入、截图的修改前后、PDF 的例句分行、可识别的着重文字和图版按原件位置记录。读图会话按编号逐处写入文字，独立复核会话重新打开原图，可保留或修正每条记录。清晰部分与无法判读的部分要分开写；原件自身不一致时保留双方原话。文本已经完整、没有图版的材料可以直接排版。

复核结束后，Worker 保存阅读稿及其 SHA-256 清单。生成 Prompt 的新会话只能读取这次定稿。发布时再次核对每份内容，指纹不符会使该任务失败。正常结果仍包含 `translation_prompt` 和 `documents_markdown`，另附 `processing_mode` 和 `prompt_input`。其中 `prompt_input.files` 记录原件指纹、Markdown 文件名、字节数和内容指纹，`prompt_input.sha256` 标识整组有序定稿。历史任务仍按原任务号取其已发布结果。

校验会拦截把“无需生成句段译文”带进下游任务目标的草稿。发布器仅清理目标或交付章节中可独立识别的准备阶段状态句，引用、代码和客户示例保留。发生清理时，任务目录留有清理前的文本供对照。

### 仅整理 IM 资料

校对人员可以先取得每份原件的可编辑 Markdown。部署该版本后，`GET /v1/ready` 的 `organize_business_tags` 包含 `IM`。在 Langflow 使用 `operation="organize"`，项目字段和 `documents` 的传法与 `submit` 相同，业务标签填 `IM`。直连 Worker 的提交入口是 `POST /v1/material-tasks/organize`。查询与取回沿用同一个任务号及原有的 `status`、`result` 操作。

结果为 `{"processing_mode":"organize","documents_markdown":[...]}`。每份附件仍有 `source_filename`、`markdown_filename` 和 `markdown`。整理器将原文按标题、段落和表格排版，用逐图核对的文字替换内嵌图片，在对应位置注明原图序号。辨认有歧义的内容标为“待确认”，可对照原件审核。文字版用于业务确认，生成项目 Prompt 的调用继续使用 `submit`。

导入仓库的三节点画布。输入节点接收业务 JSON，材料节点调用 Worker，`ChatOutput-Materials` 输出任务响应。仓库附带的调用脚本可读取本地项目附件、编码上传，并从 Langflow 外层响应中提取业务 JSON。[画布文件](integrations/materials/flow.json) · [节点源码](integrations/materials/component.py) · [调用脚本](integrations/materials/call.py)

| 配置位置 | 配置项 |
| --- | --- |
| Langflow 画布 | 在第二个「项目材料 Worker」节点填写 **Worker 调用地址** 和 **材料 Worker 令牌**，然后保存。地址填写服务基址，支持带路径前缀的 HTTPS 反向代理。 |
| Langflow 服务端 | 地址或令牌字段留空时，分别读取 `MATERIALS_WORKER_URL` 和 `MATERIALS_WORKER_TOKEN`。正式远程接入使用 HTTPS 或受保护网络。 |
| 调用脚本所在机器 | `MATERIALS_FLOW_URL` 填完整的 `https://<Langflow 主机>/api/v1/run/<flow_id>`。`LANGFLOW_API_KEY` 用于 Langflow 的 `x-api-key` 请求头，未设置时脚本会提示隐藏输入。 |

从仓库根目录执行。将示例 URL、附件路径和任务号替换为实际值。可重复使用 `--document` 上传多份文件，省略该参数即可验证零附件场景。

```powershell
$env:MATERIALS_FLOW_URL = 'https://<Langflow 主机>/api/v1/run/<flow_id>'
$key = [guid]::NewGuid().ToString()
python integrations/materials/call.py submit --submission-key $key --project-name '设备面板操作手册' --business-tag IM --source-language en --target-language zh-CN --requirements '面向现场操作员' --document '.\客户指南.docx'
$id = Read-Host '上一步返回的 task_id'
python integrations/materials/call.py status $id
# 查询到 completed 后取回
python integrations/materials/call.py result $id --output result.json --save-markdown
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

安装后双击桌面的 **Worker8787.exe** 即可启动材料服务。控制台按编号列出全部执行通道，显示运行中数量、项目、当前动作、文件和已转写文档数。鼠标停留在运行中的行上可查看任务号。每 5 秒自动刷新，也可以点击“刷新状态”。最小化或点击“隐藏到托盘”会保留服务。双击托盘图标或桌面 EXE 可以再次打开窗口。点击窗口 × 或右击托盘选择 **退出并停止服务**，控制台将在材料进程退出后关闭。安装时可用 `-BaseUrl` 指定窗口里显示的调用地址。

主机重启后可双击 EXE 或从分支目录运行 `.\packaging\materials\start.ps1`。服务会继续调度排队任务，恢复已经写出的结果。运行途中停止且尚未发布结果的任务在下次启动时标为 `interrupted`。升级前等已接受的任务完成，退出托盘程序并停止服务，再运行安装脚本。

若首次启动日志显示 `pypandoc` 缺少 `get_pandoc_path`，检查服务账号对 `vendor\pypandoc\__init__.py` 和 `vendor\pypandoc\files\pandoc.exe` 的读取和执行权限。本机部署曾因依赖子目录没有继承 `vendor` 的访问权限而出现该错误。修复该实例依赖目录的权限继承后，重新运行启动脚本。

## 开发、测试与源码

材料服务的 Python 工程位于 `deployments/worker8788/source`。在该目录的 Python 3.12+ 虚拟环境中运行专项测试。安装 LibreOffice 后，可同时覆盖旧版 Word 转写和原件页图。

```powershell
Set-Location deployments/worker8788/source
python -m pip install -e ".[test,materials]"
python -X utf8 -m pytest -q -rs tests/test_materials.py tests/test_material_spreadsheet.py tests/test_material_pdf.py tests/test_material_text.py tests/test_material_references.py
```

测试覆盖按内容识别、CSV 多行记录、TXT 编码与换行、未知二进制错误、PDF 直传、原页文字和页图、扫描页核对、Word 转换、表格和图片交付、同会话读取多个文件、幂等重试、并发排队、零附件、损坏文件和重启恢复。测试使用模拟模型响应。验收实际模型时，用带有一条指定译法和一个例外条件的 Word 走一次快速试用，核对提示词是否包含两项要求，并检查 Markdown 是否保留原文和表格。[专项测试](deployments/worker8788/source/tests/test_materials.py)

| 入口 | 内容 |
| --- | --- |
| [材料 HTTP 服务](deployments/worker8788/source/src/codex_remote_worker/material_service.py) | 请求校验、鉴权、调度、任务状态和结果交付。 |
| [Python 联调客户端](integrations/materials/material_client.py) | 从提交到查询及按服务端文件名导出，和飞书示例使用同一脚本。 |
| [模型会话](deployments/worker8788/source/src/codex_remote_worker/material_agent.py) | 逐份读取、原页查看、提示词生成和交付校验。 |
| [材料转换](deployments/worker8788/source/src/codex_remote_worker/material_documents.py) | 内容识别、Word/Excel/PDF 转写、图片内嵌与页图生成。[CSV 与文本读取](deployments/worker8788/source/src/codex_remote_worker/material_text.py) |
| [8788 整单翻译 Worker](deployments/worker8788/README.md) | 同仓库中的翻译订单接口、逐句语言对、上下文 TM、审校与恢复。 |
| [早期 Worker 基线](deployment/current-runtime/README.md) · [整单接口历史说明](docs/WHOLE_TASK_API.md) · [第三方声明](NOTICE.md) | 根目录 `src/`、早期运行快照和历史接口的参考资料。 |
