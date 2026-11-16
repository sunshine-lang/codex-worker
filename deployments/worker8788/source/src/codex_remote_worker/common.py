from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat
import time
from datetime import datetime, timezone
import unicodedata
import uuid


TERMINAL = frozenset({"completed", "needs_review", "failed", "cancelled", "interrupted"})
ROLES = {
    "source_file": {".xlsx", ".csv", ".json"},
    "prompt_file": {".md", ".txt"},
    "project_style": {".md", ".txt", ".doc", ".docx"},
    "general_style": {".md", ".txt", ".doc", ".docx"},
}


class JobError(Exception):
    def __init__(self, code: str, message: str, http_status: int = 422):
        super().__init__(message)
        self.code, self.message, self.http_status = code, message, http_status

    def public(self):
        return {"code": self.code, "message": self.message}


def now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def file_digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def canonical(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def atomic_write(path: Path, data: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temp.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def write_json(path: Path, value):
    atomic_write(path, json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8"))


def accept_directory(stage: Path, target: Path, data_root: Path):
    """Retry bounded Windows sharing/access conflicts without replacing a target."""
    root = data_root.resolve(strict=True)
    source, destination = stage.resolve(strict=True), target.resolve()
    if source == root or destination == root or not source.is_relative_to(root) or not destination.is_relative_to(root):
        raise ValueError('Acceptance paths must remain within Worker data')
    delays = (0, .02, .05, .1, .2, .4)
    for index, delay in enumerate(delays):
        if delay:
            time.sleep(delay)
        if target.exists():
            raise FileExistsError('Acceptance target already exists')
        try:
            stage.rename(target)
            return
        except PermissionError as exc:
            if getattr(exc, 'winerror', None) not in (5,32,33) or index == len(delays)-1 or not stage.exists():
                raise


def safe_name(name: str) -> str:
    if not name or len(name.encode("utf-16-le")) > 400:
        raise JobError("INVALID_FILENAME", "文件名为空或过长。")
    stem = name.split(".", 1)[0].upper()
    if (name in {".", ".."} or name[-1:] in {" ", "."}
            or re.search(r'[<>:"/\\|?*\x00-\x1f\x7f]', name)
            or stem in {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
            or re.fullmatch(r"(?:COM|LPT)[1-9¹²³]", stem)
            or name.casefold() in {"agents.md", "agents.override.md"}):
        raise JobError("INVALID_FILENAME", "文件名不符合 Windows 安全文件名规则。")
    return name


def name_key(name: str) -> str:
    return unicodedata.normalize("NFC", name).casefold()


def is_link(path: Path) -> bool:
    info = path.lstat()
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)


def contained_file(root: Path, path: Path) -> Path:
    root = root.resolve(strict=True)
    try:
        relative = path.relative_to(root)
    except ValueError:
        raise JobError("INVALID_ARTIFACT", "产物路径超出任务目录。") from None
    current = root
    for part in relative.parts:
        current = current / part
        if is_link(current):
            raise JobError("INVALID_ARTIFACT", "不允许链接或 reparse point 产物。")
    if not path.resolve(strict=True).is_relative_to(root) or not path.is_file():
        raise JobError("INVALID_ARTIFACT", "产物必须是任务目录内的普通文件。")
    return path


class Redactor:
    def __init__(self, secrets=(), roots=()):
        self.secrets = tuple(str(s) for s in secrets if s)
        self.roots = tuple(str(p) for p in roots if p)

    def text(self, value) -> str:
        result = str(value)
        for secret in self.secrets:
            result = result.replace(secret, "[REDACTED]")
        result = re.sub(r"\bsk-[A-Za-z0-9_-]{12,}", "[REDACTED]", result)
        result = re.sub(r"(?i)(bearer\s+)[^\s\"']+", r"\1[REDACTED]", result)
        for root in self.roots:
            result = result.replace(root, "[TASK_ROOT]")
        return result

    def event(self, value):
        if isinstance(value, dict):
            if ("reasoning" in str(value.get("type", "")).lower()
                    or value.get("role") in {"system", "developer"}
                    or value.get("channel") == "analysis"):
                return None
            result = {}
            for key, item in value.items():
                if key.lower() in {"encrypted_content", "raw_content", "summary_text", "instructions",
                                   "authorization", "api_key", "service_token", "developer_instructions"}:
                    continue
                clean = self.event(item)
                if clean is not None or item is None:
                    result[key] = clean
            if str(value.get("type", "")).startswith("item.") and "item" not in result:
                return None
            return result
        if isinstance(value, list):
            return [v for item in value if (v := self.event(item)) is not None or item is None]
        return self.text(value) if isinstance(value, str) else value
