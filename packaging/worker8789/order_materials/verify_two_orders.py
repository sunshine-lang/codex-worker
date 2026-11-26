"""Live acceptance on the 8789 development Flow only. Run AFTER the server and Flow update."""

import argparse
import copy
from datetime import datetime
from getpass import getpass
import json
import os
from pathlib import Path
import time
import uuid

from call_mvp_v2 import Client, FLOW, accept, canonical_sha, deliver, load, prepare, save
from materials import materials_digest, materials_index, parse_materials_json

TERM = "具体实施方式"
FIRST = "Detailed Description of the Embodiments"
SECOND = "Modes for Carrying Out the Invention"


def cases(preprocessed, project):
    refs = preprocessed["documents_markdown"]
    if len(refs) != 4 or refs[3]["markdown_filename"] != "04-HRYY_doc.md":
        raise ValueError("Expected the four named references from the HRYY patent test project.")
    if any(text not in refs[3]["markdown"] for text in (TERM, "附图说明")):
        raise ValueError("Expected the actual section headings in the fourth project file.")
    base = {
        "prompt_file": {"filename": "翻译Prompt.md", "content": preprocessed["translation_prompt"]},
        "reference_files": [{"filename": r["markdown_filename"], "content": r["markdown"]} for r in refs],
    }
    instruction = (
        "本项目本单确认的专利标题译法：单独成行的“" + TERM + "”须原样译为“{}”，保留大小写，不添加标点。\n\n"
    )
    bundles = []
    for target in (FIRST, SECOND):
        current = copy.deepcopy(base)
        current["reference_files"][3]["content"] = instruction.format(target) + refs[3]["markdown"]
        bundles.append(parse_materials_json(json.dumps(current, ensure_ascii=False)))
    before, after = [materials_index(b) for b in bundles]
    if [i for i, (a, b) in enumerate(zip(before, after)) if a != b] != [4]:
        raise ValueError("Only reference number four may differ.")
    context = "该专利说明书中单独成行的章节标题。"

    def row(sid, text):
        return {
            "segment_id": sid,
            "source_language": "zh-CN",
            "target_language": "en-US",
            "source_text": text,
            "target_text": "",
            "context": context,
            "tb_list": [],
            "tm_list": [],
        }

    first = row("136", TERM)
    first["tm_list"] = [
        {
            "source": TERM,
            "target": FIRST,
            "match_rate": 100,
            "enabled": True,
            "context": context,
            "source_language": "zh-CN",
            "target_language": "en-US",
        }
    ]
    return {"project_name": project, "segments": [first, row("122", "附图说明")]}, bundles


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--materials-result", type=Path, help="8787 HRYY result.json, already downloaded")
    cli.add_argument("--out", type=Path, required=True)
    cli.add_argument(
        "--prepare-only", action="store_true", help="Create local test inputs without contacting a service"
    )
    a = cli.parse_args()
    root = a.out.resolve()
    meta = root / "case.json"
    if not meta.exists():
        if not a.materials_result:
            cli.error("New test requires --materials-result")
        project = (
            "HRYY_8789_随单验收_" + datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6]
        )
        order, bundles = cases(load(a.materials_result), project)
        root.mkdir(parents=True, exist_ok=False)
        for name, bundle in zip(("A", "B"), bundles):
            prepare(root / name, order, bundle)
        save(
            meta,
            {
                "flow_id": FLOW,
                "project_name": project,
                "fourth_file": bundles[0]["reference_files"][3]["filename"],
                "source_result_sha256": canonical_sha(load(a.materials_result)),
                "expected": [FIRST, SECOND],
                "test_rule": "Headings from source CSV; A/B each prepend the same one-line test rule to reference four with different approved translations. Original project files are untouched.",
            },
        )
    case = load(meta)
    assert case["flow_id"] == FLOW
    if a.prepare_only:
        print(root)
        return
    key = os.environ.get("LANGFLOW_API_KEY", "").strip() or getpass("Langflow API key: ").strip()
    client = Client(key)
    states = {k: accept(client, root / k) for k in ("A", "B")}
    a_id, b_id = [states[n]["task_id"] for n in ("A", "B")]
    ack = {n: load(root / n / "receipt.json") for n in states}
    if (
        a_id == b_id
        or not ack["A"].get("project_id")
        or ack["B"].get("project_id") != ack["A"]["project_id"]
        or ack["B"].get("previous_project_task_id") != a_id
    ):
        raise RuntimeError("Worker did not bind B after A in the same project.")
    print("A", a_id, "B", b_id, "project", case["project_name"], flush=True)
    pending = set(states)
    final = {}
    waited = False
    deadline = time.monotonic() + 10800
    timeline = root / "timeline.jsonl"
    if timeline.exists():
        waited = any(
            json.loads(l).get("waiting_on_task_id") == a_id for l in timeline.read_text().splitlines()
        )
    while pending and time.monotonic() < deadline:
        for n in ("A", "B"):
            if n not in pending:
                continue
            v = client.control("status", states[n]["task_id"])
            save(root / n / "latest-status.json", v)
            observation = {
                k: v.get(k)
                for k in (
                    "task_id",
                    "status",
                    "stage",
                    "project_id",
                    "codex_thread_id",
                    "waiting_on_task_id",
                    "updated_at",
                )
            }
            observation.update(order=n, observed_at=datetime.now().astimezone().isoformat())
            with timeline.open("a", encoding="utf-8") as f:
                f.write(json.dumps(observation, ensure_ascii=False) + "\n")
            if n == "B" and v.get("waiting_on_task_id") == a_id:
                waited = True
            if v["status"] in ("failed", "stopped"):
                raise RuntimeError(f"{n} {v['status']}: {v.get('error')}")
            if v["status"] == "completed":
                final[n] = deliver(client, root / n, states[n])
                pending.remove(n)
        if pending:
            time.sleep(3)
    if pending:
        raise TimeoutError("Retry with the same --out to continue these orders.")
    targets = [final[n]["result"]["segments"][0]["target_text"] for n in ("A", "B")]
    threads = [final[n].get("codex_thread_id") for n in ("A", "B")]
    reads = [(final[n].get("session_progress") or {}).get("read_reference_files", []) for n in ("A", "B")]
    indices = [materials_index(load(root / n / "materials.json")) for n in ("A", "B")]
    changed = [i for i, (x, y) in enumerate(zip(*indices)) if x != y]
    finalized = final["A"].get("order_completed_at")
    started = (final["B"].get("execution_window") or {}).get("started_at")
    ordered = bool(
        finalized
        and started
        and datetime.fromisoformat(finalized.replace("Z", "+00:00"))
        <= datetime.fromisoformat(started.replace("Z", "+00:00"))
    )
    checks = {
        "same_order_json": load(root / "A" / "request.json") == load(root / "B" / "request.json"),
        "both_orders_have_four_references": all(len(index) == 5 for index in indices),
        "A_final_before_B_started": ordered,
        "only_fourth_reference_changed": changed == [4],
        "same_worker_thread": bool(threads[0]) and threads[0] == threads[1],
        "predecessor_verified": ack["B"]["previous_project_task_id"] == a_id,
        "change_identified_by_worker": final["B"].get("changed_reference_files") == [case["fourth_file"]],
        "exact_translations": targets == [FIRST, SECOND],
        "fourth_reference_read_both_times": all(case["fourth_file"] in files for files in reads),
        "both_self_reviewed": all(final[n].get("self_review_status") == "completed" for n in ("A", "B")),
        "old_100pct_tm_checked_by_agent": all(
            ((final[n].get("tm_decisions") or {}).get("0") or {}).get("actor") == "agent" for n in ("A", "B")
        ),
    }
    report = {
        "flow_id": FLOW,
        "tasks": [a_id, b_id],
        "threads": threads,
        "translations": targets,
        "waiting_observed": waited,
        "A_order_completed_at": finalized,
        "B_job_started_at": started,
        "material_digests": [materials_digest(idx) for idx in indices],
        "checks": checks,
        "passed": all(checks.values()),
    }
    save(root / "acceptance.json", report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if not report["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
