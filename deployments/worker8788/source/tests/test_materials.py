import asyncio
import base64
import io
import json
import shutil
import threading
import time
import uuid
import zipfile
from types import SimpleNamespace

import pytest
from docx import Document
from lxml import etree
from fastapi.testclient import TestClient
from pydantic import SecretStr

from codex_remote_worker.common import JobError, write_json
from codex_remote_worker.material_agent import MaterialSession, conversation
from codex_remote_worker.material_documents import page_preview, standalone_markdown, transcribe
from codex_remote_worker.material_service import accept, create_material_app, serve_with_desktop_stop
from codex_remote_worker.store import Store

AUTH = {"Authorization": "Bearer " + "test-only-" * 5}
ROUTE = "/v1/material-tasks"


def test_desktop_stop_uses_only_the_current_launch_request(tmp_path):
    stale = tmp_path / "serve.stop.previous"
    current = tmp_path / "serve.stop.current"
    stale.touch()
    server = SimpleNamespace(should_exit=False)

    async def serve():
        await asyncio.sleep(0.45)
        assert not server.should_exit
        current.touch()
        while not server.should_exit:
            await asyncio.sleep(0.02)

    server.serve = serve
    asyncio.run(asyncio.wait_for(serve_with_desktop_stop(server, current), timeout=4))
    assert stale.exists() and not current.exists()


def example_prompt(guide_text):
    return f"""# 角色设定
你是设备面板英译简中的译员。

# 任务目标
将操作说明译为简体中文。

# 外部指南文档挂载与优先级说明（KNOWLEDGE BASE GUIDELINES）
{guide_text}

# 翻译核心准则（CORE TRANSLATION RULES）
结合同一标签出现的场景判断译法。即使 TM 匹配率为 100%，也要核对上下文和客户修订。

# 格式与避坑强化提醒（FORMATTING DOs & DON'Ts）
保留量值、单位以及表格中各列的对应关系。

# 标签保留与交付格式 (TAGS & DELIVERABLES)
保留占位符，按本次任务约定交付译文。"""


def require_pandoc():
    if not shutil.which("pandoc"):
        pytest.importorskip("pypandoc", reason="Install the materials extra for Word conversion tests")


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
    """Exercise read completion and a formatting correction on the original thread."""
    def __init__(self, queue, count):
        self.queue, self.count = queue, count
        self.turns, self.replies, self.messages = [], [], []

    async def request(self, method, data):
        assert method == "turn/start"
        self.turns.append(data["threadId"])
        self.messages.append(data["input"][0]["text"])
        turn = "turn-" + str(len(self.turns))
        if len(self.turns) == 2:
            for i in range(self.count):
                await self.queue.put({"id": i, "method": "item/tool/call",
                    "params": {"threadId": data["threadId"], "turnId": turn, "tool": "material_read",
                               "arguments": {"document": i, "offset": 0}}})
        refs = [doc["reference"] for doc in json.loads(self.messages[0])["documents"]]
        prompt = "【角色设定】自由格式正文" if len(self.turns) == 2 else example_prompt("按本单资料执行。\n" + "\n".join(refs))
        await self.queue.put({"method": "turn/completed", "params": {"threadId": data["threadId"],
            "turn": {"id": turn, "status": "completed", "items": [{"type": "agentMessage", "phase": "final_answer",
                "text": json.dumps({"translation_prompt": prompt})}]}}})
        return {"turn": {"id": turn}}

    async def _write(self, reply):
        assert reply["result"]["success"]
        self.replies.append(json.loads(reply["result"]["contentItems"][0]["text"])["markdown"])


def test_real_word_conversion_http_and_single_thread(tmp_path):
    require_pandoc()
    replies = []
    async def agent(_settings, _root, session):
        assert session.prepared_count == 2
        assert sorted(p.name for p in (_root / "markdown").iterdir()) == ["01-材料.md", "02-材料.md"]
        queue = asyncio.Queue()
        rpc = FixtureRpc(queue, len(session.docs))
        prompt = await conversation(rpc, "same-thread", queue, session)
        assert rpc.turns == ["same-thread"] * 3
        assert "仍有文档尚未读完" in rpc.messages[1]
        assert "Markdown 一级标题" in rpc.messages[2]
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
        for name in ("01-材料.md", "02-材料.md"):
            assert "[`" + name + "`]" in result["translation_prompt"]
        assert [d["markdown_filename"] for d in result["documents_markdown"]] == ["01-材料.md", "02-材料.md"]
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
    prompt = example_prompt("本次无外部附件，按业务标签、语向和翻译需求执行。")
    async def agent(_settings, _root, session):
        session.update("generating_prompt", "正在生成项目提示词")
        await asyncio.to_thread(release.wait, 15)
        return session.prompt(json.dumps({"translation_prompt": prompt}))

    with TestClient(create_material_app(settings(tmp_path), agent)) as client:
        assert client.get("/v1/ready").status_code == 401
        assert client.get("/v1/ready", headers=AUTH).json()["max_attachment_bytes_per_task"] == 256 * 1024**2
        route = "/v1/ready?include_channels=true"
        assert client.get(route).status_code == 401
        assert "channels" not in client.get("/v1/ready", headers=AUTH).json()
        idle = [{"execution_slot": slot, "status": "idle"} for slot in (1, 2)]
        assert client.get(route, headers=AUTH).json()["channels"] == idle
        tasks = [client.post(ROUTE, headers=AUTH, json=request()).json()["task_id"]]
        try:
            first = wait(client, tasks[0], "running")
            channels = client.get(route, headers=AUTH).json()["channels"]
            occupied = first["execution_slot"] - 1
            assert channels[occupied]["task_id"] == tasks[0]
            assert channels[1 - occupied] == idle[1 - occupied]
            tasks.extend(client.post(ROUTE, headers=AUTH, json=request()).json()["task_id"] for _ in range(2))
            first, second = [wait(client, t, "running") for t in tasks[:2]]
            assert {first["execution_slot"], second["execution_slot"]} == {1, 2}
            assert status(client, tasks[2])["status"] == "queued"
            channels = client.get(route, headers=AUTH).json()["channels"]
            assert [c["execution_slot"] for c in channels] == [1, 2]
            assert {c["task_id"] for c in channels} == set(tasks[:2])
            for job in (first, second):
                channel = channels[job["execution_slot"] - 1]
                assert channel["status"] == "running" and channel["task_id"] == job["task_id"]
                assert channel["project_name"] == job["project_name"]
                assert channel["recent_action"] == "正在生成项目提示词"
                assert channel["documents_completed"] == channel["documents_total"] == 0
            assert client.get(f"{ROUTE}/{tasks[0]}/result", headers=AUTH).status_code == 409
        finally:
            release.set()
        for t in tasks:
            wait(client, t, "completed")
            assert client.get(f"{ROUTE}/{t}/result", headers=AUTH).json() == {"translation_prompt": prompt, "documents_markdown": []}
        assert client.get(route, headers=AUTH).json()["channels"] == idle
        for d in ({"source_filename": "../a.docx", "content_base64": "Zg=="},
                  {"source_filename": "a.doc", "content_base64": "broken!"},
                  {"source_filename": "empty.data", "content_base64": ""}):
            assert client.post(ROUTE, headers=AUTH, json=request([d])).status_code == 422


