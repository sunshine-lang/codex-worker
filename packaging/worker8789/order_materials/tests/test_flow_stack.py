"""Optional integration using a read-only export of the exact dev node code; no live model."""

import asyncio
import json
import os
from pathlib import Path
from types import ModuleType
import sys
import uuid

import httpx
import pytest

from codex_remote_worker.app import create_app
from codex_remote_worker.app_server_text import TextAppServerRunner
from codex_remote_worker import app_server_text
from codex_remote_worker.task_runtime import layout
from codex_remote_worker.task_tools import TaskTools
from materials import materials_digest, materials_index
from test_orders import MaterialRpc, materials
from test_translation_tasks import wait_for_task


def components(monkeypatch):
    src = os.environ.get("LF8789_NODE_DIR")
    if not src:
        pytest.skip("Set LF8789_NODE_DIR to the code exported by langflow_dev.py --export-nodes.")
    package = Path(__file__).resolve().parents[2]
    monkeypatch.syspath_prepend(str(package))
    from langflow_dev import NODES, patched_graph

    class Field:
        def __init__(self, **kw):
            self.__dict__.update(kw)

    for name, attrs in {
        "lfx.custom": {"Component": type("Component", (), {})},
        "lfx.io": {
            x: Field
            for x in "BoolInput DataInput FileInput HandleInput IntInput MessageTextInput SecretStrInput StrInput Output".split()
        },
        "lfx.schema": {"Data": Field, "Message": Field},
        "lfx.services.deps": {"get_storage_service": None},
        "lfx.utils.file_path_security": {
            "component_file_access_scopes": None,
            "enforce_local_file_access": None,
        },
    }.items():
        mod = ModuleType(name)
        mod.__dict__.update(attrs)
        monkeypatch.setitem(sys.modules, name, mod)
    graph = {
        "nodes": [
            {
                "id": n,
                "data": {
                    "node": {
                        "template": {
                            "code": {"value": (Path(src) / (n + ".py")).read_text(encoding="utf-8")},
                            "request_json": {"name": "request_json"},
                            "prompt_file": {},
                            "project_style": {},
                            "general_style": {},
                        }
                    }
                },
            }
            for n in NODES
        ]
    }
    g = patched_graph(graph)
    assert patched_graph(g) == g
    ret = []
    for v in g["nodes"]:
        ns = {}
        exec(compile(v["data"]["node"]["template"]["code"]["value"], v["id"], "exec"), ns)
        ret.append(ns)
    return ret, Field


def test_langflow_to_8789_materials_and_result(settings, monkeypatch, tmp_path):
    (entry, submit, query), data = components(monkeypatch)
    settings = settings.model_copy(
        update={
            "port": 8789,
            "continuous_project_session": True,
            "project_context_enabled": True,
            "max_concurrent_jobs": 5,
        }
    )
    settings.doc_converter.write_bytes(b"fixture")
    monkeypatch.setattr(
        TextAppServerRunner,
        "baseline_ready",
        lambda _: {"ready": True, "service_version": "fixture", "codex_version": "fixture", "tools": {}},
    )

    async def prepare(settings, job, root, touch, stopped, redactor, *, host_prepared=False):
        assert host_prepared
        layout(settings, job, root)
        return TaskTools(root / "work/.worker/manifest.json").prepare()

    monkeypatch.setattr(app_server_text, "prepare_task_runtime", prepare)
    runner = TextAppServerRunner(settings)
    rpc = MaterialRpc(runner)
    runner.rpc = rpc
    runner.started = True
    runner.generation = 1
    app = create_app(settings, runner=runner)
    transport = httpx.ASGITransport(app=app)
    store = tmp_path / "lf"
    store.mkdir()
    for module in (submit, query):
        impl = module["TaskClient"]
        module["TaskClient"] = lambda base, token, *, allow_insecure_http, _impl=impl: _impl(
            base, token, allow_insecure_http=allow_insecure_http, transport=transport
        )
        module["component_review_root"] = lambda comp, base: store

    def route(business, mat=None, key=""):
        inp = entry["MVPInputs"]()
        inp.request_json = json.dumps(business, ensure_ascii=False)
        inp.materials_json = json.dumps(mat, ensure_ascii=False) if mat is not None else ""
        inp.submission_key = key
        return inp.prepare().data

    async def call(ns, klass, state):
        inst = ns[klass]()
        inst.state = data(data=state)
        inst.service_url = "http://52.62.69.192:8789"
        inst.service_token = settings.service_token
        inst.allow_insecure_http = True
        inst.timeout_seconds = 600
        return (await inst.run()).data

    # Default fixture uses a small fourth guide. The larger private input is opt-in.
    project = "Offline named order"
    example = os.environ.get("LF8789_REAL_CASE")
    if example:
        snap = Path(example)
        ma = json.loads((snap / "A/materials.json").read_bytes())
        mb = json.loads((snap / "B/materials.json").read_bytes())
        order = json.loads((snap / "A/request.json").read_bytes())
        project = order["project_name"]
    else:
        ma, mb = materials(), materials("New Heading")
        order = {
            "project_name": project,
            "segments": [
                {
                    "segment_id": "1",
                    "source_language": "zh-CN",
                    "target_language": "en-US",
                    "source_text": "具体实施方式",
                    "target_text": "",
                    "context": "Title of the embodiments section.",
                    "tb_list": [],
                    "tm_list": [
                        {
                            "source": "具体实施方式",
                            "target": "Old Heading",
                            "match_rate": 100,
                            "enabled": True,
                            "context": "Title of the embodiments section.",
                        }
                    ],
                }
            ],
        }

    async def exercise():
        async with app.router.lifespan_context(app):
            assert route(order)["error"]["code"] == "INVALID_INPUT"
            receipts = []
            for m in (ma, mb):
                k = str(uuid.uuid4())
                state = route(order, m, k)
                assert "error" not in state
                out = await call(submit, "MVPSubmit", state)
                ack = out.get("public_snapshot", {})
                assert ack.get("materials_sha256") == materials_digest(materials_index(m)), out.get(
                    "submission_error"
                )
                receipts.append((k, ack))
            for (_, ack), m in zip(receipts, (ma, mb)):
                tid = ack["task_id"]
                async with httpx.AsyncClient(
                    transport=transport,
                    base_url="http://worker",
                    headers={"Authorization": "Bearer " + settings.service_token.get_secret_value()},
                ) as cli:
                    v = await wait_for_task(cli, tid)
                    assert v["status"] == "completed", (
                        v.get("error"),
                        app.state.store.get(app.state.translation_tasks.store.get(tid)["session_job_id"]).get(
                            "host_error"
                        ),
                    )
                final = (await call(query, "MVPQuery", route({"operation": "result", "task_id": tid})))[
                    "public_snapshot"
                ]
                assert final.get("business_result_restored") and final.get("worker_result_hash_verified"), (
                    final.get("error")
                )
                assert final["result"]["segments"][0]["target_text"] == rpc.heading_by_task[tid]
                assert final["materials_sha256"] == materials_digest(materials_index(m))
            assert (
                receipts[1][1]["previous_project_task_id"] == receipts[0][1]["task_id"] and rpc.threads == 1
            )
            bad = await call(submit, "MVPSubmit", route(order, mb, receipts[0][0]))
            assert "SUBMISSION_INPUT_CHANGED" in bad["submission_error"]["message"]

    asyncio.run(exercise())
