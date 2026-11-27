import base64
import hashlib
import json
from io import BytesIO
from pathlib import Path

import pytest
from codex_remote_worker.material_patent import PIPELINE
from codex_remote_worker.material_patent_session import (
    FrozenPromptSession,
    PatentEvidenceSession,
    PatentReviewSession,
)
from codex_remote_worker.material_service import create_material_app
from docx import Document
from fastapi.testclient import TestClient
from PIL import Image
from test_materials import AUTH, ROUTE, example_prompt, request, wait
from test_materials import settings as make_settings

from codex_remote_worker.common import canonical


def image_guide():
    image = BytesIO()
    Image.new("RGB", (25, 15), "blue").save(image, format="PNG")
    image.seek(0)
    doc = Document()
    doc.add_heading("客户修改", 1)
    doc.add_paragraph("iii) retain the original item")
    doc.add_picture(image)
    output = BytesIO()
    doc.save(output)
    return output.getvalue()


def submit_files(*entries):
    return {
        **request(
            [
                {"source_filename": name, "content_base64": base64.b64encode(data).decode()}
                for name, data in entries
            ]
        ),
        "business_tag": "专利",
    }


def read_all(session, index):
    offset = 0
    body = []
    while offset is not None:
        part = json.loads(session.call("material_read", {"document": index, "offset": offset})[0]["text"])
        body.append(part["markdown"])
        offset = part["next_offset"]
    return "".join(body)


def test_patent_submit_uses_independent_image_review_then_exact_reading_copy(tmp_path):
    phases = []
    expected = []
    pdf = (Path(__file__).parent / "fixtures/mixed-pages.pdf").read_bytes()
    source = submit_files(("客户.docx", image_guide()), ("参考.pdf", pdf))
    initial = "iii) and iv) the next half"
    revised = "iii) …; and the next half。原图在同一项内补入 and。"
    prompt = example_prompt("[材料](material:0)；[材料](material:1)\n" + revised)

    async def agent(_settings, folder, session):
        phases.append(type(session))
        if isinstance(session, FrozenPromptSession):
            assert [t["name"] for t in session.tools] == ["material_read"]
            with pytest.raises(ValueError):
                session.call("material_page", {"document": 0, "page": 1})
            with pytest.raises(ValueError):
                session.prompt(json.dumps({"translation_prompt": prompt}))
            expected.extend(read_all(session, i) for i in range(2))
            assert revised in expected[0] and initial not in expected[0]
            assert "wherein the first member comprises" in expected[1]
            assert "扫描中的独立例句" in expected[1]
            assert "data:image/" not in "".join(expected)
            wrong_goal = prompt.replace("# 任务目标\n", "# 任务目标\n无需生成句段译文或回填订单。\n")
            with pytest.raises(ValueError, match="收到业务订单后的译员"):
                session.prompt(json.dumps({"translation_prompt": wrong_goal}))
            return session.prompt(json.dumps({"translation_prompt": prompt}))
        review = isinstance(session, PatentReviewSession)
        for i, d in enumerate(session.model_documents()):
            read_all(session, i)
            for f in d["figures"]:
                args = {"document": i, "number": f["number"]}
                with pytest.raises(ValueError):
                    session.call(
                        "material_verify" if review else "material_note",
                        {**args, "decision": "keep", "text": ""},
                    )
                result = session.call("material_figure", args)
                assert result[1]["type"] == "inputImage"
                if review:
                    assert "首轮记录" in result[-1]["text"]
                    session.call(
                        "material_verify",
                        {**args, "decision": "keep" if i else "replace", "text": "" if i else revised},
                    )
                else:
                    with pytest.raises(ValueError):
                        session.call("material_note", {**args, "text": "见原图"})
                    session.call(
                        "material_note", {**args, "text": initial if i == 0 else "扫描中的独立例句。"}
                    )
        return session.prompt('{"complete":true}')

    with TestClient(create_material_app(make_settings(tmp_path), agent)) as client:
        assert client.get("/v1/ready", headers=AUTH).json()["patent_reading_prompt"] == PIPELINE
        created = client.post(ROUTE, headers=AUTH, json=source)
        assert created.status_code == 202
        task = created.json()["task_id"]
        assert client.post(ROUTE, headers=AUTH, json=source).json()["task_id"] == task
        status = wait(client, task, "completed")
        result = client.get(f"{ROUTE}/{task}/result", headers=AUTH).json()
        assert phases == [PatentEvidenceSession, PatentReviewSession, FrozenPromptSession]
        assert [d["markdown"] for d in result["documents_markdown"]] == expected
        assert revised in result["translation_prompt"]
        index = result["prompt_input"]
        assert (
            index["sha256"]
            == status["reading_copy_sha256"]
            == hashlib.sha256(canonical(index["files"])).hexdigest()
        )
        assert [d["sha256"] for d in index["files"]] == [
            hashlib.sha256(t.encode()).hexdigest() for t in expected
        ]
        assert [d["source_sha256"] for d in index["files"]] == [
            hashlib.sha256(base64.b64decode(d["content_base64"])).hexdigest() for d in source["documents"]
        ]
        assert json.loads((tmp_path / "tasks" / task / "reading-review.json").read_bytes())["documents"][0][
            "views"
        ][0]["revised"]


