"""Local Windows post-update check. No model call or order submission."""

import json
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


def main():
    data = Path(r"C:\CodexWorkerData-AppServer-8789")
    if not (data / ".worker8789").is_file():
        raise ValueError("Not an installed 8789 instance.")
    token = json.loads((data / "private/credentials.local.json").read_text(encoding="utf-8-sig"))[
        "CODEX_WORKER_SERVICE_TOKEN"
    ]
    op = build_opener(ProxyHandler({}), NoRedirect())

    def get(path):
        req = Request("http://127.0.0.1:8789" + path, headers={"Authorization": "Bearer " + token})
        with op.open(req, timeout=5) as r:
            return json.load(r)

    baseline = json.loads((Path(__file__).with_name("runtime_base.json")).read_text())["commit"]
    deadline = time.monotonic() + 120
    last = "waiting"
    while time.monotonic() < deadline:
        try:
            ready = get("/v1/ready")
            caps = get("/v1/tasks/capabilities")
            ok = (
                ready.get("ready") is True
                and caps.get("order_materials_contract") == "order-materials/1"
                and caps.get("source_baseline_commit") == baseline
                and ready.get("max_concurrent_jobs") == 5
                and caps.get("same_name_parallel") is False
                and caps.get("project_thread_continuity") == "source_file.project_name"
                and all(
                    caps.get(k) is True
                    for k in (
                        "strict_project_order",
                        "task_stop",
                        "task_resume",
                        "replace_in_place",
                        "auto_resume_interrupted_predecessor",
                    )
                )
            )
            if ok:
                print("8789 ready: project FIFO, same thread, resume/replace, order-materials/1")
                return
            last = "capability mismatch"
        except (URLError, OSError, ValueError) as exc:
            last = type(exc).__name__
        time.sleep(3)
    raise SystemExit("8789 did not confirm new runtime: " + last)


if __name__ == "__main__":
    main()
