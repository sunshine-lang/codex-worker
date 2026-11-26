from pathlib import Path
import io
import json
import runpy
import sys
from types import SimpleNamespace

import pytest


save = runpy.run_path(str(Path(__file__).with_name("material_client.py")))["save_result"]


def result(*docs):
    return {"translation_prompt": "项目说明", "documents_markdown": list(docs)}


def test_download_uses_server_names_and_can_read_historical_results(tmp_path):
    old = {"source_filename": "同名.pdf", "markdown": "原文"}
    new = {**old, "markdown_filename": "实际  交付.md"}
    save(tmp_path / "new", result(new), ["同名.pdf"])
    assert (tmp_path / "new/实际  交付.md").read_bytes() == "原文".encode()
    assert not (tmp_path / "new/01-同名.md").exists()
    save(tmp_path / "old", result(old, {**old,"source_filename":"同名.docx"}))
    assert (tmp_path / "old/01-同名.md").is_file() and (tmp_path / "old/02-同名.md").is_file()
    save(tmp_path / "none", result(), [])
    assert (tmp_path/"none/翻译Prompt.md").is_file()


@pytest.mark.parametrize("bad", ["../other.md", r"..\other.md", "C:other.md", "NUL.md", "翻译Prompt.md", "",
                                  "01-ONE.MD", None])
def test_bad_delivered_names_are_rejected_before_writing(tmp_path, bad):
    first = {"source_filename":"one.txt", "markdown_filename":"01-one.md", "markdown":"one"}
    last = {**first,"source_filename":"two.pdf", "markdown_filename":bad}
    with pytest.raises(ValueError):
        save(tmp_path, result(first,last))
    assert not list(tmp_path.iterdir())


def test_partially_upgraded_item_is_reported(tmp_path):
    with pytest.raises(ValueError):
        save(tmp_path, result({"source_filename":"a","markdown_filename":"01-a.md","markdown":"one"},
                              {"source_filename":"b","markdown":"two"}))
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("export_files", [False, True])
def test_result_command_preserves_json_mode_and_can_save_delivered_names(tmp_path, monkeypatch, export_files):
    here = Path(__file__).parent
    module = runpy.run_path(str(here/"call.py"))
    data = result({"source_filename":"示例.pdf","markdown_filename":"服务端指定.md","markdown":"正文"})
    envelope = {"outputs":[{"outputs":[{"component_id":"ChatOutput-Materials",
                "results":{"message":{"text":json.dumps(data)}}}]}]}
    def open_request(*args, **kwargs):
        return io.BytesIO(json.dumps(envelope).encode())
    monkeypatch.setitem(module["main"].__globals__, "build_opener", lambda *_:SimpleNamespace(open=open_request))
    monkeypatch.syspath_prepend(str(here))
    monkeypatch.setenv("LANGFLOW_API_KEY", "local-fixture")
    target = tmp_path/"received.json"
    args = ["call.py","--flow-url","https://worker.example.test","result","mat_"+"1"*32,"--output",str(target)]
    monkeypatch.setattr(sys, "argv", args + (["--save-markdown"] if export_files else []))
    assert module["main"]() == 0 and json.loads(target.read_bytes()) == data
    assert (tmp_path/"服务端指定.md").is_file() == export_files
    assert (tmp_path/"翻译Prompt.md").is_file() == export_files
