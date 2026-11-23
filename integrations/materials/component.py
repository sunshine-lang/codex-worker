"""Langflow gateway for an asynchronous project-materials Worker."""

import base64
import json
import os
import re
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from lfx.custom import Component
from lfx.io import HandleInput, Output, SecretStrInput
from lfx.schema import Message
from lfx.services.deps import get_storage_service
from lfx.utils.file_path_security import component_file_access_scopes, enforce_local_file_access


# The deployment can pin its Worker here or set MATERIALS_WORKER_URL on Langflow.
DEFAULT_WORKER_URL = ""
MAX_INPUT_BYTES = 32 * 1024 * 1024


class MaterialsGateway(Component):
    display_name = "项目材料 Worker"
    description = "提交原始材料、查询后台任务进度，或取回翻译 Prompt 和逐份 Markdown。"
    name = "MaterialsGateway"
    icon = "Files"
    inputs = [
        HandleInput(name="request", display_name="材料请求", input_types=["Message"], required=True),
        SecretStrInput(name="worker_token", display_name="材料 Worker 令牌", required=False),
    ]
    outputs = [Output(name="response", display_name="任务响应", method="dispatch")]

    def uploaded_file(self, reference):
        if not isinstance(reference, str) or not reference:
            raise ValueError("附件需要 Langflow 已上传文件的路径。")
        candidate = Path(reference)
        if ".." in candidate.parts:
            raise ValueError("附件路径无效。")
        path = candidate if candidate.is_absolute() else Path(self.get_full_path(reference))
        scopes = component_file_access_scopes(self)
        path = Path(enforce_local_file_access(path, scope_ids=scopes)).resolve()
        storage = get_storage_service()
        roots = [Path(storage.build_full_path(str(scope), "material-scope")).resolve().parent for scope in scopes if scope]
        if not any(path.is_relative_to(root) for root in roots) or path.suffix.lower() not in (".doc", ".docx"):
            raise ValueError("请提交当前用户或流程已上传的 Word 原件。")
        return path

    def submission(self, obj, attachments):
        fields = ("project_name", "business_tag", "source_language", "target_language")
        for field in fields:
            if not isinstance(obj.get(field), str) or not obj[field].strip():
                raise ValueError(f"{field} 需要非空文本。")
        if not isinstance(obj.get("translation_requirements", ""), str):
            raise ValueError("translation_requirements 需要项目级文字要求。")
        documents = obj.get("documents", [])
        if not isinstance(documents, list):
            raise ValueError("documents 需要是附件数组。")
        if attachments:
            if documents:
                raise ValueError("请在 documents 或聊天附件中选择一处提交文件。")
            documents = [{"file_path": path} for path in attachments]
        payload = {key: obj[key] for key in fields}
        payload.update(translation_requirements=obj.get("translation_requirements", ""), documents=[])
        size = 0
        for doc in documents:
            if not isinstance(doc, dict) or ("content_base64" in doc) == ("file_path" in doc):
                raise ValueError("每份文档需要 content_base64 或已上传的 file_path。")
            if "file_path" in doc:
                path = self.uploaded_file(doc["file_path"])
                name = doc.get("source_filename", path.name)
                if path.stat().st_size > MAX_INPUT_BYTES - size:
                    raise ValueError("本次附件总量超过 32 MiB。")
                raw = path.read_bytes()
                encoded = base64.b64encode(raw).decode("ascii")
            else:
                encoded = doc["content_base64"]
                name = doc.get("source_filename")
                if not isinstance(encoded, str) or len(encoded) > 4 * ((MAX_INPUT_BYTES - size + 2) // 3):
                    raise ValueError("附件编码无效或超过 32 MiB。")
                try:
                    raw = base64.b64decode(encoded, validate=True)
                except ValueError:
                    raise ValueError("附件不是有效的 Base64。") from None
            if not isinstance(name, str) or Path(name).suffix.lower() not in (".doc", ".docx"):
                raise ValueError("首版附件需要带原文件名的 .doc 或 .docx。")
            if not raw:
                raise ValueError(f"文件为空：{name}")
            size += len(raw)
            if size > MAX_INPUT_BYTES:
                raise ValueError("本次附件总量超过 32 MiB。")
            payload["documents"].append({"source_filename": name, "content_base64": encoded})
        return payload

    async def dispatch(self) -> Message:
        key = None
        try:
            obj = json.loads(self.request.text)
            if not isinstance(obj, dict):
                raise ValueError("请输入一个 JSON 对象。")
            operation = obj.get("operation", "submit")
            if operation == "submit":
                key = str(uuid.UUID(str(obj["submission_key"]))) if "submission_key" in obj else str(uuid.uuid4())
                data = self.submission(obj, getattr(self.request, "files", None))
                data["submission_key"] = key
                method, path = "POST", "/v1/material-tasks"
            elif operation in ("status", "result"):
                task_id = obj.get("task_id")
                if not isinstance(task_id, str) or not re.fullmatch(r"mat_[0-9a-f]{32}", task_id):
                    raise ValueError("需要材料 Worker 返回的 task_id。")
                method, data = "GET", None
                path = "/v1/material-tasks/" + task_id + ("/result" if operation == "result" else "")
            else:
                raise ValueError("operation 支持 submit、status、result。")
            base = (os.environ.get("MATERIALS_WORKER_URL") or DEFAULT_WORKER_URL).rstrip("/")
            secret = self.worker_token
            token = (secret.get_secret_value() if hasattr(secret, "get_secret_value") else str(secret or ""))
            token = token or os.environ.get("MATERIALS_WORKER_TOKEN", "")
            if not token or urlsplit(base).scheme not in ("http", "https") or not urlsplit(base).hostname:
                result = {"error": {"code": "WORKER_NOT_CONFIGURED", "message": "请配置材料 Worker 的地址和专用令牌。"}}
            else:
                async with httpx.AsyncClient(timeout=httpx.Timeout(90, connect=8), follow_redirects=False) as client:
                    options = {"json": data} if method == "POST" else {}
                    answer = await client.request(method, base + path, headers={"Authorization": "Bearer " + token}, **options)
                    try:
                        result = answer.json()
                    except ValueError:
                        result = {"error": {"code": "WORKER_HTTP_ERROR", "message": f"Worker 返回 HTTP {answer.status_code}。"}}
                    if not isinstance(result, dict) or (answer.is_error and "error" not in result):
                        result = {"error": {"code": "WORKER_HTTP_ERROR", "message": f"Worker 返回 HTTP {answer.status_code}。"}}
        except (ValueError, TypeError) as exc:
            result = {"error": {"code": "INVALID_INPUT", "message": str(exc)}}
        except OSError:
            result = {"error": {"code": "FILE_UNAVAILABLE", "message": "无法读取本次上传的 Word 附件。"}}
        except httpx.RequestError:
            result = {"error": {"code": "WORKER_UNAVAILABLE", "message": "未收到材料 Worker 响应；提交时请用相同 submission_key 重试。"}}
        if key:
            result.setdefault("submission_key", key)
        self.status = result.get("status") or (result.get("error") or {}).get("code") or "已取回材料结果"
        return Message(text=json.dumps(result, ensure_ascii=False))
