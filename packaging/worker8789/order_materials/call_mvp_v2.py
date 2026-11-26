"""Standalone 8789 development caller. Python 3.10+, standard library only."""

import argparse
from datetime import datetime
from getpass import getpass
import gzip
import hashlib
from http.client import IncompleteRead
import json
import os
import re
from pathlib import Path
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
import uuid

from materials import MATERIALS_CONTRACT, materials_digest, materials_index, parse_materials_json

BASE = "http://218.60.7.98:7860"
FLOW = "9f26a456-0a79-4b62-9cfe-438006c339b3"
URL = BASE + "/api/v1/run/advanced/" + FLOW
SUBMIT = "ChatOutput-MVPResult"
STATUS = "ChatOutput-MVPStatus"


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


def load(path):
    return json.loads(Path(path).read_bytes())


def save(path, value):
    path = Path(path)
    tmp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w", encoding="utf-8") as f:
            json.dump(value, f, ensure_ascii=False, allow_nan=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)


def canonical_sha(value):
    data = json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    ).encode()
    return hashlib.sha256(data).hexdigest()


def read_materials(prompt, refs):
    def file_item(path):
        path = Path(path)
        return {"filename": path.name, "content": path.read_bytes().decode("utf-8")}

    materials = {"prompt_file": file_item(prompt), "reference_files": [file_item(p) for p in refs]}
    return parse_materials_json(json.dumps(materials, ensure_ascii=False))


class Client:
    def __init__(self, key):
        if not key:
            raise ValueError("A Langflow API key is required.")
        self.key = key
        self.opener = build_opener(ProxyHandler({}), NoRedirect())

    def request(self, req):
        for attempt in range(3):
            try:
                with self.opener.open(req, timeout=240) as resp:
                    raw = resp.read()
                    return json.loads(
                        gzip.decompress(raw) if resp.headers.get("Content-Encoding") == "gzip" else raw
                    )
            except HTTPError as exc:
                if attempt == 2 or exc.code not in (408, 425, 429, 500, 502, 503, 504):
                    raise
            except (URLError, ConnectionError, TimeoutError, IncompleteRead):
                if attempt == 2:
                    raise
            time.sleep(2**attempt)

    def call(self, business, *, submission_key=None, materials=None):
        operation = business.get("operation", "submit")
        output = SUBMIT if operation in ("submit", "replace") else STATUS
        node = {}
        if output == SUBMIT:
            if materials is None or submission_key is None:
                raise ValueError("A new order requires a submission key and its confirmed materials.")
            node = {
                "submission_key": str(uuid.UUID(submission_key)),
                "materials_json": json.dumps(
                    materials, ensure_ascii=False, allow_nan=False, separators=(",", ":")
                ),
            }
        body = {
            "inputs": [
                {
                    "components": ["ChatInput-MVPInput"],
                    "type": "chat",
                    "input_value": json.dumps(
                        business, ensure_ascii=False, allow_nan=False, separators=(",", ":")
                    ),
                }
            ],
            "outputs": [output],
            "stream": False,
            "tweaks": {
                "ChatInput-MVPInput": {"session_id": str(uuid.uuid4()), "should_store_message": False},
                output: {"should_store_message": False},
            },
        }
        if node:
            body["tweaks"]["CustomComponent-MVPInputs"] = node
        req = Request(
            URL,
            method="POST",
            data=json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
            headers={"x-api-key": self.key, "Content-Type": "application/json", "Accept-Encoding": "gzip"},
        )
        outer = self.request(req)
        values = [
            json.loads(n["results"]["message"]["text"])
            for g in outer.get("outputs", [])
            for n in g.get("outputs", [])
            if n.get("component_id") == output
        ]
        if len(values) != 1 or not isinstance(values[0], dict):
            raise ValueError("Missing unique task response.")
        value = values[0]
        if value.get("status") not in ("queued", "running", "stopping", "stopped", "completed", "failed"):
            error = value.get("error") or {}
            raise ValueError(f"{value.get('status')} {error.get('code', '')} {error.get('message', '')}")
        if output == STATUS and value.get("task_id") != business.get("task_id"):
            raise ValueError("Response belongs to a different order.")
        return value

    def control(self, operation, task):
        return self.call({"operation": operation, "task_id": task})


def prepare(folder, order, materials):
    if (
        not isinstance(order, dict)
        or order.get("operation", "submit") != "submit"
        or not isinstance(order.get("segments"), list)
    ):
        raise ValueError("Expected an order with project_name and segments.")
    if not isinstance(order.get("project_name"), str) or not order["project_name"].strip():
        raise ValueError("Expected a named project.")
    key = str(uuid.uuid4())
    folder.mkdir(mode=0o700, parents=True, exist_ok=False)
    save(folder / "request.json", order)
    save(folder / "materials.json", materials)
    save(
        folder / "state.json",
        {
            "flow_id": FLOW,
            "submission_key": key,
            "task_id": None,
            "input_sha256": canonical_sha(order),
            "materials_sha256": materials_digest(materials_index(materials)),
        },
    )


