# Worker 8789

这个分支从 8788 的部署提交 325a308 建立。翻译代码在
deployments/worker8788/source/src/codex_remote_worker，8789 桌面控制台和安装脚本在
packaging/worker8789。工作树与安装目录分开，修改源码后需要重新部署才会生效。

在分支根目录使用管理员 PowerShell 7 构建和首次安装。

~~~powershell
.\packaging\worker8789\build.ps1
.\packaging\worker8789\install.ps1
~~~

桌面的 Worker8789.exe 显示公网调用地址 http://52.62.69.192:8789，可复制地址和 API Token。
“刷新状态”通过本机接口读取服务生效的供应商、模型、思考等级与通道占用。这些字段在控制台中只读。
首次授权可在管理员 PowerShell 中运行安装目录下的 desktop\login.ps1。
Windows 管理员登录后，CodexWorker8789-Recovery 启动服务并定期检查监听状态。

实例安装在 C:\CodexRemoteWorker-AppServer-8789，任务和日志位于
C:\CodexWorkerData-AppServer-8789。配置在安装目录的 config.trial.json，
模型设置在数据目录的 private\codex-home。实例沿用机器上的 Worker 运行账号、
Python 和 Codex 可执行文件。

## 更新已安装的开发 Worker

在服务器的开发仓库根目录使用管理员 PowerShell 7。先退出 8789 桌面控制台，等现有开发订单完成。

~~~powershell
git switch codex/8789-dev
git pull --ff-only origin codex/8789-dev
.\packaging\worker8789\update.ps1
~~~

更新脚本针对 8789 的安装目录、任务库、监听进程和恢复计划。发现仍有活动订单时会退出。成功后查看 8789 的就绪状态：`ready=true`、`independent_app_server_processes=5`；`GET /v1/tasks/capabilities` 应返回 `task_stop=true`、`same_name_parallel=true` 和 `execution_slot_unit=whole_project`。Windows 原生更新、授权和真实模型验收需要在该主机上进行。

## 开发版调用

Langflow 的 `翻译流程MVP-v2-dev` ID 为 `9f26a456-0a79-4b62-9cfe-438006c339b3`。使用现有 advanced 请求格式，将下列业务对象序列化为 `input_value`，输出选 `ChatOutput-MVPStatus`。

~~~json
{"operation":"stop","task_id":"task_替换为开发订单实际编号"}
~~~

收到 `stopping` 后，用同一编号发送 `{"operation":"status","task_id":"…"}`，直到 `stopped`。整单停止是终态。重新翻译需新的提交键。排队的订单可直接返回 `stopped`；执行中的订单先中断当前 turn，确认清理后释放执行位。已经完成或失败的订单会返回原终态。旧 Worker 尚未安装此功能时，停止入口返回 `worker_upgrade_required`。

直连 8789 的接口为 `POST /v1/tasks/{task_id}/stop`（原 Worker Bearer 凭据）。HTTP 202 表示已请求停止，HTTP 200 表示返回终态。错误编号返回原任务错误。状态和停止请求均可重复。

开发版有五个执行位。同名、不同提交键的订单分别执行；每个需要模型的订单只创建一个 session job，所有批次、修订和自检都在该会话内完成。状态中的 `execution_slot` 在 Worker 取得执行信息后出现。全量 TM 直接复用沿用原有处理。验收时可提交六笔同名、需要模型的订单，停止一个正在执行的任务，检查等待订单接手空位，并核对其余订单的结果和任务编号。

维护 dev Flow 时，使用同目录的 `langflow_dev.py`，凭据取自 `LANGFLOW_API_KEY`；默认预览，`--apply` 仅更新开发副本并读回核对。
