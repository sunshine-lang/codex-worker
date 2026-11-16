# JSON 输入与最终回复交付（0.3.0，本机加固合并版）

日期：2026-11-11。仅合并 JSON 协议与运行工具分流；保留现有沙箱、启动器、缓存、审计、维护保护和 CSV/XLSX 流程。

## 接口不变

仍通过 `POST /v1/jobs` multipart 上传 source_file、prompt_file、project_style、general_style 和 options，使用原认证、幂等键、异步查询和产物下载接口。JSON 不是新增的同步 HTTP 翻译接口。

`GET /v1/ready` 返回 `service_version: "0.3.0"` 和 `input_contracts: ["siemens-json/1", "table-csv/1"]`。

## 翻译输入示例（source_file 为 .json）

```json
{
  "contract": "siemens-json/1",
  "phase": "translate",
  "rows": [
    {
      "row_id": "00001",
      "record": {
        "source_language": "en",
        "target_language": "zh-CN",
        "source_text": "Hello",
        "target_text": "",
        "context": null,
        "tb_list": [],
        "tm_list": []
      }
    }
  ]
}
```

输入顶层必须为 contract、phase、rows，行数 1..10000，row_id 为唯一非空字符串。本协议只支持 en → zh-CN；record 可包含原始业务字段，工作副本与原件不允许模型修改。tb_list/tm_list 每项需包含非空 source_text、target_text。

## 模型翻译最终回复

```json
{"rows":[{"row_id":"00001","target_text":"你好","term_decisions":[]}]}
```

只允许纯 JSON，顶层及每行字段严格检查。行号须与输入一一对应，译文不可为空，不接受 Markdown 代码块、说明文字、缺行、重复行、额外行或额外输出字段。term_decisions 每项只包含非空 source_text、target_text。

由 Worker 校验最终消息后保存为 `<输入文件名>_result.json` 和 final_response.json。JSON 任务不调用 CSV builder；CSV/XLSX 任务继续原有交付方式。不合格回复标记 failed / INVALID_JSON_RESULT，不发布 translation 产物；本次没有增加自动模型重试。

原始业务字段保存在输入与任务快照中；输出只包含约定结果字段，调用方按 row_id 回填，不能按返回位置盲目拼接。

## 可选审校协议

phase=review 时输入 record.target_text 必须非空，最终每行字段严格为 row_id、decision、issues、coverage、new_senses。decision 支持 pass/revise/needs_review；coverage 每项为整数 id 和非空 evidence；new_senses 每项为 source_text、target_text、reason。非 pass 需有 issues，审校不能在结果中直接返回改写 target_text。

Worker 只承接单阶段任务和结构验证，不自动安排独立审校、修改或重试。结构合格不等于语言质量合格；审校闭环由调用方负责。本次未修改 Langflow 节点或用户 Prompt。

## 构建与验证

自动回归：149 passed、3 skipped。真实非管理员服务账户的 EXE 沙箱 9 项探测及 JSON 工具探测通过；真实 HTTP 拒绝重复输入行号已验证。本次验收未调用真实翻译模型，耗时以用户后续业务调用为准。

构建产物：C:/Users/Administrator/Documents/codex-remote-worker/dist/windows-exe/CodexRemoteWorker/CodexRemoteWorker.exe。
实际部署：C:/CodexRemoteWorker/bin/CodexRemoteWorker/CodexRemoteWorker.exe。
EXE 为 onedir 构建，不能脱离同目录的 _internal 文件夹单独分发。桌面服务启动器保持原有版本与入口。
