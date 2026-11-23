import asyncio
import base64
import io
import json
import threading
import time
import uuid
import zipfile
from types import SimpleNamespace

import pytest
from docx import Document
from lxml import etree
from PIL import Image
from fastapi.testclient import TestClient
from pydantic import SecretStr

from codex_remote_worker.common import JobError, write_json
from codex_remote_worker.material_agent import conversation
from codex_remote_worker.material_documents import page_preview, standalone_markdown, transcribe
from codex_remote_worker.material_service import accept, create_material_app
from codex_remote_worker.store import Store

AUTH = {"Authorization": "Bearer " + "test-only-" * 5}
ROUTE = "/v1/material-tasks"


def settings(root, slots=2):
    return SimpleNamespace(data_root=root, max_concurrent_jobs=slots, queue_capacity=10,
                           service_token=SecretStr(AUTH["Authorization"][7:]))


def request(documents=()):
    return {"project_name": "同名项目", "business_tag": "IM", "source_language": "en",
            "target_language": "zh-CN", "translation_requirements": "设备手册",
            "submission_key": str(uuid.uuid4()), "documents": list(documents)}


def word(text, name="材料.docx"):
    doc = Document()
    doc.add_heading("说明", 1)
    doc.add_paragraph(text)
    table = doc.add_table(rows=2, cols=2)
    for row, pair in zip(table.rows, [("English", "中文"), ("Device", "设备")]):
        for cell, value in zip(row.cells, pair):
            cell.text = value
    buf = io.BytesIO()
    doc.save(buf)
    return {"source_filename": name, "content_base64": base64.b64encode(buf.getvalue()).decode()}


def status(client, task):
    return client.get(ROUTE + "/" + task, headers=AUTH).json()


def wait(client, task, target):
    for _ in range(500):
        value = status(client, task)
        if value["status"] == target:
            return value
        if value["status"] in ("failed", "interrupted", "completed"):
            pytest.fail(str(value))
        time.sleep(.02)
    pytest.fail("Timed out waiting for " + target)


class FixtureRpc:
    """First answer too early, then walk both actual files in a single thread."""
    def __init__(self, queue, count):
        self.queue, self.count = queue, count
        self.turns, self.replies = [], []

    async def request(self, method, data):
        assert method == "turn/start"
        self.turns.append(data["threadId"])
        turn = "turn-" + str(len(self.turns))
        if len(self.turns) == 2:
            for i in range(self.count):
                await self.queue.put({"id": i, "method": "item/tool/call",
                    "params": {"threadId": data["threadId"], "turnId": turn, "tool": "material_read",
                               "arguments": {"document": i, "offset": 0}}})
        await self.queue.put({"method": "turn/completed", "params": {"threadId": data["threadId"],
            "turn": {"id": turn, "status": "completed", "items": [{"type": "agentMessage", "phase": "final_answer",
                "text": json.dumps({"translation_prompt": "fixture prompt"})}]}}})
        return {"turn": {"id": turn}}

    async def _write(self, reply):
        assert reply["result"]["success"]
        self.replies.append(json.loads(reply["result"]["contentItems"][0]["text"])["markdown"])


def test_real_word_conversion_http_and_single_thread(tmp_path):
    replies = []
    async def agent(_settings, _root, session):
        queue = asyncio.Queue()
        rpc = FixtureRpc(queue, len(session.docs))
        prompt = await conversation(rpc, "same-thread", queue, session)
        assert rpc.turns == ["same-thread", "same-thread"]
        replies.extend(rpc.replies)
        return prompt

    data = request([word("Alpha original"), word("Beta original")])
    with TestClient(create_material_app(settings(tmp_path), agent)) as client:
        response = client.post(ROUTE, headers=AUTH, json=data)
        assert response.status_code == 202
        task = response.json()["task_id"]
        final = wait(client, task, "completed")
        assert final["documents_completed"] == final["documents_total"] == 2
        result = client.get(f"{ROUTE}/{task}/result", headers=AUTH).json()
        assert set(result) == {"translation_prompt", "documents_markdown"}
        assert result["translation_prompt"] == "fixture prompt"
        assert [d["source_filename"] for d in result["documents_markdown"]] == ["材料.docx"] * 2
        for i, text in enumerate(("Alpha original", "Beta original")):
            assert text in replies[i] and text in result["documents_markdown"][i]["markdown"]
            assert "| Device" in result["documents_markdown"][i]["markdown"]
        replay = client.post(ROUTE, headers=AUTH, json=data)
        assert replay.status_code == 200 and replay.json()["task_id"] == task
        data["source_language"] = "de"
        assert client.post(ROUTE, headers=AUTH, json=data).status_code == 409