def test_patent_changed_frozen_input_cannot_publish(tmp_path):
    async def agent(_settings, folder, session):
        assert isinstance(session, FrozenPromptSession)  # Text only; no visual turns needed.
        read_all(session, 0)
        prompt = session.prompt(json.dumps({"translation_prompt": example_prompt("[材料](material:0)")}))
        (folder / "reading-copy" / session.docs[0]["markdown_filename"]).write_text("replaced")
        return prompt

    with TestClient(create_material_app(make_settings(tmp_path), agent)) as client:
        task = client.post(ROUTE, headers=AUTH, json=submit_files(("a.txt", b"original"))).json()["task_id"]
        failed = wait(client, task, "failed")
        assert failed["error"]["code"] == "INPUT_CHANGED"
        assert client.get(f"{ROUTE}/{task}/result", headers=AUTH).status_code == 409


def test_patent_pdf_keeps_separate_examples_and_inserts_the_figure_at_its_source_position():
    from codex_remote_worker.material_patent import render_document

    views = [
        {"kind": "pdf_page", "page": 1, "native_text": True},
        {"kind": "pdf_figure", "page": 1, "figure": 1, "box": (10, 40, 110, 60)},
    ]
    content = {
        "pages": [
            {
                "page": 1,
                "lines": [
                    ("First example", "First example", 10),
                    ("Second example", "<em>Second example</em>", 20),
                    ("Next section", "Next section", 70),
                ],
                "page_id": 0,
                "end": 2,
                "links": [],
                "transparent": "",
            }
        ],
        "parts": [],
        "views": views,
        "revisions": [],
    }
    body = render_document({"source_filename": "guide.pdf"}, content, ["", "(a) and (b)，同一项内并列。"])
    assert "First example  \n<em>Second example</em>" in body
    assert body.index("Second example") < body.index("图中例 1.1") < body.index("Next section")


def test_patent_document_with_no_figures_keeps_original_records(tmp_path):
    calls = []

    async def only_final(_settings, _folder, session):
        calls.append(type(session))
        body = read_all(session, 0)
        assert "001" in body and "{0}" in body
        return session.prompt(json.dumps({"translation_prompt": example_prompt("[材料](material:0)")}))

    with TestClient(create_material_app(make_settings(tmp_path), only_final)) as client:
        response = client.post(ROUTE, headers=AUTH, json=submit_files(("notes.txt", b"001\n{0}\n")))
        task = response.json()["task_id"]
        wait(client, task, "completed")
        result = client.get(f"{ROUTE}/{task}/result", headers=AUTH).json()
        assert calls == [FrozenPromptSession]
        assert result["processing_mode"] == PIPELINE


def test_published_prompt_removes_standalone_receipt_text_only():
    from codex_remote_worker.material_patent_session import downstream_prompt

    prompt = example_prompt("[材料](material:0)")
    goal = "将操作说明译为简体中文。"
    administrative = "本轮无待译业务订单，仅交付项目翻译要求。"
    original = prompt.replace(goal, goal + administrative)
    quoted = "\n\n> " + administrative + "\n\n```text\n" + administrative + "\n```"
    assert downstream_prompt(original + quoted) == prompt + quoted
