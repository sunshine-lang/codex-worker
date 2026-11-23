# 项目材料预处理 Worker

这个服务将一次材料提交分配给一个执行位和一个 Codex 会话。输入可以没有附件，也可以包含一份或多份 Word 原件。返回一份项目翻译 Prompt 和逐文件原文 Markdown。任务状态保存在材料服务自己的 SQLite 中。

## 在现有 8789 Windows 服务器部署

在仓库的 `codex/materials-preprocess-worker` 分支，以管理员身份打开 PowerShell 7。安装器读取已配置好的 8789 模型、Codex 登录和服务账号配置，在端口 8790 启动独立服务。安装目录为 `C:\CodexRemoteWorker-AppServer-8790`，材料任务数据在 `C:\CodexWorkerData-AppServer-8790`。默认支持五笔提交并行，同名项目的不同提交各自拥有任务号、文件目录和会话。

1. 安装 LibreOffice，以便转写旧版 Word 和渲染原件页图。标准安装位置会被自动识别，自定义位置可设置 `MATERIALS_SOFFICE` 指向 `soffice.com`。安装器使用 uv、系统 Python 启动器或现有 Python 的 pip 将附加依赖装到 8790 专属目录。
2. 执行以下命令。`-LangflowAddress` 可以传 Langflow 的实际出口 IP 或网段，用于配置该来源到 8790 的 Windows 防火墙规则。使用已有反向代理或已有放行规则时可省略此项。公网安全组如适用，也需要允许 Langflow 到达配置的 Worker 地址。
3. 将专用令牌填入 Langflow 的“项目材料 Worker”节点并保存。也可以在 Langflow 服务环境设置 `MATERIALS_WORKER_TOKEN`。已部署画布使用同一开发服务器的 8790 地址，另一个部署地址可通过 Langflow 环境变量 `MATERIALS_WORKER_URL` 指定。通过 HTTPS 代理提供服务时填写代理地址。

~~~powershell
.\packaging\materials\install.ps1 -Slots 5
.\packaging\materials\start.ps1
~~~

安装器仅在首次安装时生成专用令牌。部署人员可在服务器上将它复制到剪贴板，随后粘贴到 Langflow 的密钥输入框。

~~~powershell
$privateConfig = 'C:\CodexWorkerData-AppServer-8790\private\credentials.local.json'
(Get-Content -LiteralPath $privateConfig -Raw | ConvertFrom-Json).CODEX_WORKER_SERVICE_TOKEN | Set-Clipboard
~~~

启动脚本会调用受认证的 `GET /v1/ready`，检查 HTTP 服务和通道配置。首次真实模型验收使用下文的材料提交。运行时日志位于材料数据目录的 `serve.stderr.log`。主机重启后可再次运行启动脚本。已有排队任务继续调度，已发布的结果可用原任务号找回，中断的在途任务显示 `interrupted`，需要重新处理时使用新提交键。

## Langflow 与上游契约

流程是请求输入 → 项目材料 Worker → 任务响应。可移植的画布在 [flow.json](../../integrations/materials/flow.json)，节点源码在 [component.py](../../integrations/materials/component.py)。导入新实例时，通过 Langflow 服务环境设置 `MATERIALS_WORKER_URL`，再配置独立令牌。运行入口使用 `POST /api/v1/run/<flow_id>`，以聊天输入传入序列化的业务 JSON，输出组件是 `ChatOutput-Materials`。

以下为示例提交。将示意的 Base64 替换为文件内容。首版 `translation_requirements` 接受文字，可传空字符串。项目名是标识，`documents` 默认为空列表。文件支持 `.doc` 和 `.docx`，附件原始字节合计上限为 32 MiB。

~~~json
{
  "operation": "submit",
  "submission_key": "bc9f05dd-d73c-416c-8a7c-cdb024083ce4",
  "project_name": "设备手册",
  "business_tag": "IM",
  "source_language": "en",
  "target_language": "zh-CN",
  "translation_requirements": "面向现场工程师",
  "documents": [
    {"source_filename": "客户指南.docx", "content_base64": "<文件的Base64>"}
  ]
}
~~~

Langflow 也可接收当前调用者已上传的文件，在附件对象里以 `file_path` 替代 `content_base64`，并用 `source_filename` 保留原名。聊天附件和业务 JSON 附件选择一种传递方式。直连 Worker 时使用文件名和 Base64 内容。

| 操作 | 用户请求 | Worker HTTP | 成功结果 |
|---|---|---|---|
| 提交 | 上述 JSON | `POST /v1/material-tasks` | HTTP 202，取得 `task_id` 和 `submission_key`。同键同内容复用原任务，同键改内容返回 409。 |
| 查询 | `{"operation":"status","task_id":"mat_…"}` | `GET /v1/material-tasks/{task_id}` | 排队、执行、完成或失败状态，同时返回阶段、当前文件、文档完成数、最近动作与更新时间。 |
| 取回 | `{"operation":"result","task_id":"mat_…"}` | `GET /v1/material-tasks/{task_id}/result` | 完成后返回 `translation_prompt` 字符串和 `documents_markdown` 数组。数组顺序与输入一致，元素含 `source_filename` 和 `markdown`。 |

Worker 请求使用 `Authorization: Bearer <专用令牌>`。运行 Langflow 时要读取任务响应 JSON 的 `error.code`，Langflow 外层的 HTTP 200 只表示画布完成了本次调用。上传已接收但网络断开时，用相同提交键和同一组原件重试。已获得任务号则直接查询。提供了附件但解析失败时，状态中会给出失败文件和错误，结果接口不会当作空附件交付。

## 首次验收

调用脚本使用 Python 标准库。先在本地设置 `MATERIALS_FLOW_URL` 为完整的 Langflow 运行入口；`LANGFLOW_API_KEY` 未设置时，脚本会隐藏输入。两个本地 Word 路径换成实际路径。

~~~powershell
python integrations/materials/call.py submit --project-name 西门子 --business-tag IM --source-language en --target-language zh-CN --requirements "设备手册" --document "EC通用准则.doc" --document "西门子指南.docx"
python integrations/materials/call.py status <返回的任务号>
python integrations/materials/call.py result <返回的任务号> --output result.json
~~~

检查两份 Markdown 是否对应原文件、术语表的行列和例外是否保留。图片放在原位置并内嵌到结果，Codex 可以通过材料工具查看原件页图。再用新提交键去掉客户指南重跑，核对客户专属要求是否随材料变化。单独验证零附件时，预期得到该业务标签和语向的 Prompt 以及空文档数组。
