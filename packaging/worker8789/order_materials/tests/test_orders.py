import asyncio
import copy
import json
import re
import uuid
from pathlib import Path

import httpx
import pytest

from codex_remote_worker.app import create_app
from codex_remote_worker.app_server_text import TextAppServerRunner
from codex_remote_worker.contextual_tm import tm_candidate_id
from codex_remote_worker.task_runtime import layout
from codex_remote_worker.task_tools import TaskTools
from codex_remote_worker import app_server_text
from materials import materials_digest, materials_index, parse_materials_json
from test_project_session import ProjectRpc, read_args
from test_translation_tasks import wait_for_task


def materials(word="Old Heading", count=4):
    return {
        "prompt_file": {
            "filename": "翻译要求.md",
            "content": "Use the approved headings in the current materials.",
        },
        "reference_files": [
            {
                "filename": f"{i + 1:02d}-术语.md",
                "content": (f"Approved heading: {word}" if i == 3 else "Supporting instructions."),
            }
            for i in range(count)
        ],
    }


def packet():
    source = "具体实施方式"
    ctx = "Title of the embodiments section."
    return {
        "project_name": "P",
        "segments": [
            {
                "segment_id": "136",
                "source_language": "zh-CN",
                "target_language": "en-US",
                "source_text": source,
                "target_text": "",
                "context": ctx,
                "tb_list": [],
                "tm_list": [
                    {"source_text": source, "target_text": "Old Heading", "context": ctx, "match_rate": 100}
                ],
            }
        ],
    }


def parts(order, mat):
    return [
        ("source_file", ("order.json", json.dumps(order, ensure_ascii=False).encode(), "application/json")),
        (
            "prompt_file",
            (mat["prompt_file"]["filename"], mat["prompt_file"]["content"].encode(), "text/markdown"),
        ),
        *(
            ("reference_files", (r["filename"], r["content"].encode(), "text/markdown"))
            for r in mat["reference_files"]
        ),
    ]


class MaterialRpc(ProjectRpc):
    """Scripted model. The translation is derived from the actual tool response in this turn."""

    def __init__(self, runner):
        super().__init__(runner)
        self.turn_target = None
        self.last_completion = None
        self.active_parent = None
        self.dispatched = []
        self.heading_by_task = {}

    async def request(self, method, params, **kw):
        if method == "thread/read":
            self.calls.append((method, params))
            assert params["threadId"] == self.thread_id
            return {"thread": {"id": self.thread_id, "cwd": str(self.cwd)}}
        if method == "thread/resume":
            self.calls.append((method, params))
            assert params["threadId"] == self.thread_id and Path(params["cwd"]) == self.cwd
            assert not params.get("runtimeWorkspaceRoots") and "dynamicTools" not in params
            return {
                "thread": {"id": self.thread_id},
                "activePermissionProfile": {"id": "translation"},
                "approvalPolicy": "never",
                "model": "server/default-model",
                "modelProvider": "fixture",
                "reasoningEffort": "high",
                "cwd": str(self.cwd),
                "runtimeWorkspaceRoots": [],
            }
        if method != "turn/start":
            return await super().request(method, params, **kw)
        self.calls.append((method, params))
        self.turns += 1
        self.turn_id = "turn-" + str(self.turns)
        s = self.runner.project_session
        task = s.task["task_id"]
        if task not in self.dispatched:
            self.dispatched.append(task)
        self.actions = []
        if s.phase == "translating":
            payload, _ = json.JSONDecoder().raw_decode(params["input"][0]["text"])
            assert "current_order_materials" in payload and payload["guides"] == {}
            self.turn_target = None
            self.actions.extend(
                ("project_read", read_args("guides", query=n)) for n in s.overview()["guide_queries"]
            )
            self.actions.append(("project_read", read_args("source")))
        else:
            self.actions = [
                ("project_read", read_args("source")),
                ("project_review", {"row_ids": list(s.pending), "issues": [], "note": "checked"}),
                ("project_read", read_args("checks")),
            ]
        self._emit()
        return {"turn": {"id": self.turn_id}}

    async def _write(self, value):
        assert value["result"]["success"], value
        self.messages.append(value)
        tool, args = self.current
        s = self.runner.project_session
        if tool == "project_read" and args["operation"] == "guides" and args["query"].startswith("04-"):
            body = json.loads(value["result"]["contentItems"][0]["text"])
            if body["content"].startswith("Approved heading: "):
                self.turn_target = body["content"].split("Approved heading: ", 1)[1]
            else:
                self.turn_target = re.search("须原样译为“([^”]+)”", body["content"]).group(1)
            self.heading_by_task[s.task["task_id"]] = self.turn_target
        if tool == "project_read" and args["operation"] == "source" and s.phase == "translating":
            assert self.turn_target is not None
            ref = s.records[0]["tm_list"][0]
            action = "reuse" if self.turn_target == ref["target_text"] else "adapt"
            rows = [
                {
                    "row_id": "0",
                    "target_text": self.turn_target,
                    "term_decisions": [],
                    "tm_decision": {"action": action, "candidate_id": tm_candidate_id(ref)},
                }
            ]
            rows.extend(
                {
                    "row_id": key,
                    "target_text": "Brief Description of the Drawings",
                    "term_decisions": [],
                    "tm_decision": {"action": "retranslate", "candidate_id": ""},
                }
                for key in s.pending
                if key != "0"
            )
            self.actions.append(("project_write", {"rows": rows, "notes": "Checked current glossary."}))
        self._emit()


