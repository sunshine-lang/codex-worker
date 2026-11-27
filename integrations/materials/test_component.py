"""Gateway configuration tests. Run in a Langflow environment."""
import asyncio
import base64
import hashlib
from functools import partial
import json
from pathlib import Path
import runpy
from types import MethodType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest

httpx = pytest.importorskip("httpx")
pytest.importorskip("lfx")
ROOT = Path(__file__).parent
component = runpy.run_path(str(ROOT / "component.py"))
Gateway = component["MaterialsGateway"]
TASK = "mat_" + "1" * 32


def run(monkeypatch, operation, entered, saved, *, token="fixture"):
    monkeypatch.setenv("MATERIALS_WORKER_URL", "https://environment.test")
    monkeypatch.delenv("MATERIALS_WORKER_TOKEN", raising=False)
    calls = []
    def reply(request):
        calls.append((request.method, str(request.url)))
        return httpx.Response(200, json={"status": "ok"})
    monkeypatch.setattr(httpx, "AsyncClient", partial(httpx.AsyncClient, transport=httpx.MockTransport(reply)))
    data = {"operation": operation, "task_id": TASK}
    if operation in ("submit", "organize"):
        data.update(project_name="fixture", business_tag="IM", source_language="en", target_language="zh")
    gateway = SimpleNamespace(request=SimpleNamespace(text=json.dumps(data)), worker_url=entered,
                              worker_token=token, saved_worker_url=AsyncMock(return_value=saved))
    gateway.submission = MethodType(Gateway.submission, gateway)
    message = asyncio.run(Gateway.dispatch(gateway))
    return json.loads(message.text), calls


@pytest.mark.parametrize(("operation", "method", "path"), [
    ("organize", "POST", "/v1/material-tasks/organize"),
    ("submit", "POST", "/v1/material-tasks"), ("status", "GET", f"/v1/material-tasks/{TASK}"),
    ("result", "GET", f"/v1/material-tasks/{TASK}/result")])
def test_saved_address_drives_each_operation(monkeypatch, operation, method, path):
    base = "https://worker.test/materials"
    value, calls = run(monkeypatch, operation, "  " + base + "/ ", base)
    assert value["status"] == "ok" and calls == [(method, base + path)]


def test_blank_editor_address_uses_environment(monkeypatch):
    value, calls = run(monkeypatch, "status", "", "")
    assert value["status"] == "ok" and calls == [("GET", "https://environment.test/v1/material-tasks/" + TASK)]


def test_unsaved_address_is_not_used_for_a_bearer_request(monkeypatch):
    value, calls = run(monkeypatch, "status", "https://edited.test", "https://saved.test")
    assert value["error"]["code"] == "INVALID_INPUT" and calls == []


def test_missing_token_names_the_field(monkeypatch):
    value, calls = run(monkeypatch, "status", "https://worker.test", "https://worker.test", token="")
    assert "材料 Worker 令牌" in value["error"]["message"] and calls == []


def test_export_shows_address_next_to_token():
    graph = json.loads((ROOT / "flow.json").read_text())["data"]
    node = next(n["data"]["node"] for n in graph["nodes"] if n["id"] == "CustomComponent-MaterialsGateway")
    assert node["field_order"] == ["request", "worker_url", "worker_token"]
    assert node["template"]["worker_url"] == next(f.to_dict() for f in Gateway.inputs if f.name == "worker_url")
    assert node["template"]["code"]["value"] == (ROOT / "component.py").read_text()
    assert node["metadata"]["code_hash"] == hashlib.sha256((ROOT / "component.py").read_bytes()).hexdigest()[:12]
    entry = next(n for n in graph["nodes"] if n["id"] == "ChatInput-Materials")
    assert "fileTypes" not in entry["data"]["node"]["template"]["files"]


def test_client_and_gateway_apply_one_budget_to_all_originals(tmp_path, monkeypatch):
    make_documents = runpy.run_path(str(ROOT / "material_client.py"))["make_documents"]
    gateway_settings = Gateway.submission.__globals__
    assert make_documents.__globals__["MAX_BYTES"] == gateway_settings["MAX_INPUT_BYTES"] == 128 * 1024**2
    monkeypatch.setitem(make_documents.__globals__, "MAX_BYTES", 8)
    monkeypatch.setitem(gateway_settings, "MAX_INPUT_BYTES", 8)
    files = [tmp_path / "first.txt", tmp_path / "second.txt"]
    for file in files:
        file.write_bytes(b"1234")
    body = {"project_name": "fixture", "business_tag": "IM", "source_language": "en", "target_language": "zh"}
    docs = make_documents(files)
    assert Gateway.submission(None, {**body, "documents": docs}, [])["documents"] == docs
    files[1].write_bytes(b"12345")
    with pytest.raises(ValueError):
        make_documents(files)
    # Four and five bytes encode to the same length. Check the decoded total as well.
    docs[1]["content_base64"] = base64.b64encode(b"12345").decode()
    with pytest.raises(ValueError):
        Gateway.submission(None, {**body, "documents": docs}, [])


@pytest.mark.parametrize("filename", ["feedback.xlsx", "claims.PDF", "source.csv", "requirements.txt", "custom.data", "no_suffix"])
def test_attachment_is_forwarded_verbatim_for_both_upload_paths(tmp_path, filename, monkeypatch):
    body = {"project_name":"example", "business_tag":"life sciences", "source_language":"en", "target_language":"zh"}
    scope = tmp_path / "current-flow"
    scope.mkdir()
    source = scope / filename
    source.write_bytes(b"sample attachment bytes")
    doc = {"source_filename":source.name, "content_base64":base64.b64encode(source.read_bytes()).decode()}
    storage = SimpleNamespace(build_full_path=lambda owner, name: tmp_path / owner / name)
    runtime = Gateway.uploaded_file.__globals__
    monkeypatch.setitem(runtime, "component_file_access_scopes", lambda _: [scope.name])
    monkeypatch.setitem(runtime, "enforce_local_file_access", lambda path, **_: path)
    monkeypatch.setitem(runtime, "get_storage_service", lambda: storage)
    gateway = SimpleNamespace()
    gateway.uploaded_file = MethodType(Gateway.uploaded_file, gateway)
    assert Gateway.submission(gateway, {**body, "documents":[doc]}, [])["documents"] == [doc]
    assert Gateway.submission(gateway, body, [str(source)])["documents"] == [doc]
    assert Gateway.submission(gateway, {**body, "documents":[{"file_path": str(source)}]}, [])["documents"] == [doc]
    outside = tmp_path / filename
    outside.write_bytes(b"other file")
    with pytest.raises(ValueError, match="当前用户或流程"):
        Gateway.submission(gateway, body, [str(outside)])
