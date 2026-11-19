# Worker 8789

这个分支从 8788 的部署提交 325a308 建立。翻译代码在
deployments/worker8788/source/src/codex_remote_worker，8789 桌面控制台和安装脚本在
packaging/worker8789。工作树与安装目录分开，修改源码后需要重新部署才会生效。

在分支根目录使用管理员 PowerShell 7 构建和首次安装。

~~~powershell
.\packaging\worker8789\build.ps1
.\packaging\worker8789\install.ps1
~~~

桌面的 Worker8789.exe 显示本机地址、API Token、模型、思考等级和执行通道。
首次点击“登录 ChatGPT”可为本实例授权。查询 /v1/ready 时使用控制台显示的 Token。
Windows 管理员登录后，CodexWorker8789-Recovery 启动服务并定期检查监听状态。

实例安装在 C:\CodexRemoteWorker-AppServer-8789，任务和日志位于
C:\CodexWorkerData-AppServer-8789。配置在安装目录的 config.trial.json，
模型设置在数据目录的 private\codex-home。实例沿用机器上的 Worker 运行账号、
Python 和 Codex 可执行文件。
