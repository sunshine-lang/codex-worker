# 8789 上游对接示例

8789 允许多笔订单使用同一个 `project_name`。上游为每笔业务订单分配一个稳定的 UUID 作为 `Idempotency-Key`，提交成功后保存 Worker 返回的 `task_id`。用户点击某一行的“停止”时，从这一行取出 `task_id` 发请求。即使两行显示的项目名完全相同，也能单独停止。

## 地址和接口

Worker 基址为 `http://52.62.69.192:8789`。从服务器桌面的 **Worker8789.exe → 复制 Token** 获取该实例的凭据，放入上游后端环境变量 `WORKER_8789_TOKEN`。每个请求都带 `Authorization: Bearer <token>`。该公网地址使用明文 HTTP，跨公网传输真实令牌和订单时宜通过受保护的网络通道。

| 操作 | HTTP 请求 | 上游处理 |
|---|---|---|
| 提交一笔订单 | `POST /v1/tasks` | 每次用 multipart 上传一整单。首次受理返回 `202`，保存 `task_id` |
| 查询一笔订单 | `GET /v1/tasks/{task_id}` | 读取这单的 `status`。运行时可查看 `execution_slot` |
| 停止一笔订单 | `POST /v1/tasks/{task_id}/stop` | 请求体留空。返回 `202 stopping` 时，继续查询同一个 `task_id` |
| 下载一笔结果 | `GET /v1/tasks/{task_id}/result` | `completed` 后下载。已停止订单查询结果返回 `409` |

## 可运行的 Python 示例

需要 Python 3 和 `httpx`（`python -m pip install httpx`）。将以下代码保存为 `demo_8789.py`，设置好令牌后运行 `python demo_8789.py team1-demo-001`。参数表示这一组示例订单的业务编号前缀。首次运行会实际提交 A、B 两单，并请求停止 A。重跑同一组使用相同提交键，要演示两笔新单就换一个前缀。

```python
import json
import os
import sys
import time
import uuid

import httpx


def submit(client, order_no, source_text):
    # 同一笔业务订单重试时使用相同 UUID，不同订单号得到不同 UUID。
    key = str(uuid.uuid5(uuid.NAMESPACE_URL, "worker8789/upstream-demo/" + order_no))
    source = {
        "project_name": "设备手册",
        "segments": [{
            "segment_id": "1",
            "source_text": source_text,
            "source_language": "en",
            "target_language": "zh",
            "target_text": "",
            "tm_list": [],
            "tb_list": [],
        }],
    }
    files = {
        "source_file": ("source.json", json.dumps(source, ensure_ascii=False).encode("utf-8"), "application/json"),
        "prompt_file": ("prompt.md", b"Translate into Simplified Chinese.", "text/plain"),
        "project_style": ("project.md", b"Use concise technical wording.", "text/plain"),
        "general_style": ("general.md", b"Keep equipment identifiers unchanged.", "text/plain"),
    }
    response = client.post("/v1/tasks", headers={"Idempotency-Key": key}, files=files,
                           data={"options": json.dumps({"language_mode": "per-segment", "timeout_seconds": 360})})
    response.raise_for_status()
    return response.json()["task_id"]


def status(client, task_id):
    response = client.get(f"/v1/tasks/{task_id}")
    response.raise_for_status()
    return response.json()["status"]


if __name__ == "__main__":
    prefix = sys.argv[1]
    with httpx.Client(base_url="http://52.62.69.192:8789", timeout=30,
                      headers={"Authorization": "Bearer " + os.environ["WORKER_8789_TOKEN"]}) as client:
        task_a = submit(client, prefix + "-A", "Close the cover of unit WX101.")
        task_b = submit(client, prefix + "-B", "Open the inlet valve of unit WX201.")
        print("订单 A", task_a, "订单 B", task_b)

        # 只取订单 A 的 task_id；B 继续按自己的状态执行。
        reply = client.post(f"/v1/tasks/{task_a}/stop")
        reply.raise_for_status()
        state_a = reply.json()["status"]
        print("A 停止回执", reply.status_code, state_a)
        for _ in range(30):
            if state_a != "stopping":
                break
            time.sleep(2)
            state_a = status(client, task_a)
        print("A 当前状态", state_a, "B 当前状态", status(client, task_b))
```

## 上游如何处理重试和停止

- **创建和重试**：建议保存 `上游订单号 → 提交键、Worker 实例、task_id`。首次提交超时也可用原键、原文件和原选项重发。相同提交会返回 `200`、原 `task_id` 和 `reused_submission: true`。相同键携带不同输入会返回 `409 IDEMPOTENCY_CONFLICT`。已停止的订单若要重新翻译，为新的执行分配新键。
- **并行**：两次提交只需分别等到受理回执，即可同时在途。当前 8789 有 5 个整单通道，通道已满时新单排队。每笔订单的停止按钮始终绑定本行的 `task_id`，通道释放后可由后续订单复用。
- **停止回执**：排队单可直接返回 `200 stopped`。运行单通常先返回 `202 stopping`，上游应显示“正在停止”，轮询到 `stopped` 再显示“已停止”。示例轮询最多约一分钟，若仍是 `stopping`，保留该状态，后续继续查询。若订单已经完成或失败，停止接口返回 `200` 和原终态。重复请求停止同一笔订单是安全的。

若上游通过 Langflow 开发流程“翻译流程MVP-v2-dev”调用，按仓库约定将 `{"operation":"stop","task_id":"<订单 A 的实际 task_id>"}` 序列化为已有 advanced 请求的 `input_value`，输出选 `ChatOutput-MVPStatus`。查询时将 `operation` 改为 `status`。上面的 Python 示例直接调用 Worker。
