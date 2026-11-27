import asyncio
import json
from types import SimpleNamespace
from urllib.parse import unquote, urlsplit

from fastapi.testclient import TestClient
from lxml import html
import pytest
import subprocess

from codex_remote_worker.material_agent import conversation
from codex_remote_worker.material_documents import pandoc_executable
from codex_remote_worker.material_references import material_manifest, resolve_references
from codex_remote_worker.material_service import create_material_app
from test_materials import AUTH, ROUTE, example_prompt, request, settings, wait


def test_long_multibyte_stems_fit_downstream_filesystems(tmp_path):
    stem = "方案" * 90
    docs = material_manifest([{"source_filename":stem+".doc"}, {"source_filename":stem+".pdf"}])
    for i, doc in enumerate(docs, 1):
        name = doc["markdown_filename"]
        assert name.startswith(f"{i:02d}-") and name.endswith(".md") and len(name.encode()) <= 255
        (tmp_path/name).write_text("正文")
    assert len(list(tmp_path.iterdir())) == 2


def test_resolves_actual_links_without_editing_examples_or_external_titles():
    docs = material_manifest([{"source_filename": "原  文[1] _`(100%)#甲&amp;.pdf"},
                              {"source_filename": "原  文[1] _`(100%)#甲&amp;.docx"}, {"source_filename": "无后缀"}])
    refs = "、".join(d["reference"] for d in docs)
    original = "> 客户原句：保留文件名 原  文[1] _`(100%)#甲&amp;.pdf。"
    code = "```text\n[材料](material:0)\n```"
    prompt = example_prompt(refs + " 第 7 页。\n\n项目需求。另见《外部手册.md》（未提供）。\n\n" + original + "\n\n" + code)
    final = resolve_references(prompt, docs)
    assert original in final and code in final and "《外部手册.md》（未提供）" in final
    assert resolve_references(final, docs) == final
    tree = html.fragment_fromstring(subprocess.check_output([pandoc_executable(), "--sandbox", "--wrap=none", "-f", "gfm", "-t", "html5"],
                                                           input=final.encode()).decode(), create_parent="main")
    links = [(a.text_content(), unquote(urlsplit(a.get("href")).path)) for a in tree.iter("a")]
    assert links == [(d["markdown_filename"], d["markdown_filename"]) for d in docs]
    assert links[-1][0] == "03-无后缀.md"


@pytest.mark.parametrize("catalog, extra", [
    ("只读了文件", ""), ("`[材料](material:0)`", ""), ("[材料](material:9)", ""),
    ("[材料](material:0)", "参见《客户指南.docx》第 3 条"),
    ("[材料](material:0)", "《客户指南.docx》（未提供）"),
    ("[材料](material:0)", "见 [手册](missing.md)"), ("[材料](material:0)", "见 `02-不存在.md`"),
    ("[材料](material:0)", "[01-客户指南.md](https://example.org/other)"),
    ("[材料](material:0)", "[客户指南.docx](https://example.org/other)"),
    ("[材料](material:0)", "[文件甲](01-客户指南.md)"),
    ("[材料](material:0)", "[01-客户指南.md](../01-客户指南.md)"),
])
def test_bad_or_missing_citation_is_rejected(catalog, extra):
    docs = material_manifest([{"source_filename":"客户指南.docx"}])
    with pytest.raises(ValueError):
        resolve_references(example_prompt(catalog) + "\n\n" + extra, docs)


def test_correction_identifies_all_redundant_names_even_when_the_links_are_valid():
    docs = material_manifest([{"source_filename": "面板反馈.docx"}, {"source_filename": "操作范围.txt"}])
    catalog = "[材料](material:0) 原图 1：按钮修订。\n\n[材料](material:1) 正文：适用对象。"
    with pytest.raises(ValueError) as rejected:
        resolve_references(example_prompt(catalog + "\n\n《01-面板反馈.md》（原件《面板反馈.docx》）和 `02-操作范围.md`。"), docs)
    message = str(rejected.value)
    assert "面板反馈.docx、01-面板反馈.md → [材料](material:0)" in message
    assert "02-操作范围.md → [材料](material:1)" in message
    result = resolve_references(example_prompt(catalog), docs)
    assert resolve_references(result, docs) == result


def test_unresolved_citations_cannot_publish_a_partial_result(tmp_path):
    async def agent(_settings, _folder, session):
        session.call("material_read", {"document":0,"offset":0})
        return example_prompt("[材料](material:0) 另参见 [失效引用](missing.md)")
    with TestClient(create_material_app(settings(tmp_path),agent)) as client:
        resp = client.post(ROUTE, headers=AUTH, json=request([{"source_filename":"说明.txt","content_base64":"T0s="}]))
        task = resp.json()["task_id"]
        failed = wait(client, task, "failed")
        assert failed["error"]["code"] == "INCOMPLETE_DELIVERY"
        assert client.get(f"{ROUTE}/{task}/result", headers=AUTH).status_code == 409
    assert not (tmp_path/"tasks"/task/"result.json").exists()


def test_reference_correction_and_prepared_bytes_reach_downstream(tmp_path):
    async def agent(_settings, folder, session):
        [entry] = session.docs
        deployed = folder/"markdown"/entry["markdown_filename"]
        assert deployed.is_file() and session.prepared_count == 1
        part = json.loads(session.call("material_read", {"document":0,"offset":0})[0]["text"])
        assert part["markdown_filename"] == deployed.name and part["reference"] == entry["reference"]
        attempts = []
        queue = asyncio.Queue()
        async def turn(_method, args):
            attempts.append(args["input"][0]["text"])
            if len(attempts) == 1:
                assert json.loads(attempts[0])["documents"][0]["markdown_filename"] == deployed.name
                message = example_prompt("见《要求.txt》")
            else:
                assert "reference" in attempts[-1]
                message = example_prompt(entry["reference"] + " 首段；额外资料《外部手册.pdf》（未提供）")
            turn_id = str(len(attempts))
            done = {"id":turn_id,"status":"completed","items":[{"type":"agentMessage","text":json.dumps({"translation_prompt":message})}]}
            await queue.put({"method":"turn/completed","params":{"threadId":"test","turnId":turn_id,"turn":done}})
            return {"turn":{"id":turn_id}}
        final = await conversation(SimpleNamespace(request=turn), "test", queue, session)
        assert len(attempts) == 2
        return final
    with TestClient(create_material_app(settings(tmp_path),agent)) as client:
        r = client.post(ROUTE, headers=AUTH, json=request([{"source_filename":"要求.txt","content_base64":"T0s="}]))
        task = r.json()["task_id"]
        wait(client,task,"completed")
        out = client.get(f"{ROUTE}/{task}/result",headers=AUTH).json()
    [delivery] = out["documents_markdown"]
    assert set(delivery) == {"source_filename","markdown_filename","markdown"}
    assert delivery["markdown_filename"] == "01-要求.md"
    assert (tmp_path/"tasks"/task/"markdown"/"01-要求.md").read_bytes() == delivery["markdown"].encode()
    assert "[`01-要求.md`]" in out["translation_prompt"] and "《外部手册.pdf》（未提供）" in out["translation_prompt"]