def accept(client, folder):
    state = load(folder / "state.json")
    if state.get("flow_id") != FLOW:
        raise ValueError("The result directory belongs to a different Flow.")
    if state.get("task_id"):
        if not re.fullmatch(r"task_[a-f0-9]{32}", state["task_id"]):
            raise ValueError("Invalid task_id in the saved state.")
        return state
    if not state.get("submission_key") or not state.get("materials_sha256"):
        raise ValueError("旧格式开发订单没有任务号或随单资料，请先核对原提交回执。")
    uuid.UUID(state["submission_key"])
    order, materials = load(folder / "request.json"), load(folder / "materials.json")
    if (
        canonical_sha(order) != state["input_sha256"]
        or materials_digest(materials_index(materials)) != state["materials_sha256"]
    ):
        raise ValueError("Saved submission inputs changed.")
    ack = client.call(order, submission_key=state["submission_key"], materials=materials)
    if (
        not re.fullmatch(r"task_[a-f0-9]{32}", str(ack.get("task_id", "")))
        or ack.get("project_name") != order["project_name"]
        or ack.get("materials_sha256") != state["materials_sha256"]
        or ack.get("materials_contract") != MATERIALS_CONTRACT
    ):
        raise ValueError("Submission receipt does not match this order and its materials.")
    save(folder / "receipt.json", ack)
    state["task_id"] = ack["task_id"]
    save(folder / "state.json", state)
    return state


def deliver(client, folder, state):
    final = client.control("result", state["task_id"])
    if (
        final.get("status") != "completed"
        or final.get("result_available") is not True
        or final.get("worker_result_hash_verified") is not True
        or final.get("business_result_restored") is not True
        or (state.get("materials_sha256") and final.get("materials_sha256") != state["materials_sha256"])
        or (
            final.get("progress", {}).get("model_required_segments")
            and final.get("self_review_status") != "completed"
        )
    ):
        raise ValueError("Incomplete delivery or material mismatch.")
    order = load(folder / "request.json")
    result = final["result"]
    # Historical 8789 callers used json.dumps' default separators for their input digest.
    input_sha = canonical_sha(order)
    if "materials_sha256" not in state and "idempotency_key" in state:
        input_sha = hashlib.sha256(
            json.dumps(order, ensure_ascii=False, sort_keys=True, allow_nan=False).encode()
        ).hexdigest()
    if input_sha != state["input_sha256"] or canonical_sha(result) != final.get("business_result_sha256"):
        raise ValueError("Input/result digest mismatch.")
    if len(result["segments"]) != len(order["segments"]) or (
        {**result, "segments": order["segments"]} != order
    ):
        raise ValueError("Business envelope or segment count changed.")
    for orig, row in zip(order["segments"], result["segments"]):
        if (
            not isinstance(row.get("target_text"), str)
            or not row["target_text"].strip()
            or {**row, "target_text": orig["target_text"]} != orig
        ):
            raise ValueError("Empty target or a changed non-translation field.")
    save(folder / "result.json", result)
    save(folder / "delivery.json", {k: v for k, v in final.items() if k not in ("result", "tm_decisions")})
    return final


def run(client, folder):
    state = accept(client, folder)
    deadline = time.monotonic() + 10800
    last = None
    print("task_id =", state["task_id"], flush=True)
    while time.monotonic() < deadline:
        v = client.control("status", state["task_id"])
        save(folder / "latest-status.json", v)
        info = (
            v["status"],
            v.get("stage"),
            (v.get("progress") or {}).get("finished_segments"),
            v.get("codex_thread_id"),
        )
        if info != last:
            print(*info, flush=True)
            last = info
        if v["status"] == "completed":
            deliver(client, folder, state)
            print(folder / "result.json")
            return
        if v["status"] == "stopped":
            return
        if v["status"] == "failed":
            raise ValueError(str(v.get("error")))
        time.sleep(5)
    raise TimeoutError("Local wait timed out. Resume using the same output folder.")


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    g = cli.add_mutually_exclusive_group(required=True)
    g.add_argument("--input", type=Path)
    g.add_argument("--resume", type=Path)
    g.add_argument("--status")
    g.add_argument("--stop")
    cli.add_argument("--prompt-file", type=Path)
    cli.add_argument("--reference-file", type=Path, action="append", default=[])
    cli.add_argument("--out", type=Path)
    a = cli.parse_args()
    key = os.environ.get("LANGFLOW_API_KEY", "").strip()
    key_file = Path(__file__).with_name("langflow_api_key.txt")
    if not key and key_file.is_file():
        key = key_file.read_text(encoding="utf-8-sig").strip()
    key = key or getpass("Langflow API key: ").strip()
    client = Client(key)
    if not a.input and (a.prompt_file or a.reference_file or a.out):
        cli.error("File arguments accompany --input only.")
    if a.status or a.stop:
        task = a.status or a.stop
        if a.stop:
            val = client.control("stop", task)
            end = time.monotonic() + 600
            while val["status"] == "stopping" and time.monotonic() < end:
                time.sleep(3)
                val = client.control("status", task)
        else:
            val = client.control("status", task)
        print(json.dumps(val, ensure_ascii=False, indent=2))
        if a.stop and val["status"] not in ("stopped", "completed", "failed"):
            raise TimeoutError("Stop not confirmed.")
        return
    folder = a.resume
    if a.input:
        if not a.prompt_file:
            cli.error("--input requires --prompt-file")
        folder = a.out or Path("build/8789-runs") / (
            "dev-" + datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
        )
        prepare(folder, load(a.input), read_materials(a.prompt_file, a.reference_file))
    print("8789 result folder:", folder.resolve(), flush=True)
    run(client, folder)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
    except (ValueError, OSError, HTTPError) as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