def test_8789_fifo_resume_and_actual_fourth_file(settings, monkeypatch):
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

    async def host_prepare(settings, job, root, touch, stopped, redactor, *, host_prepared=False):
        assert host_prepared
        layout(settings, job, root)
        return TaskTools(root / "work/.worker/manifest.json").prepare()

    monkeypatch.setattr(app_server_text, "prepare_task_runtime", host_prepare)
    runner = TextAppServerRunner(settings)
    rpc = MaterialRpc(runner)
    runner.rpc = rpc
    runner.started = True
    runner.generation = 1

    async def exercise():
        app = create_app(settings, runner=runner)
        async with app.router.lifespan_context(app):
            auth = {"Authorization": "Bearer " + settings.service_token.get_secret_value()}
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test", headers=auth
            ) as client:
                caps = (await client.get("/v1/tasks/capabilities")).json()
                assert (
                    caps["order_materials_contract"] == "order-materials/1"
                    and caps["same_name_parallel"] is False
                )
                assert caps["project_thread_continuity"] == "source_file.project_name"
                assert all(
                    caps[k]
                    for k in (
                        "strict_project_order",
                        "task_stop",
                        "task_resume",
                        "replace_in_place",
                        "auto_resume_interrupted_predecessor",
                    )
                )
                order = packet()
                ma = materials()
                mb = materials("New Heading")

                async def send(m, key):
                    return await client.post(
                        "/v1/tasks",
                        files=parts(order, m),
                        data={"options": "{}"},
                        headers={"Idempotency-Key": key},
                    )

                ka, kb = str(uuid.uuid4()), str(uuid.uuid4())
                a = await send(ma, ka)
                assert a.status_code == 202, a.text
                b = await send(mb, kb)
                assert b.status_code == 202, b.text
                aa, bb = a.json(), b.json()
                assert (
                    aa["project_id"] == bb["project_id"] and bb["previous_project_task_id"] == aa["task_id"]
                )
                assert (await send(ma, ka)).json()["task_id"] == aa["task_id"]
                assert (await send(mb, ka)).status_code == 409
                done = []
                for receipt in (aa, bb):
                    v = await wait_for_task(client, receipt["task_id"])
                    if v["status"] != "completed":
                        stored = app.state.translation_tasks.store.get(v["task_id"])
                        job = (
                            app.state.store.get(stored["session_job_id"])
                            if stored.get("session_job_id")
                            else {}
                        )
                        pytest.fail(str((v.get("error"), stored.get("host_error"), job.get("host_error"))))
                    done.append(v)
                assert [v["status"] for v in done] == ["completed", "completed"], [
                    (v["error"], app.state.translation_tasks.store.get(v["task_id"]).get("host_error"))
                    for v in done
                ]
                assert rpc.dispatched == [aa["task_id"], bb["task_id"]]
                assert done[1]["changed_reference_files"] == ["04-术语.md"]
                assert not done[1]["removed_reference_files"]
                from datetime import datetime

                assert datetime.fromisoformat(done[0]["order_completed_at"]) <= datetime.fromisoformat(
                    done[1]["execution_window"]["started_at"]
                )

                assert (
                    rpc.threads == 1
                    and sum(m == "thread/resume" for m, _ in rpc.calls) == 1
                    and sum(m == "thread/read" for m, _ in rpc.calls) == 1
                )
                assert done[0]["codex_thread_id"] == done[1]["codex_thread_id"] == rpc.thread_id
                for v, mat, expected in zip(done, (ma, mb), ("Old Heading", "New Heading")):
                    assert v["materials_sha256"] == materials_digest(materials_index(mat))
                    assert "04-术语.md" in v["session_progress"]["read_reference_files"]
                    assert v["self_review_status"] == "completed"
                    assert v["progress"]["tm_reused_segments"] == 0
                    assert (await client.get(v["result_path"])).json()["segments"][0][
                        "target_text"
                    ] == expected
                    assert (await client.get(v["status_path"] + "/batches")).json()["tm_decisions"]["0"][
                        "actor"
                    ] == "agent"
                # Historical delivery remains retrievable after the successor has finished.
                assert (await client.get(aa["status_path"] + "/result")).json()["segments"][0][
                    "target_text"
                ] == "Old Heading"
                assert (await client.post("/v1/tasks/" + bb["task_id"] + "/stop")).json()[
                    "status"
                ] == "completed"

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "bad",
    [
        {},
        {"prompt_file": {"filename": "p.md", "content": " "}, "reference_files": []},
        {
            "prompt_file": {"filename": "p.md", "content": "p"},
            "reference_files": [{"filename": "P.md", "content": "x"}],
        },
        {"prompt_file": {"filename": "../p.md", "content": "p"}, "reference_files": []},
        {
            "prompt_file": {"filename": "p.md", "content": "p"},
            "reference_files": [{"filename": "B.md", "content": ""}],
        },
    ],
)
def test_invalid_materials(bad):
    with pytest.raises(ValueError):
        parse_materials_json(json.dumps(bad))


