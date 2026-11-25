import base64
import hashlib
import io
import json
import re
import zipfile

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook
from openpyxl.drawing.image import Image as SheetImage
from PIL import Image

from codex_remote_worker.common import JobError
from codex_remote_worker.material_documents import page_preview, standalone_markdown, transcribe
from codex_remote_worker.material_service import create_material_app
from test_materials import AUTH, ROUTE, example_prompt, request, require_pandoc, settings, wait, word


def xlsx_sample():
    book = Workbook()
    sheet = book.active
    sheet.title = "Feedback"
    sheet.append(["Project", "Rule", "Picture"])
    sheet.append(["WO01", "Use the approved term."])
    sheet.merge_cells("A2:A3")
    sheet["B3"] = "Leave the locked row untouched."
    other = book.create_sheet("Other")
    other["B2"] = 0
    other["D2"] = "=1+2"  # openpyxl does not create a cached result
    by_sheet_cell = []
    for tab, anchor, colour in [(sheet, "C2", "red"), (sheet, "C2", "blue"),
                                (sheet, "C3", "yellow"), (other, "C4", "green")]:
        stream = io.BytesIO()
        Image.new("RGB", (13, 7), colour).save(stream, format="PNG")
        by_sheet_cell.append((tab.title, anchor, hashlib.sha256(stream.getvalue()).hexdigest()))
        stream.seek(0)
        tab.add_image(SheetImage(stream), anchor)
    out = io.BytesIO()
    book.save(out)
    return out.getvalue(), by_sheet_cell


@pytest.mark.parametrize("word_name, sheet_name", [("材料.docx", "客户原表.xlsx"), ("指南.blob", "数据.pdf")])
def test_original_xlsx_via_material_service_with_word(tmp_path, word_name, sheet_name):
    require_pandoc()
    raw, expected = xlsx_sample()
    async def agent(_settings, _folder, session):
        for index in range(len(session.docs)):
            offset = 0
            while offset is not None:
                part = session.call("material_read", {"document": index, "offset": offset})
                offset = json.loads(part[0]["text"])["next_offset"]
        return example_prompt("规则与所在行的截图一起读取。")
    data = request([word("Style", name=word_name), {"source_filename":sheet_name,
                                    "content_base64":base64.b64encode(raw).decode()}])
    with TestClient(create_material_app(settings(tmp_path), agent)) as client:
        assert "xlsx" in client.get("/v1/ready", headers=AUTH).json()["document_types"]
        r = client.post(ROUTE, headers=AUTH, json=data)
        assert r.status_code == 202, r.text
        task = r.json()["task_id"]
        wait(client, task, "completed")
        out = client.get(f"{ROUTE}/{task}/result", headers=AUTH).json()["documents_markdown"]
        assert [d["source_filename"] for d in out] == [word_name, sheet_name]
        md = out[1]["markdown"]
        assert "A2:A3" in md and "WO01" in md and "B2　0" in md and "=1+2" in md
        occurrences = []
        for line in md.splitlines():
            if line.startswith("# 工作表 "):
                sheet = line.removeprefix("# 工作表 ")
            elif line.startswith("## 第 "):
                row = int(line.split()[2])
            elif line.startswith("<img "):
                payload = re.search(r'src="data:image/png;base64,([^"]+)"', line)[1]
                coordinate = re.search(r'alt="[^"]+ · ([A-Z]+[0-9]+) 截图"', line)[1]
                assert int(re.search(r'[0-9]+', coordinate)[0]) == row
                occurrences.append((sheet, coordinate, hashlib.sha256(base64.b64decode(payload)).hexdigest()))
        assert occurrences == expected
        assert client.post(ROUTE, headers=AUTH, json=data).json()["task_id"] == task


def test_image_preview_uses_row_view(tmp_path, monkeypatch):
    import codex_remote_worker.material_documents as docs
    require_pandoc()
    source = tmp_path / "input.xlsx"
    source.write_bytes(xlsx_sample()[0])
    folder = tmp_path / "output"
    transcribe(source, folder)
    def capture(path, dest, target):
        assert path == folder / "spreadsheet.docx" and target == "pdf"
        raise RuntimeError("verified preview input")
    monkeypatch.setattr(docs, 'office_convert', capture)
    with pytest.raises(RuntimeError, match="verified preview input"):
        page_preview(source, folder, 1)


def test_cropped_picture_on_its_original_row(tmp_path):
    require_pandoc()
    raw, _ = xlsx_sample()
    source = tmp_path / "crop.xlsx"
    from lxml import etree
    with zipfile.ZipFile(io.BytesIO(raw)) as inp, zipfile.ZipFile(source, "w") as out:
        for name in inp.namelist():
            value = inp.read(name)
            if name == "xl/drawings/drawing1.xml":
                drawing = etree.fromstring(value)
                fill = drawing.find(".//{*}blipFill")
                art = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
                fill.insert(1, etree.Element(art + "srcRect", l="25000", t="20000"))
                value = etree.tostring(drawing)
            out.writestr(name, value)
    folder = tmp_path / "output"
    transcribe(source, folder)
    md = standalone_markdown(folder)
    data = re.search(r'src="data:image/png;base64,([^"]+)"[^>]+alt="Feedback · C2 截图"', md)[1]
    with Image.open(io.BytesIO(base64.b64decode(data))) as im:
        assert im.size == (10, 6)


def test_external_image_reference_fails_explicitly(tmp_path):
    raw, _ = xlsx_sample()
    source = tmp_path / "external.xlsx"
    with zipfile.ZipFile(io.BytesIO(raw)) as inp, zipfile.ZipFile(source, "w") as out:
        for name in inp.namelist():
            value = inp.read(name)
            if "drawings/_rels/" in name:
                value = value.replace(b' Target="', b' TargetMode="External" Target="')
            out.writestr(name, value)
    with pytest.raises(JobError) as failure:
        transcribe(source, tmp_path/'output')
    assert failure.value.code == "EXTERNAL_RESOURCE"
