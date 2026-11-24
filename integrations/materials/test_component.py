"""Gateway configuration tests. Run in a Langflow environment."""
import asyncio
import base64
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
Gateway = runpy.run_path(str(ROOT / "component.py"))["MaterialsGateway"]
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
    if operation == "submit":
        data.update(project_name="fixture", business_tag="IM", source_language="en", target_language="zh")
    gateway = SimpleNamespace(request=SimpleNamespace(text=json.dumps(data)), worker_url=entered,
                              worker_token=token, saved_worker_url=AsyncMock(return_value=saved))
    gateway.submission = MethodType(Gateway.submission, gateway)
    message = asyncio.run(Gateway.dispatch(gateway))
    return json.loads(message.text), calls


@pytest.mark.parametrize(("operation", "method", "path"), [
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
    entry = next(n for n in graph["nodes"] if n["id"] == "ChatInput-Materials")
    assert "xlsx" in entry["data"]["node"]["template"]["files"]["fileTypes"]


def test_xlsx_is_forwarded_verbatim_for_both_upload_paths(tmp_path):
    body = {"project_name":"example", "business_tag":"life sciences", "source_language":"en", "target_language":"zh"}
    source = tmp_path / "feedback.xlsx"
    source.write_bytes(b"sample workbook bytes")
    doc = {"source_filename":source.name, "content_base64":base64.b64encode(source.read_bytes()).decode()}
    gateway = SimpleNamespace(uploaded_file=lambda value: Path(value))
    assert Gateway.submission(gateway, {**body, "documents":[doc]}, [])["documents"] == [doc]
    assert Gateway.submission(gateway, body, [str(source)])["documents"] == [doc]