def test_zero_references_and_fingerprint():
    value = materials(count=0)
    assert parse_materials_json(json.dumps(value)) == value
    a = materials()
    b = copy.deepcopy(a)
    b["reference_files"].reverse()
    assert materials_digest(materials_index(a)) != materials_digest(materials_index(b))


def test_8789_recovery_replace_and_invalid_files(settings, monkeypatch):
    from codex_remote_worker.translation_tasks import TaskManager
    from test_translation_tasks import JsonRunner

    settings = settings.model_copy(
        update={
            "port": 8789,
            "project_context_enabled": True,
            "continuous_project_session": True,
            "max_concurrent_jobs": 5,
        }
    )

    async def no_poll(self):
        pass

    monkeypatch.setattr(TaskManager, "start", no_poll)

    async def exercise():
        app = create_app(settings, runner=JsonRunner())
        async with app.router.lifespan_context(app):
            auth = {"Authorization": "Bearer " + settings.service_token.get_secret_value()}
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test", headers=auth
            ) as client:
                order = packet()
                m = materials(count=0)

                def kid():
                    return {"Idempotency-Key": str(uuid.uuid4())}

                async def send(files, options=None, key=None):
                    return await client.post(
                        "/v1/tasks",
                        files=files,
                        data={"options": json.dumps(options or {})},
                        headers=key or kid(),
                    )

                assert (await send(parts(order, m)[:1])).status_code == 422
                assert (await send(parts(order, m) + parts(order, m)[1:])).status_code == 422
                bad = copy.deepcopy(m)
                bad["reference_files"] = [{"filename": "翻译要求.md", "content": "different"}]
                assert (await send(parts(order, bad))).status_code == 422
                first = (await send(parts(order, m))).json()
                a = first["task_id"]
                assert [f["role"] for f in first["material_files"]] == ["prompt_file"]
                second = (await send(parts(order, materials()))).json()
                b = second["task_id"]
                mgr = app.state.translation_tasks

                async def waiter():
                    await mgr.advance(mgr.store.get(b))
                    return (await client.get("/v1/tasks/" + b)).json()

                assert (await waiter())["waiting_on_task_id"] == a
                assert (await client.post("/v1/tasks/" + a + "/stop")).json()["status"] == "stopped"
                assert (await waiter())["stage"] == "waiting_for_project_recovery"
                recovery = kid()
                for _ in range(2):
                    recovered = await client.post("/v1/tasks/" + a + "/resume", headers=recovery)
                    assert recovered.status_code == 200 and recovered.json()["task_id"] == a
                assert mgr.store.get(a)["resume_keys"].count(recovery["Idempotency-Key"]) == 1
                await client.post("/v1/tasks/" + a + "/stop")
                k = kid()
                rev = (await send(parts(order, materials("revised")), {"replaces_task_id": a}, k)).json()
                assert rev["task_id"] != a and rev["project_id"] == first["project_id"]
                assert (await send(parts(order, materials("revised")), {"replaces_task_id": a}, k)).json()[
                    "task_id"
                ] == rev["task_id"]
                assert (await waiter())["waiting_on_task_id"] == rev["task_id"]
                assert (await client.get("/v1/tasks/" + a)).json()["replaced_by_task_id"] == rev["task_id"]

    asyncio.run(exercise())


