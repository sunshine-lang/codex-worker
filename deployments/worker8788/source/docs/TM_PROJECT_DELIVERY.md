# 0.4.0：100% TM 与项目归档

在当前加固版上增量合并上游 `Worker_TM项目归档接入Spec.md`。未替换 runner、doctor、Windows 进程隔离、登录或模型配置。

## 协议

`POST /v1/jobs` 的鉴权、幂等键、四份文件与 options 不变。新增 `siemens-json/2`，保留 `/1` 与 `table-csv/1`。
根字段为 `contract, phase, project_name, request_id, rows, tm_reuse`；全局数字字符串 row_id 不重新编号。
模型只输出 rows；tm_reuse 的 selection 和 target_text 由 Worker 重新校验。
100 与 100.0 可复用；字符串、布尔值、非有限值、越界比例拒绝。锁定 TM 不因 TB 被改写。

纯 TM 仍创建 job，通过原鉴权/就绪检查后生成 `{"rows":[]}`，不调用 runner。run 中记录
`harness=tm-reuse, model_called=false, exit_code=0, thread_id=null`。不自动生成审校或返工 job。
外部完整 segments/zh 响应、TM 警告及语言审校仍由上游形成。

## 归档与安全

执行目录仍为 `Data/jobs/<job_id>`。归档位于 `Data/projects/<安全片段+哈希>/jobs/<job_id>`。
`project_archive` 为相对于 data_root 的实际归档目录；归档错误以 archive_error 明确返回，原件不删除。
固定 input role 文件名，复制时核对大小和 SHA-256；路径各级、元数据及最终文件拒绝符号链接/重解析点。
归档写入支持 Windows 扩展路径，不更改系统 LongPathsEnabled。项目名始终只是标签。
完成、失败、取消、重启恢复均更新归档；终态落库前完成最终归档尝试。

## 启动与发布

启动器按 `/2`、`/1`、`table-csv/1` 能力校验，不再锁死 0.3.0；新启动与复用进程均检查。
发布必须同步 EXE、工具 Python 包和 JSON 探针。原生探针只使用固定合成数据，不调用模型、不建任务。
`scripts/deploy-tm.local.py` 在维护标记、桌面控制锁、实例锁及 SQLite 写事务保护下备份和替换。
验证失败可恢复备份。模型、凭据、Codex 主目录配置和原任务数据不迁移。

参考包整体基于旧版；没有整体覆盖。补充了 selection 类型、归档元数据/产物重解析点、长路径、
并发终态与队列取消等保护；配套上游五条数据测试使用原协议模块与模拟 runner 验证。
