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