@pytest.mark.parametrize("manual", [False, True])
def test_auto_resume_interrupted_predecessor_uses_development_scope(settings, monkeypatch, manual):
    from unittest.mock import Mock
    from codex_remote_worker.project_recovery import interruption_key, reconciled_after_restart
    from codex_remote_worker.translation_tasks import TaskManager
    from test_interrupted_predecessor import incident

    settings = settings.model_copy(update={"port": 8789, "continuous_project_session": True})
    job = incident()
    process = {"pid": 123, "created_at": 123, "job_object": "Local\\CodexWorkerAppServer8789-" + "a" * 32}
    job.update(
        process=process,
        restart_cleanup={"verified_at": "verified", "started_at": job["started_at"], "process": process},
    )
    assert reconciled_after_restart(job)
    wrong = copy.deepcopy(job)
    wrong["process"]["job_object"] = wrong["process"]["job_object"].replace("8789", "8788")
    assert not reconciled_after_restart(wrong)
    jobs = Mock()
    jobs.by_key.return_value = job
    mgr = TaskManager(settings, jobs, Mock())
    calls = []

    def accept_fixture(key):
        tid, stage = mgr.new_stage()
        record = dict(
            task_id=tid,
            request_key=key,
            fingerprint=key,
            project_name="p",
            created_at=key,
            status="queued",
            stage="queued",
            continuous_project_session=True,
            batches=[{}],
        )
        return mgr.store.accept(record, stage, mgr.root / tid, 10, project_name_key="p")[0]

    prior = accept_fixture("A")
    waiter = accept_fixture("B")
    prior.update(status="failed", stage="failed", session_job_id=job["job_id"], error=job["error"])
    mgr.store.save(prior)

    def resume(_settings, task_id, *, recovery_key, waiting_task_id):
        calls.append((task_id, recovery_key, waiting_task_id))
        prior.update(
            status="queued", stage="waiting_for_worker", error=None, auto_recovery={"state": "scheduled"}
        )
        mgr.store.save(prior)

    monkeypatch.setattr("codex_remote_worker.translation_tasks.queue_resume", resume)

    async def exercise():
        if manual:
            await mgr.request_stop(prior["task_id"])
        for _ in range(2):
            await mgr.advance(mgr.store.get(waiter["task_id"]))
        v = mgr.store.get(waiter["task_id"])
        assert v["waiting_on_task_id"] == prior["task_id"] and not v.get("session_job_id")
        assert calls == ([] if manual else [(prior["task_id"], interruption_key(job), waiter["task_id"])])

    try:
        asyncio.run(exercise())
    finally:
        mgr.store.close()
