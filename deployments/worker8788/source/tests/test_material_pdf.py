import base64
import io
import json
from pathlib import Path
import re

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from codex_remote_worker.common import JobError
from codex_remote_worker import material_documents
from codex_remote_worker.material_service import create_material_app
from test_materials import AUTH, ROUTE, example_prompt, request, settings, wait


SAMPLE = Path(__file__).parent / "fixtures" / "mixed-pages.pdf"
PHRASE = "wherein the first member comprises <X>."


def test_pdf_text_and_scan_through_material_service(tmp_path):
    prompt = example_prompt(PHRASE)
    previews = []

    async def agent(_settings, _folder, session):
        text = session.call("material_read", {"document": 0, "offset": 0})
        part = json.loads(text[0]["text"])
        assert part["next_offset"] is None and PHRASE in part["markdown"]
        assert part["pages_to_view"] == [1, 2]
        assert "base64," not in part["markdown"]
        with pytest.raises(ValueError, match="第 1 页"):
            session.prompt(json.dumps({"translation_prompt": prompt}))
        for page in (0, 4, True):
            with pytest.raises(ValueError, match="1 到 3"):
                session.call("material_page", {"document": 0, "page": page})
        for page in (1, 2, 3):
            items = session.call("material_page", {"document": 0, "page": page})
            assert f"第 {page} 页，共 3 页" in items[0]["text"]
            previews.append(items[1]["imageUrl"])
            if page == 1:
                with pytest.raises(ValueError, match="第 2 页"):
                    session.prompt(json.dumps({"translation_prompt": prompt}))
            if page == 2:
                assert session.reading_complete
        return session.prompt(json.dumps({"translation_prompt": prompt}))

    doc = {"source_filename": "测试.PDF", "content_base64": base64.b64encode(SAMPLE.read_bytes()).decode()}
    with TestClient(create_material_app(settings(tmp_path), agent)) as client:
        assert "pdf" in client.get("/v1/ready", headers=AUTH).json()["document_types"]
        submitted = client.post(ROUTE, headers=AUTH, json=request([doc]))
        assert submitted.status_code == 202
        task = submitted.json()["task_id"]
        wait(client, task, "completed")
        result = client.get(f"{ROUTE}/{task}/result", headers=AUTH).json()
    assert result["translation_prompt"] == prompt
    [delivery] = result["documents_markdown"]
    assert delivery["source_filename"] == doc["source_filename"]
    md = delivery["markdown"]
    assert re.findall(r"^## 第 (\d+) 页$", md, re.M) == ["1", "2", "3"]
    assert PHRASE in md and "![example](assets/placeholder.png)" in md
    assert "本页无可提取的文字" in md
    assert re.findall(r"^!\[第 \d+ 页原图\]\((data:image/png;base64,[^)]+)\)$", md, re.M) == previews
    for uri in previews:
        with Image.open(io.BytesIO(base64.b64decode(uri.split(",", 1)[1], validate=True))) as im:
            assert im.format == "PNG" and im.convert("L").getextrema()[0] < 100


def test_page_images_count_towards_delivery_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(material_documents, "MAX_DELIVERY", 1024)
    with pytest.raises(JobError) as failure:
        material_documents.transcribe(SAMPLE, tmp_path)
    assert failure.value.code == "LIMIT_EXCEEDED"


def test_exported_chat_input_lists_the_worker_formats():
    root = Path(__file__).resolve().parents[4]
    graph = json.loads((root / "integrations/materials/flow.json").read_text(encoding="utf-8"))["data"]
    chat = next(node for node in graph["nodes"] if node["id"] == "ChatInput-Materials")
    assert chat["data"]["node"]["template"]["files"]["fileTypes"] == list(material_documents.DOCUMENT_TYPES)