def test_slots_zero_files_and_validation(tmp_path):
    release = threading.Event()
    async def agent(_settings, _root, session):
        await asyncio.to_thread(release.wait, 5)
        return session.prompt('{"translation_prompt":"fixture industry prompt"}')

    with TestClient(create_material_app(settings(tmp_path), agent)) as client:
        assert client.get("/v1/ready").status_code == 401
        tasks = [client.post(ROUTE, headers=AUTH, json=request()).json()["task_id"] for _ in range(3)]
        try:
            first, second = [wait(client, t, "running") for t in tasks[:2]]
            assert {first["execution_slot"], second["execution_slot"]} == {1, 2}
            assert status(client, tasks[2])["status"] == "queued"
            assert client.get(f"{ROUTE}/{tasks[0]}/result", headers=AUTH).status_code == 409
        finally:
            release.set()
        for t in tasks:
            wait(client, t, "completed")
            assert client.get(f"{ROUTE}/{t}/result", headers=AUTH).json()["documents_markdown"] == []
        for d in ({"source_filename": "../a.docx", "content_base64": "Zg=="},
                  {"source_filename": "a.doc", "content_base64": "broken!"},
                  {"source_filename": "a.exe", "content_base64": "Zg=="}):
            assert client.post(ROUTE, headers=AUTH, json=request([d])).status_code == 422


def test_corrupt_file_is_a_failed_task(tmp_path):
    async def agent(_settings, _root, session):
        await asyncio.to_thread(session.call, "material_read", {"document": 0, "offset": 0})
        raise AssertionError("corrupt document unexpectedly parsed")
    with TestClient(create_material_app(settings(tmp_path), agent)) as client:
        data = request([{"source_filename": "broken.docx", "content_base64": "eHg="}])
        task = client.post(ROUTE, headers=AUTH, json=data).json()["task_id"]
        failed = wait(client, task, "failed")
        assert failed["current_file"] == "broken.docx" and failed["error"]["code"] == "INVALID_DOCUMENT"
        assert client.get(f"{ROUTE}/{task}/result", headers=AUTH).status_code == 409


def test_restart_recovers_published_and_marks_unfinished(tmp_path):
    (tmp_path / "staging").mkdir()
    (tmp_path / "tasks").mkdir()
    store = Store(tmp_path / "materials.sqlite")
    tasks = []
    for _ in range(2):
        job, _ = accept(store, tmp_path, request(), 10)
        store.claim(max_active=2)
        tasks.append(job["job_id"])
    store.close()
    write_json(tmp_path / "tasks" / tasks[0] / "result.json", {"translation_prompt": "saved", "documents_markdown": []})
    with TestClient(create_material_app(settings(tmp_path))) as client:
        assert status(client, tasks[0])["status"] == "completed"
        assert status(client, tasks[1])["status"] == "interrupted"


def test_email_link_with_embedded_fallback_keeps_original_picture(tmp_path):
    picture = io.BytesIO()
    Image.new("RGB", (8, 8), "blue").save(picture, format="PNG")
    picture.seek(0)
    doc = Document()
    doc.add_paragraph("Picture and caption")
    doc.add_picture(picture)
    memory = io.BytesIO()
    doc.save(memory)
    source = tmp_path / "customer.docx"
    ns = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    with zipfile.ZipFile(memory) as original, zipfile.ZipFile(source, "w") as edited:
        for filename in original.namelist():
            raw = original.read(filename)
            if filename == "word/document.xml":
                xml = etree.fromstring(raw)
                blip = xml.xpath('//*[local-name()="blip"]')[0]
                blip.set("{" + ns + "}link", "rIdEmail")
                raw = etree.tostring(xml)
            if filename == "word/_rels/document.xml.rels":
                xml = etree.fromstring(raw)
                etree.SubElement(xml, xml[0].tag, Id="rIdEmail", Target="cid:fixture",
                                 TargetMode="External", Type=ns + "/image")
                raw = etree.tostring(xml)
            edited.writestr(filename, raw)
    before = source.read_bytes()
    target = tmp_path / "converted"
    transcribe(source, target)
    assert source.read_bytes() == before
    assert "data:image/png;base64," in standalone_markdown(target)
    assert "assets/media" not in standalone_markdown(target)


def test_old_word_and_original_page(tmp_path):
    from codex_remote_worker.material_documents import office_convert, office_executable
    try:
        office_executable()
    except JobError:
        pytest.skip("LibreOffice is required for the legacy Word case")
    sample = tmp_path / "source.docx"
    sample.write_bytes(base64.b64decode(word("Legacy source line")["content_base64"]))
    source = office_convert(sample, tmp_path / "legacy", "doc")
    out = tmp_path / "prepared"
    assert "Legacy source line" in transcribe(source, out).read_text(encoding="utf-8")
    image, pages = page_preview(source, out, 1)
    assert pages >= 1 and image.startswith("data:image/jpeg;base64,")
