# 检查点恢复与供应商兼容

## 已保存进度

`project-session-state.json` 保存任务/Job/原始计划绑定、译文、版本、自检版本和项目笔记。有效工具调用后持久化，Windows 原子保存仅对短暂共享冲突做有限重试。无效检查点不会被清空或覆盖。

允许乱序翻译。恢复时逐项检查行 ID、非空译文、版本、任务绑定和文件指纹；恢复的模型必须重新读取当前资料，规则检查也需重新读取。正常模型轮次提前结束由控制器在同一会话续行；实际执行失败不会自动排队重试。

## 显式恢复

使用已部署服务的 Python 和源码路径，在服务空闲且模型配置与原任务一致时执行：

```powershell
python -m codex_remote_worker.project_recovery --config C:\CodexRemoteWorker-AppServer-8788\config.trial.json --task-id task_REPLACE_WITH_FAILED_TASK_ID
```

命令先备份旧记录及执行材料，校验原输入和检查点，再将任务重排；命令本身不调用模型。存在其他未完成任务、绑定不一致或模型配置已变化时拒绝恢复。切换供应商后不能直接将旧任务当作原配置继续。

优先通过 `thread/resume` 恢复原会话。当前 Windows App Server 若在模型轮次开始前明确返回运行环境初始化错误 `app runtime provisioning service is unavailable; refusing helper fallback`，才允许在相同受限权限下创建无本地环境的新会话，从已验证检查点继续；记录新旧会话关联及 `history_restored=false`。其他 RPC 错误不会触发此兜底。检查点数据保留不等于旧聊天历史已搬入新会话。

## 官方订阅与第三方供应商

桌面控制器从当前用户 Codex 配置、认证和可用模型目录读取供应商信息，不读取 cc-switch 数据库。应用设置时同步至 8788 的专用 Codex home；运行任务不直接使用个人 Codex home。设置变更仅允许在服务空闲时进行，失败回滚原配置。

官方订阅使用 ChatGPT 登录状态。第三方供应商须支持 Responses 和项目所需工具调用；OpenRouter 目录用于筛选可用工具模型，不凭通用 reasoning 支持猜测所有思考等级。

项目会话不发送最终状态 `outputSchema`，避免已观察到的第三方接口直接生成状态而跳过工具的组合问题。译文结构和完成条件仍由 Worker 校验。完整工具结果不再重复写入 App Server INFO 正文日志，大消息回传保留独立传输容量检查。

上下文覆盖保存在 8788 专用 `config.toml`。桌面“应用”操作会重新生成服务配置，之后应重新核对这些覆盖值；需要恢复本次 872,000 / 784,800 设置时，可在空闲状态运行仓库的 `tools/set_worker8788_context.py`。
