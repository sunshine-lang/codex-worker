"""8789 task-local named Markdown, for the translation task API and project_read."""

import codecs
import hashlib
import os
from pathlib import Path

from starlette.datastructures import UploadFile
from .build_8789 import BASELINE_COMMIT as BASELINE_COMMIT
from .common import JobError, ROLES, contained_file
from .materials import (
    MATERIALS_CONTRACT,
    MAX_REFERENCES,
    MAX_PROMPT_BYTES,
    MAX_FILE_BYTES,
    MAX_TOTAL_BYTES,
    material_filename,
    materials_digest,
)


def upload_parts(form):
    fields = list(form.multi_items())
    if (
        set(k for k, _ in fields) - {"source_file", "prompt_file", "reference_files", "options"}
        or any(len(form.getlist(k)) != 1 for k in ("source_file", "prompt_file", "options"))
        or not isinstance(form.get("options"), str)
    ):
        raise JobError("INVALID_INPUT", "需要订单 JSON、一份 Prompt、零到多份 reference_files 和 options。")
    references = form.getlist("reference_files")
    if len(references) > MAX_REFERENCES or any(
        not isinstance(v, UploadFile) for k, v in fields if k != "options"
    ):
        raise JobError("INVALID_MATERIALS", "资料数量超限或上传对象无效。")
    return [
        ("source_file", form["source_file"]),
        ("prompt_file", form["prompt_file"]),
        *((f"ref_{i:04d}", f) for i, f in enumerate(references)),
    ]


def task_uploads(port, form, existing):
    # A known pre-upgrade submission key can recover its original receipt. The
    # task store still compares the original fingerprint before it returns it.
    legacy = set(form) == {*ROLES, "options"}
    old_key = existing is not None and not order_has_materials(existing.get("inputs", {}))
    if legacy and (port != 8789 or old_key):
        if (
            any(len(form.getlist(k)) != 1 for k in form)
            or not isinstance(form["options"], str)
            or any(not isinstance(form[k], UploadFile) for k in ROLES)
        ):
            raise JobError("INVALID_INPUT", "旧订单重试的资料结构无效。")
        return None
    return upload_parts(form)


async def receive_order_files(settings, parts, stage):
    manifest, names, total = {}, set(), 0
    for role, part in parts:
        name = part.filename or ""
        if role == "source_file":
            if Path(name).suffix.lower() != ".json":
                raise JobError("INVALID_TASK_INPUT", "source_file 必须是 JSON。")
            filename = "source_file.json"
        else:
            try:
                normalized = material_filename(name)
            except (ValueError, UnicodeError) as exc:
                raise JobError("INVALID_FILENAME", str(exc)) from exc
            if normalized in names:
                raise JobError("DUPLICATE_FILENAME", "本单资料重名：" + name)
            names.add(normalized)
            filename = role + ".md"
        limit = min(MAX_FILE_BYTES, settings.max_file_bytes)
        if role == "prompt_file":
            limit = min(limit, settings.max_prompt_bytes, MAX_PROMPT_BYTES)
        sha, size, nonblank = hashlib.sha256(), 0, False
        decoder = codecs.getincrementaldecoder("utf-8-sig")() if role != "source_file" else None
        path = stage / "input" / filename
        try:
            with path.open("xb") as output:
                while data := await part.read(65536):
                    total += len(data)
                    size += len(data)
                    if size > limit or total > min(settings.max_upload_bytes, MAX_TOTAL_BYTES):
                        raise JobError("UPLOAD_TOO_LARGE", "订单及资料超过字节限制。", 413)
                    if decoder is not None:
                        text = decoder.decode(data)
                        nonblank = nonblank or bool(text.strip())
                    sha.update(data)
                    output.write(data)
                if decoder is not None:
                    text = decoder.decode(b"", final=True)
                    nonblank = nonblank or bool(text.strip())
                output.flush()
                os.fsync(output.fileno())
        except UnicodeError as exc:
            raise JobError("INVALID_MATERIALS", "资料必须是 UTF-8 文本：" + name) from exc
        if not size or (decoder is not None and not nonblank):
            raise JobError("EMPTY_FILE", "订单或资料为空：" + name)
        entry = {"filename": filename, "bytes": size, "sha256": sha.hexdigest()}
        if decoder is not None:
            entry["display_name"] = name
        manifest[role] = entry
    return manifest


def order_has_materials(manifest):
    return "display_name" in manifest.get("prompt_file", {})


def guide_entries(manifest):
    if order_has_materials(manifest):
        return [(k, manifest[k]) for k in sorted(manifest) if k.startswith("ref_")]
    return [(k, manifest[k]) for k in ("project_style", "general_style")]


def index_for(manifest):
    entries = [("prompt_file", manifest["prompt_file"]), *guide_entries(manifest)]
    return [
        {
            "role": "prompt_file" if k == "prompt_file" else "reference_files",
            "filename": v["display_name"],
            "bytes": v["bytes"],
            "sha256": v["sha256"],
        }
        for k, v in entries
    ]


def reference_changes(current, previous):
    refs = {v["display_name"]: v["sha256"] for _, v in guide_entries(current)}
    old = (
        {v["display_name"]: v["sha256"] for _, v in guide_entries(previous)}
        if order_has_materials(previous)
        else {}
    )
    return {
        "changed_reference_files": [n for n, digest in refs.items() if old.get(n) != digest],
        "removed_reference_files": [n for n in old if n not in refs],
    }


def material_fields(task):
    manifest = task.get("inputs") or {}
    if not manifest.get("prompt_file", {}).get("display_name"):
        return {}
    index = index_for(manifest)
    return {
        "materials_contract": MATERIALS_CONTRACT,
        "materials_sha256": materials_digest(index),
        "material_files": index,
        **(task.get("material_changes") or reference_changes(manifest, {})),
    }


def guides_opened(session):
    reference_names = {entry["display_name"] for _, entry in guide_entries(session.task["inputs"])}
    return {
        call["query"]
        for call in session.calls
        if call.get("operation") == "guides"
        and call.get("query") in reference_names
        and call.get("char_offset", 0) == 0
        and call.get("activity", {}).get("sha256")
    }


def read_guide(session, args):
    """Look up by exact manifest name; never interpret a model-chosen filename as a path."""
    manifest = session.task["inputs"]
    query = args["query"]
    items = [manifest["prompt_file"], *(v for _, v in guide_entries(manifest))]
    entry = next((v for v in items if v["display_name"] == query), None)
    if entry is None:
        raise ValueError("Unknown guide. Use a filename from overview.material_files.")
    # A replacement job appends a revision notice to its execution copy of the prompt.
    # The order manifest describes the original uploaded files.
    root = session.settings.data_root / "tasks" / session.task["task_id"] / "input"
    path = contained_file(root.resolve(), root / entry["filename"])
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != entry["sha256"]:
        raise JobError("INPUT_CHANGED", "本单资料内容校验失败。")
    # Page the original Markdown before building the tool envelope. Image data URIs
    # may be very long; never put multi-megabyte guides into an App Server message.
    text = raw.decode("utf-8-sig")
    start = args["char_offset"]
    if start > len(text):
        raise ValueError("Guide offset exceeds its length.")
    end = min(len(text), start + min(args["char_limit"] or 12000, 32000))
    more = end < len(text)
    return {
        "format": "markdown_text",
        "filename": query,
        "sha256": entry["sha256"],
        "content": text[start:end],
        "char_offset": start,
        "has_more": more,
        "next_char_offset": end if more else None,
        "next_start": None,
        "next_read": {**args, "char_offset": end} if more else None,
    }
