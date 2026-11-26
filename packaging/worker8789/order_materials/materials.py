"""The 8789 order's file contract. Shared with the Langflow nodes and the caller."""

import hashlib
import json
import re
import unicodedata

MATERIALS_CONTRACT = "order-materials/1"
MAX_REFERENCES = 128
MAX_PROMPT_BYTES = 1024**2
MAX_FILE_BYTES = 32 * 1024**2
MAX_TOTAL_BYTES = 64 * 1024**2


def material_filename(name):
    if not isinstance(name, str) or not name.lower().endswith(".md") or not name[:-3].strip():
        raise ValueError("资料必须有非空的 Markdown 文件名。")
    if (
        len(name.encode("utf-8")) > 255
        or name.startswith(".")
        or name != name.strip()
        or re.search(r'[<>:"/\\|?*\x00-\x1f\x7f]', name)
        or name.split(".")[0].upper() in {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
        or re.match(r"^(?:com|lpt)[1-9¹²³]\.", name, re.I)
        or name.casefold() in {"agents.md", "agents.override.md"}
    ):
        raise ValueError("请提供安全的 Markdown 文件名，不含路径。")
    return unicodedata.normalize("NFC", name).casefold()


def unique_json_keys(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("资料 JSON 中的字段重复。")
        value[key] = item
    return value


def parse_materials_json(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("新翻译需求必须提供 materials_json 和本单 Prompt。")
    if len(value.encode("utf-8")) > 2 * MAX_TOTAL_BYTES:
        raise ValueError("编码后的资料超过请求限制。")
    data = json.loads(value, object_pairs_hook=unique_json_keys)
    if not isinstance(data, dict) or set(data) != {"prompt_file", "reference_files"}:
        raise ValueError("资料需要 prompt_file 和 reference_files。")
    refs = data["reference_files"]
    if not isinstance(refs, list) or len(refs) > MAX_REFERENCES:
        raise ValueError(f"reference_files 必须是 0 到 {MAX_REFERENCES} 项的数组。")
    names = set()
    total = 0
    for idx, item in enumerate([data["prompt_file"], *refs]):
        if not isinstance(item, dict) or set(item) != {"filename", "content"}:
            raise ValueError("每份文件需要 filename 和 content。")
        name = material_filename(item["filename"])
        if name in names:
            raise ValueError("资料文件名重复：" + item["filename"])
        names.add(name)
        content = item["content"]
        if not isinstance(content, str) or not content.lstrip("\ufeff").strip():
            raise ValueError("文件正文为空：" + item["filename"])
        size = len(content.encode("utf-8"))
        total += size
        if size > (MAX_PROMPT_BYTES if idx == 0 else MAX_FILE_BYTES) or total > MAX_TOTAL_BYTES:
            raise ValueError("资料超过字节限制：" + item["filename"])
    return data


def materials_index(materials):
    index = []
    for i, item in enumerate([materials["prompt_file"], *materials["reference_files"]]):
        raw = item["content"].encode("utf-8")
        index.append(
            {
                "role": "prompt_file" if i == 0 else "reference_files",
                "filename": item["filename"],
                "bytes": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(),
            }
        )
    return index


def materials_digest(index):
    raw = json.dumps(index, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()
