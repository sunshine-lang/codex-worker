"""Preview or publish the order materials graph on the IM 8789 development Flow."""

import argparse
import copy
from getpass import getpass
import hashlib
import json
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

BASE = "http://218.60.7.98:7860"
DEV = "9f26a456-0a79-4b62-9cfe-438006c339b3"
PROJECT = "47bc7ec5-e7bb-4304-8b0c-19298c6ace05"
WORKER = "http://52.62.69.192:8789"
FILES = Path(__file__).resolve().parent / "order_materials"
NODES = ("CustomComponent-MVPInputs", "CustomComponent-MVPSubmit", "CustomComponent-MVPQuery")
CONTRACT = "order-materials/1"


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


def request(url, headers, *, patch=None):
    body = None if patch is None else json.dumps(patch, ensure_ascii=False, allow_nan=False).encode("utf-8")
    req = Request(url, data=body, headers=headers, method="GET" if body is None else "PATCH")
    with build_opener(ProxyHandler({}), NoRedirect()).open(req, timeout=90) as resp:
        return json.load(resp), resp.headers.get("ETag")


def fetch(key):
    flow, etag = request(BASE + "/api/v1/flows/" + DEV, {"x-api-key": key})
    if (flow.get("id"), flow.get("name"), flow.get("folder_id")) != (DEV, "翻译流程MVP-v2-dev", PROJECT):
        raise ValueError("Expected the dedicated 8789 development Flow.")
    schemas = {n["id"]: n["data"]["node"] for n in flow["data"]["nodes"]}
    if any(schemas[n]["template"]["service_url"]["value"].rstrip("/") != WORKER for n in NODES[1:]):
        raise ValueError("Development graph has an unexpected Worker binding.")
    return flow, etag or flow.get("version_token")


def sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def patched_graph(original):
    graph = copy.deepcopy(original)
    nodes = {n["id"]: n["data"]["node"] for n in graph["nodes"]}
    hashes = json.loads((FILES / "flow_base.json").read_text(encoding="utf-8"))
    library = (FILES / "materials.py").read_text(encoding="utf-8").rstrip()
    marker = "# ORDER_MATERIALS_LIBRARY"
    raw = {name + ".py": nodes[name]["template"]["code"]["value"] for name in NODES}
    if all(sha(raw[name].replace(library, marker)) == hashes[name]["template"] for name in raw):
        updated = {name: raw[name] for name in raw}
    else:
        if not all(sha(src) == hashes[name]["before"] for name, src in raw.items()):
            raise ValueError("A development node has drifted; review its diff before applying.")
        with TemporaryDirectory(prefix="lf-8789-") as tmp:
            path = Path(tmp)
            for name, code in raw.items():
                (path / name).write_text(code, encoding="utf-8", newline="\n")
            subprocess.run(
                ["git", "apply", "--no-index", "--whitespace=nowarn", str(FILES / "langflow.patch")],
                cwd=path,
                check=True,
            )
            updated = {}
            for name in raw:
                src = (path / name).read_text(encoding="utf-8")
                if sha(src) != hashes[name]["template"]:
                    raise ValueError("Patched node hash mismatch: " + name)
                updated[name] = src.replace(marker, library)
    for name in NODES:
        code = updated[name + ".py"]
        compile(code, name, "exec")
        schema = nodes[name]
        schema["template"]["code"]["value"] = code
        schema.setdefault("metadata", {})["code_hash"] = sha(code)[:12]
    inputs = nodes[NODES[0]]
    template = inputs["template"]
    field = copy.deepcopy(template["request_json"])
    field.update(
        name="materials_json",
        display_name="本单 Prompt 和参考材料 JSON",
        value="",
        required=False,
        info="新提交或换稿时传入序列化的 prompt_file 和 reference_files，其他操作不需要。",
    )
    template["materials_json"] = field
    old = ("prompt_file", "project_style", "general_style")
    for k in old:
        template.pop(k, None)
    if isinstance(inputs.get("field_order"), list):
        inputs["field_order"] = [k for k in inputs["field_order"] if k not in (*old, "materials_json")] + [
            "materials_json"
        ]
    inputs.update(
        display_name="01 翻译需求与随单资料",
        description="新单接收项目 JSON、Prompt 和可变数量的 Markdown。控制查询只需原任务参数。",
    )
    return graph


def check_worker(graph):
    schema = next(n for n in graph["nodes"] if n["id"] == NODES[1])["data"]["node"]["template"]
    token = schema["service_token"].get("value")
    if not token or schema["service_token"].get("load_from_db") or set(token) <= set("*•"):
        token = getpass("8789 Worker token: ")
    caps, _ = request(WORKER + "/v1/tasks/capabilities", {"Authorization": "Bearer " + token})
    if (
        caps.get("order_materials_contract") != CONTRACT
        or caps.get("same_name_parallel") is not False
        or not caps.get("project_thread_continuity")
        or not all(
            caps.get(k) is True
            for k in (
                "strict_project_order",
                "task_stop",
                "task_resume",
                "replace_in_place",
                "auto_resume_interrupted_predecessor",
            )
        )
    ):
        raise SystemExit("先升级 8789 Worker；本次未写入开发 Flow。")


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--apply", action="store_true")
    cli.add_argument(
        "--export-nodes",
        type=Path,
        help="Save verified development node code for offline tests (no graph configuration or tokens).",
    )
    args = cli.parse_args()
    key = os.environ.get("LANGFLOW_API_KEY") or getpass("Langflow API key: ")
    flow, etag = fetch(key)
    before = flow["data"]
    after = patched_graph(before)
    if args.export_nodes:
        args.export_nodes.mkdir(parents=True, exist_ok=False)
        for node in before["nodes"]:
            if node["id"] in NODES:
                (args.export_nodes / (node["id"] + ".py")).write_text(
                    node["data"]["node"]["template"]["code"]["value"], encoding="utf-8", newline="\n"
                )
    if args.apply:
        check_worker(before)
        if before != after:
            # Some Langflow releases return a version token, others only updated_at.
            latest, revision = fetch(key)
            if (
                latest["data"] != before
                or latest.get("updated_at") != flow.get("updated_at")
                or etag != revision
            ):
                raise SystemExit("开发 Flow 在预览后已更改。")
            headers = {"x-api-key": key, "Content-Type": "application/json"}
            if revision:
                headers["If-Match"] = revision
            request(BASE + "/api/v1/flows/" + DEV, headers, patch={"data": after})
        if fetch(key)[0]["data"] != after:
            raise RuntimeError("Development Flow readback mismatch.")
    print(
        json.dumps(
            {
                "flow_id": DEV,
                "worker": WORKER,
                "applied": args.apply,
                "changes": before != after,
                "contract": CONTRACT,
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
