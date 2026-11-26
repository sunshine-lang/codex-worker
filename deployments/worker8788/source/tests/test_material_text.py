import base64
import codecs
import csv
import io
import json
import re
import zipfile

import pytest
from fastapi.testclient import TestClient

from codex_remote_worker.common import JobError
from codex_remote_worker.material_documents import standalone_markdown, transcribe
from codex_remote_worker.material_service import create_material_app
from test_materials import AUTH, ROUTE, example_prompt, request, settings, wait


def attachment(name, data):
    return {"source_filename": name, "content_base64": base64.b64encode(data).decode("ascii")}


def literal(text, language):
    match = re.fullmatch(r"(`{3,})" + language + r"\n(.*?)\n\1\n", text, re.S)
    assert match
    return match[2]


def test_csv_and_plain_materials_share_one_task(tmp_path):
    csv_text = 'ID,cn,target\r\n0007,"line one,\r\nline ""two"" <A> & |",\r\n\r\n0099,=2+2,'
    rules = '  要求一\r\n\r\n要求二\t \r\n```\r\n![原文](assets/no-file.png)\r\n'
    docs = [attachment("原文.CSV", codecs.BOM_UTF8 + csv_text.encode()), attachment("客户要求.自定义", rules.encode("gb18030"))]
    prompt = example_prompt("[材料](material:0) 和 [材料](material:1)，按原记录引用客户要求。")

    async def agent(_settings, _root, session):
        for index in range(len(session.docs)):
            read = session.call("material_read", {"document": index, "offset": 0})
            assert json.loads(read[0]["text"])["next_offset"] is None
            with pytest.raises(ValueError, match="material_read"):
                session.call("material_page", {"document": index, "page": 1})
        return session.prompt(json.dumps({"translation_prompt": prompt}))

    with TestClient(create_material_app(settings(tmp_path), agent)) as client:
        ready = client.get("/v1/ready", headers=AUTH).json()
        assert ready["format_detection"] == "content" and {"csv", "txt"} <= set(ready["document_types"])
        payload = request(docs)
        receipt = client.post(ROUTE, headers=AUTH, json=payload)
        assert receipt.status_code == 202
        task = receipt.json()["task_id"]
        wait(client, task, "completed")
        result = client.get(f"{ROUTE}/{task}/result", headers=AUTH).json()
        assert client.post(ROUTE, headers=AUTH, json=payload).json()["task_id"] == task
    table, note = result["documents_markdown"]
    assert all("[`" + d["markdown_filename"] + "`]" in result["translation_prompt"] for d in (table, note))
    assert [table["source_filename"], note["source_filename"]] == [d["source_filename"] for d in docs]
    parts = re.split(r"(?m)^## 记录 (\d+)（原文件第 ([\d–]+) 行）\n\n", table["markdown"])
    assert parts[1::3] == ["1", "2", "3", "4"]
    assert parts[2::3] == ["1", "2–3", "4", "5"]
    recovered = "".join(literal(part.removesuffix("\n"), "csv") for part in parts[3::3])
    assert recovered == csv_text.replace("\r\n", "\n")
    assert list(csv.reader(io.StringIO(recovered)))[1] == ["0007", 'line one,\nline "two" <A> & |', ""]
    assert literal(note["markdown"], "text") == rules.replace("\r\n", "\n")


@pytest.mark.parametrize(("bom","codec"), [(codecs.BOM_UTF16_BE,"utf-16-be"),(codecs.BOM_UTF32_LE,"utf-32-le"),
                                           (codecs.BOM_UTF8,"utf-8"),(b"", "gb18030")])
def test_text_encoding_and_source_markup(tmp_path, bom, codec):
    path = tmp_path / "sample.txt"
    content = 'A | 中文\r\n\r\n<img src="assets/example.png">\r\n  '
    path.write_bytes(bom + content.encode(codec))
    folder = tmp_path / "out"
    transcribe(path, folder)
    assert literal(standalone_markdown(folder), "text") == content.replace("\r\n", "\n")


def test_recognizes_a_table_without_an_extension_and_long_fields(tmp_path):
    data = "id;source\n001;" + "x"*140_000
    path = tmp_path / "unlabelled"
    path.write_text(data)
    md = transcribe(path, tmp_path/"out").read_text()
    assert "## 记录 2（原文件第 2 行）" in md
    assert data.splitlines()[1] in md


def test_bad_csv_and_unreadable_binary_do_not_publish_partial_results(tmp_path):
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as package:
        package.writestr("other.txt", "unrelated archive")
    async def agent(_settings, _root, session):
        for index in range(len(session.docs)):
            session.call("material_read", {"document": index, "offset": 0})
        raise AssertionError("unreadable document unexpectedly parsed")
    broken = [("broken.csv", b'id,text\n1,"unterminated', "INVALID_DOCUMENT"),
              ("unknown.blob", b"\x00\xff\x10\x00", "UNSUPPORTED_DOCUMENT"),
              ("opaque", archive.getvalue(), "UNSUPPORTED_DOCUMENT")]
    with TestClient(create_material_app(settings(tmp_path), agent)) as client:
        for filename, raw, code in broken:
            reply = client.post(ROUTE, headers=AUTH, json=request([attachment("first.txt", b"OK"), attachment(filename, raw)]))
            assert reply.status_code == 202
            task = reply.json()["task_id"]
            state = wait(client, task, "failed")
            assert state["current_file"] == filename
            assert state["error"]["code"] == code
            assert client.get(f"{ROUTE}/{task}/result", headers=AUTH).status_code == 409


def test_output_budget_applies_to_plain_records(tmp_path, monkeypatch):
    from codex_remote_worker import material_documents
    path = tmp_path / "table.csv"
    path.write_text("a,b\n1,2\n")
    monkeypatch.setattr(material_documents, "MAX_DELIVERY", 45)
    with pytest.raises(JobError) as error:
        transcribe(path, tmp_path/"out")
    assert error.value.code == "LIMIT_EXCEEDED"
