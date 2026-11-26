from io import BytesIO
import json
import uuid

import pytest

from call_mvp_v2 import Client, FLOW, URL, accept, load, prepare, save
from materials import MATERIALS_CONTRACT, materials_digest, materials_index
from test_orders import materials


def test_lost_receipt_reuses_saved_materials_and_submission_key(tmp_path):
    order = {"project_name": "client-order", "segments": [{"segment_id": "1"}]}
    docs = materials()
    folder = tmp_path / "delivery"
    prepare(folder, order, docs)
    calls = []

    class Stub:
        def call(self, business, *, submission_key, materials):
            calls.append((business, submission_key, materials))
            if len(calls) == 1:
                raise TimeoutError("lost response")
            return {
                "task_id": "task_" + "2" * 32,
                "status": "queued",
                "project_name": order["project_name"],
                "materials_sha256": materials_digest(materials_index(materials)),
                "materials_contract": MATERIALS_CONTRACT,
            }

    client = Stub()
    with pytest.raises(TimeoutError):
        accept(client, folder)
    original_key = load(folder / "state.json")["submission_key"]
    corrupt = materials("changed")
    save(folder / "materials.json", corrupt)
    with pytest.raises(ValueError, match="changed"):
        accept(client, folder)
    save(folder / "materials.json", docs)
    state = accept(client, folder)
    assert calls[0] == calls[1] and state["submission_key"] == original_key
    assert accept(client, folder) == state and len(calls) == 2


def test_client_posts_one_dev_advanced_request_with_materials():
    class Response(BytesIO):
        headers = {}

    tid = "task_" + "3" * 32

    class Recorder:
        def __init__(self):
            self.calls = []

        def open(self, request, timeout):
            assert request.full_url == URL and FLOW in URL
            body = json.loads(request.data)
            self.calls.append(body)
            out = body["outputs"][0]
            packet = {
                "outputs": [
                    {
                        "outputs": [
                            {
                                "component_id": out,
                                "results": {
                                    "message": {"text": json.dumps({"task_id": tid, "status": "queued"})}
                                },
                            }
                        ]
                    }
                ]
            }
            return Response(json.dumps(packet).encode())

    client = Client("test-only")
    rec = Recorder()
    client.opener = rec
    document = {"project_name": "p", "segments": []}
    m = materials()
    key = str(uuid.uuid4())
    client.call(document, submission_key=key, materials=m)
    client.control("status", tid)
    sent, query = rec.calls
    assert json.loads(sent["inputs"][0]["input_value"]) == document and sent["outputs"] == [
        "ChatOutput-MVPResult"
    ]
    entry = sent["tweaks"]["CustomComponent-MVPInputs"]
    assert entry["submission_key"] == key and json.loads(entry["materials_json"]) == m
    assert query["outputs"] == ["ChatOutput-MVPStatus"] and "CustomComponent-MVPInputs" not in query["tweaks"]


def test_read_old_accepted_8789_directory(tmp_path):
    import hashlib
    from call_mvp_v2 import canonical_sha, deliver

    task = "task_" + "a" * 32
    old = {"project_name": "old", "segments": [{"segment_id": "1", "target_text": ""}]}
    local = {
        "flow_id": FLOW,
        "task_id": task,
        "idempotency_key": str(uuid.uuid4()),
        "input_sha256": hashlib.sha256(
            json.dumps(old, ensure_ascii=False, sort_keys=True, allow_nan=False).encode()
        ).hexdigest(),
    }
    save(tmp_path / "request.json", old)
    save(tmp_path / "state.json", local)
    result = {"project_name": "old", "segments": [{"segment_id": "1", "target_text": "定稿"}]}

    class Historical:
        def control(self, op, tid):
            assert op == "result" and tid == task
            return {
                "task_id": task,
                "status": "completed",
                "result_available": True,
                "worker_result_hash_verified": True,
                "business_result_restored": True,
                "result": result,
                "business_result_sha256": canonical_sha(result),
            }

    client = Historical()
    state = accept(client, tmp_path)
    deliver(client, tmp_path, state)
    assert load(tmp_path / "result.json") == result
