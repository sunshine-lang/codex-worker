# 有界翻译并发升级

基线：用户确认在用的 Git commit `207e1b42be51c0da4786c11ec70dc4320bbdc168`。
接口兼容 `translation-task/1` 和原 `/v1/jobs`。运行版本仍属于0.4.0系列，是否安装本次增量请以
`scheduler_version: bounded-concurrency/1`、实际配置和构建哈希判断，不能只看 `service_version`。

## 改动

数据库在一个事务中按总上限认领任务；Worker维护有界的执行任务集合；整单调度器按每单窗口派发多个批次。
批次编号、子job幂等键和结果文件名均绑定原计划索引，允许乱序完成，最终仍按原记录顺序合并。
100% TM仍由程序原样复用，不进入模型。启动器、Windows权限策略、模型选择和已有完整上下文工具保持原逻辑。

日志及准备进程的阻塞管道读取使用各任务独立线程池，避免高并发占满启动/终止任务所用的控制线程池。
取消一个job不取消其他订单；服务停止会等待所有已拥有的执行任务清理进程树。

一批失败后停止新派发、取消尚未启动的兄弟子任务，让已启动批次收尾并保存成功产物，最终整单返回失败。
不会自动重跑失败或状态不明的翻译任务；服务重启沿用已有子job，已完成批次不重复计数。
这不是新的整单失败重试API，也不改变独立语义审校的职责。

## 配置

只在现有服务端配置JSON中增加下面两项，其他字段保持现状：

```json
{
  "max_concurrent_jobs": 2,
  "task_batch_concurrency": 2
}
```

- `max_concurrent_jobs`：整个Worker的执行上限，包含运行、交付检查和取消清理中的job。
- `task_batch_concurrency`：单个整单任务可同时派发的子批次数，不能大于总上限。
- 两项都默认1，范围1～16。仅设置总上限不会让同一大订单自动多批并行；需要一起设置。
- 并发由服务器配置；HTTP `options` 仍只接受 `timeout_seconds`，调用方不能提高并发额度。
- `queue_capacity` 仍限制等待队列及活动订单容量。队列满时等待，不把订单误报为执行失败。
- 新订单保存提交时的每单窗口；降低服务总上限时仍按较小值调度。旧未完成订单按稳定批次索引恢复。

建议先用2路验证实际Windows进程隔离、模型网关限流、内存和耗时，再逐级测试4、8及更高值。
16是程序允许的上界，不是当前机器或模型服务已经验收的容量。1344条5分钟目标尚未验证，不能从模拟测试推断。

## 构建与部署

当前部署脚本优先运行 `bin/CodexRemoteWorker/CodexRemoteWorker.exe`。
**仅修改src或pip安装源码不会替换冻结EXE。** 本包不自动停止服务、修改现有配置、覆盖EXE或清理数据。

1. 将本次源码包解压到独立构建目录，或在上述基线检出上应用随包补丁。不要使用旧0.4.0迁移ZIP覆盖当前服务器。
2. 在Windows使用与当前运行环境兼容、已安装项目依赖及PyInstaller的Python执行：

   ```powershell
   .\scripts\build-worker-runtime.ps1 -Python 'C:\你的构建Python\python.exe'
   ```

   产物为 `dist/concurrency-runtime/CodexRemoteWorker/` 整个目录；旁边会生成带源码和全部运行文件哈希的
   `concurrency-build-manifest.json`。不会重建或替换现有桌面启动器。

3. 等已有订单结束，使用服务器现有启动器停止旧Worker，核对旧进程和监听端口已退出。备份原配置和整个旧Worker运行目录。
4. 把新构建的 **整个** `CodexRemoteWorker` 目录替换到现有 `bin/CodexRemoteWorker`，包含其内部依赖文件，不能只拷贝EXE。
   配置只增加两项并发参数，不覆盖Token、Codex登录配置、账户、已有沙箱设置或上传文档。
5. 沿用服务器当前安装的启动器启动。更改配置会影响现有自检指纹；现有Serve启动器会执行Doctor，手工启动时也须先完成原有Doctor自检。
   `deployment/current-runtime/` 是旧部署取证副本，不要拿这些副本覆盖在用启动脚本。
6. 通过现有认证读取 `/v1/ready` 和 `/v1/tasks/capabilities`。必须看到新调度器标识及实际并发数，再进行真实小批量联调。

不要删除 `jobs.sqlite3`、`tasks.sqlite3`、`jobs/`、`tasks/`、项目归档、模型配置或登录文件。
如果构建依赖不足，先补齐构建环境，不要通过删除当前EXE强制切换到未经验证的Python启动路径。

## 运行验收

`GET /v1/tasks/capabilities` 应新增：

```json
{
  "scheduler_version": "bounded-concurrency/1",
  "bounded_parallel_execution": true,
  "concurrency": {
    "max_concurrent_jobs": 2,
    "task_batch_concurrency": 2
  }
}
```

任务查询保留 `active_worker_job_id`，并增加 `active_worker_job_ids`、`completed_batch_indices`、`batch_concurrency`。
`completed_batches` 现在明确表示已完成批次数，不表示从第0批起连续完成的前缀。

先用已有64条真实测试集，确认两个翻译子job的 `started_at` / `completed_at` 时间区间确实重叠。
再检查总活动数不超过2、原字段与顺序不变、TM原样、完整行覆盖、取消及重启行为，最后比较翻译/审校/总耗时。
模拟Runner的并发峰值只能证明程序调度逻辑，不能代替Windows/Codex实机或语言质量验收。

## 回滚

等待正在运行的批次收尾并停止服务，恢复备份的整个Worker运行目录和配置，再用原启动器启动及自检。
建议先以并发1观察。新记录保留原有字段，默认并发1兼容旧操作；但已经乱序完成的活动订单不能交给旧的串行cursor实现继续运行。
因此回滚前须让本次新增订单达到终态，或隔离其记录/产物供人工恢复，不能删除数据或假定旧版能接续所有新中间状态。