@pytest.mark.parametrize("filename", ["broken.doc", "broken.docx", "broken.xlsx", "broken.pdf"])
def test_corrupt_file_is_a_failed_task(tmp_path, filename):
    started = []
    async def agent(_settings, _root, session):
        started.append(True)
        raise AssertionError("model started before conversion succeeded")
    with TestClient(create_material_app(settings(tmp_path), agent)) as client:
        data = request([{"source_filename": "first.txt", "content_base64": "T0s="},
                        {"source_filename": filename, "content_base64": "eHg="}])
        task = client.post(ROUTE, headers=AUTH, json=data).json()["task_id"]
        failed = wait(client, task, "failed")
        assert failed["current_file"] == filename and failed["error"]["code"] == "INVALID_DOCUMENT"
        assert failed["documents_completed"] == 1 and not started
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
        assert client.get(f"{ROUTE}/{tasks[0]}/result", headers=AUTH).json()["translation_prompt"] == "saved"
        assert status(client, tasks[1])["status"] == "interrupted"


def test_explicit_contract_and_fenced_heading_example(tmp_path):
    session = MaterialSession(tmp_path, {"request": {"translation_requirements": "翻译 src，note 仅供参考。"},
                                         "documents": []}, lambda **_: None)
    prompt = "# 上游契约与模型任务字段\n\nsrc 为待译原文，note 为上下文。\n\n" + example_prompt("按本次文字要求执行。")
    prompt += "\n\n## 标签示例\n\n```text\n# 原文中的标签\n```"
    assert session.prompt(json.dumps({"translation_prompt": prompt})) == prompt


@pytest.mark.parametrize("prompt", [
    example_prompt(""),
    example_prompt("## 参考资料\n\n---"),
    "# 上游契约与模型任务字段\n\n源字段和目标字段。\n\n" + example_prompt("按业务标签执行。"),
    example_prompt("按项目需求执行。").replace("# 角色设定", "【角色设定】"),
    example_prompt("按项目需求执行。").replace("# 任务目标", "# 角色设定\n\n重复。\n\n# 任务目标"),
    example_prompt("按项目需求执行。") + "\n\n# 上游契约与模型任务字段\n\n字段说明。",
    "```markdown\n" + example_prompt("按项目需求执行。") + "\n```",
])
def test_invalid_prompt_layout_requests_correction(tmp_path, prompt):
    session = MaterialSession(tmp_path, {"request": {}, "documents": []}, lambda **_: None)
    with pytest.raises(ValueError, match="重新提交完整的 translation_prompt"):
        session.prompt(json.dumps({"translation_prompt": prompt}))


def test_email_link_with_embedded_fallback_keeps_original_picture(tmp_path):
    require_pandoc()
    Image = pytest.importorskip("PIL.Image")
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


@pytest.mark.parametrize("label", ["source.doc", "source.unknown"])
def test_old_word_and_original_page(tmp_path, label):
    require_pandoc()
    pytest.importorskip("pypdfium2")
    from codex_remote_worker.material_documents import office_convert, office_executable
    try:
        office_executable()
    except JobError:
        pytest.skip("LibreOffice is required for the legacy Word case")
    sample = tmp_path / "source.docx"
    sample.write_bytes(base64.b64decode(word("Legacy source line")["content_base64"]))
    source = office_convert(sample, tmp_path / "legacy", "doc")
    if source.name != label:
        source = source.rename(source.with_name(label))
    out = tmp_path / "prepared"
    assert "Legacy source line" in transcribe(source, out).read_text(encoding="utf-8")
    image, pages = page_preview(source, out, 1)
    assert pages >= 1 and image.startswith("data:image/jpeg;base64,")
