"""An explicitly confirmed one-row real translation; never runs on package creation/start."""
import json
from pathlib import Path
import time
from urllib.request import Request, ProxyHandler, build_opener
import uuid


def main():
    root = Path(__file__).resolve().parents[1]
    if input("这会提交一条真实翻译并使用当前 Codex 订阅额度。输入 YES 继续：").strip() != "YES":
        print("已取消，未提交任务。")
        return
    config = json.loads((root / "Data/config.local.json").read_text(encoding="utf-8-sig"))
    token = json.loads((root / "Data/private/credentials.local.json").read_text(encoding="utf-8-sig"))["CODEX_WORKER_SERVICE_TOKEN"]
    base = "http://127.0.0.1:" + str(config["port"])
    opener = build_opener(ProxyHandler({}))
    def request(path, data=None, headers=None):
        req = Request(base + path, data=data, headers={"Authorization": "Bearer " + token, **(headers or {})})
        with opener.open(req, timeout=30) as response:
            return response.read()
    ready = json.loads(request("/v1/ready"))
    if not ready.get("ready"):
        raise RuntimeError("Worker is not ready")
    boundary = "worker-demo-" + uuid.uuid4().hex
    parts = []
    for role, filename in (("source_file", "input.json"), ("prompt_file", "prompt.md"),
                           ("project_style", "project.md"), ("general_style", "general.md")):
        parts.append(("--" + boundary + '\r\nContent-Disposition: form-data; name="' + role + '"; filename="' + filename +
            '"\r\nContent-Type: application/octet-stream\r\n\r\n').encode() + (root / "examples" / filename).read_bytes() + b"\r\n")
    parts.append(("--" + boundary + '\r\nContent-Disposition: form-data; name="options"\r\n\r\n{"timeout_seconds":600}\r\n').encode())
    parts.append(("--" + boundary + "--\r\n").encode())
    key = str(uuid.uuid4())
    (root / "Data/test-submission.json").write_text(json.dumps({"idempotency_key": key}), encoding="utf-8")
    # One submission, no automatic POST retry on an ambiguous network timeout.
    job = json.loads(request("/v1/jobs", b"".join(parts), {"Content-Type": "multipart/form-data; boundary=" + boundary,
                            "Idempotency-Key": key}))
    job_id = job["job_id"]
    print("任务：" + job_id)
    (root / "Data/test-submission.json").write_text(json.dumps({"idempotency_key": key, "job_id": job_id}), encoding="utf-8")
    deadline, previous = time.monotonic() + 660, None
    while time.monotonic() < deadline:
        job = json.loads(request("/v1/jobs/" + job_id))
        status = job["status"]
        if status != previous:
            print("状态：" + status, flush=True)
            previous = status
        if status in ("completed", "needs_review"):
            artifact = next(a for a in job["artifacts"] if a["kind"] == "translation")
            path = artifact["download_path"]
            if not path.startswith("/v1/jobs/" + job_id + "/artifacts/"):
                raise RuntimeError("Unexpected artifact route")
            result = request(path)
            json.loads(result)
            destination = root / "Data/test-result.json"
            destination.write_bytes(result)
            print("真实翻译及结果下载成功：" + str(destination))
            return
        if status in ("failed", "cancelled", "interrupted"):
            print(json.dumps(job.get("error"), ensure_ascii=False))
            raise RuntimeError("Test job did not complete")
        time.sleep(3)
    raise RuntimeError("Polling timed out; inspect the existing job instead of submitting repeatedly")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print("测试未完成。请先检查服务状态，再查看 Data/test-submission.json 中的既有任务及日志；未自动重新提交，不要反复双击。")
        raise SystemExit(1)
