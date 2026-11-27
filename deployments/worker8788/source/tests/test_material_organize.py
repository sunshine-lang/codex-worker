import base64
from io import BytesIO
import json

from docx import Document
from fastapi.testclient import TestClient
from PIL import Image
import pytest

from codex_remote_worker.material_organize import figure_urls, organize_documents
from codex_remote_worker.material_service import create_material_app
from test_materials import AUTH, ROUTE, request, settings, wait


def test_normalized_guide_keeps_merged_columns_code_and_figure_order():
    uri = "data:image/png;base64,AAAA"
    template = f"""**项目指南**

一. **术语**

参考 `![]({uri})` 这个语法。

<table><tr><td>英文</td><td>错误翻译</td><td>标准翻译</td></tr>
<tr><td rowspan="2">device</td><td>甲</td><td>乙</td></tr>
<tr><td>丙</td><td>丁</td></tr></table>

<img src="{uri}"/><img src="{uri}"/>

```txt
![]({uri})
```
"""
    doc = {"source_filename": "guide.docx", "markdown_filename": "01-guide.md", "markdown": template}
    assert list(figure_urls(template)) == [uri, uri]
    notes = {"im_figures": [[{"number": 1, "text": "旧译 X 已划掉。"}, {"number": 2, "text": "新译 Y。"}]]}
    result = organize_documents([doc], notes)["documents_markdown"][0]["markdown"]
    assert result.startswith("# 项目指南\n") and "## 一、术语" in result
    assert "| 英文 | 错误翻译 | 标准翻译 |" in result
    assert "| device | 甲 | 乙 |" in result and "| device | 丙 | 丁 |" in result
    assert result.count(uri) == 2 and result.index("旧译 X") < result.index("新译 Y")
    with pytest.raises(ValueError, match="不完整"):
        organize_documents([doc], {"im_figures": [[]]})


def test_organize_api_requires_visual_read_and_keeps_legacy_submission_separate(tmp_path):
    image = BytesIO()
    Image.new("RGB", (20, 10), "red").save(image, format="PNG")
    image.seek(0)
    word = Document()
    word.add_heading("客户规则", 1)
    word.add_paragraph("截图中的勾选项只适用于表格。")
    word.add_picture(image)
    binary = BytesIO()
    word.save(binary)
    data = request([{"source_filename": "Guide.docx", "content_base64": base64.b64encode(binary.getvalue()).decode()}])
    value = {"im_figures": [[{"number": 1, "text": "图示为表格，其他语境按正文。"}]]}

    async def reader(_settings, _root, session):
        session.call("material_read", {"document": 0, "offset": 0})
        with pytest.raises(ValueError, match="material_figure"):
            session.prompt(json.dumps(value))
        seen = session.call("material_figure", {"document": 0, "number": 1})
        assert seen[1]["type"] == "inputImage"
        assert base64.b64decode(seen[1]["imageUrl"].split(",", 1)[1]) == image.getvalue()
        return session.prompt(json.dumps(value))

    with TestClient(create_material_app(settings(tmp_path), reader)) as client:
        receipt = client.post(ROUTE + "/organize", headers=AUTH, json=data)
        assert receipt.status_code == 202
        task = receipt.json()["task_id"]
        assert client.post(ROUTE + "/organize", headers=AUTH, json=data).json()["task_id"] == task
        assert client.post(ROUTE, headers=AUTH, json=data).status_code == 409
        wait(client, task, "completed")
        result = client.get(f"{ROUTE}/{task}/result", headers=AUTH).json()
        assert set(result) == {"processing_mode", "documents_markdown"}
        markdown = result["documents_markdown"][0]["markdown"]
        assert "〔原图 1〕图示为表格" in markdown and "data:image" not in markdown


def test_text_only_organize_skips_model(tmp_path):
    async def unused(*args):
        pytest.fail("A text-only reading copy does not require a model turn.")
    data = request([{"source_filename": "说明.txt", "content_base64": base64.b64encode("保留 {0}".encode()).decode()}])
    with TestClient(create_material_app(settings(tmp_path), unused)) as client:
        task = client.post(ROUTE + "/organize", headers=AUTH, json=data).json()["task_id"]
        wait(client, task, "completed")
        result = client.get(f"{ROUTE}/{task}/result", headers=AUTH).json()
        assert "保留 {0}" in result["documents_markdown"][0]["markdown"]
