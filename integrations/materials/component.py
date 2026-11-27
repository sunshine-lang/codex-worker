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
from lfx.io import HandleInput, Output, SecretStrInput, StrInput
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
        StrInput(name="worker_url", display_name="Worker 调用地址", value="", required=False,
                 info="填写并保存 Worker 基础地址，例如 http://<主机>:8787。三个操作共用此地址，留空使用服务端配置。"),
        SecretStrInput(name="worker_token", display_name="材料 Worker 令牌", required=False),
    ]
    outputs = [Output(name="response", display_name="任务响应", method="dispatch")]

    async def saved_worker_url(self):
        # Only an address saved by the flow editor may receive its stored bearer token.
        if not getattr(self.graph, "flow_id", None):
            return ""
        from langflow.services.database.models.flow.model import Flow
        from lfx.services.deps import session_scope_readonly
        from sqlmodel import select

        flow_id = uuid.UUID(str(self.graph.flow_id))
        async with session_scope_readonly() as session:
            graph = (await session.exec(select(Flow.data).where(Flow.id == flow_id))).first()
        for node in (graph or {}).get("nodes", []):
            if node.get("id") == self._vertex.id:
                field = node["data"]["node"]["template"].get("worker_url", {})
                return (field.get("value") or "").strip().rstrip("/")
        raise ValueError("请先将材料节点保存到画布。")

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
        if not any(path.is_relative_to(root) for root in roots) or not path.is_file():
            raise ValueError("请提交当前用户或流程已上传的文件。")
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
            if not isinstance(name, str) or not name.strip():
                raise ValueError("每份附件需要原文件名。")
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
            if operation in ("submit", "organize"):
                key = str(uuid.UUID(str(obj["submission_key"]))) if "submission_key" in obj else str(uuid.uuid4())
                data = self.submission(obj, getattr(self.request, "files", None))
                data["submission_key"] = key
                method, path = "POST", "/v1/material-tasks"
                if operation == "organize":
                    path += "/organize"
            elif operation in ("status", "result"):
                task_id = obj.get("task_id")
                if not isinstance(task_id, str) or not re.fullmatch(r"mat_[0-9a-f]{32}", task_id):
                    raise ValueError("需要材料 Worker 返回的 task_id。")
                method, data = "GET", None
                path = "/v1/material-tasks/" + task_id + ("/result" if operation == "result" else "")
            else:
                raise ValueError("operation 支持 submit、organize、status、result。")
            entered = (getattr(self, "worker_url", "") or "").strip().rstrip("/")
            base = entered or (os.environ.get("MATERIALS_WORKER_URL") or DEFAULT_WORKER_URL).strip().rstrip("/")
            secret = self.worker_token
            token = (secret.get_secret_value() if hasattr(secret, "get_secret_value") else str(secret or ""))
            token = token or os.environ.get("MATERIALS_WORKER_TOKEN", "")
            if base:
                parsed = urlsplit(base)
                if (parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.port == 0
                        or parsed.username is not None or parsed.query or parsed.fragment
                        or any(ch.isspace() or ch == "\\" for ch in base)):
                    raise ValueError("Worker 调用地址需要是 HTTP/HTTPS 基础地址，可包含反向代理路径。")
                if entered != await self.saved_worker_url():
                    raise ValueError("请在画布保存 Worker 调用地址后再运行。")
            if not base:
                result = {"error": {"code": "WORKER_NOT_CONFIGURED", "message": "请填写 Worker 调用地址或配置 MATERIALS_WORKER_URL。"}}
            elif not token:
                result = {"error": {"code": "WORKER_NOT_CONFIGURED", "message": "请填写材料 Worker 令牌或配置 MATERIALS_WORKER_TOKEN。"}}
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
        except httpx.InvalidURL:
            result = {"error": {"code": "INVALID_INPUT", "message": "Worker 调用地址格式无效，请在画布中检查地址。"}}
        except (ValueError, TypeError) as exc:
            result = {"error": {"code": "INVALID_INPUT", "message": str(exc)}}
        except OSError:
            result = {"error": {"code": "FILE_UNAVAILABLE", "message": "无法读取本次上传的材料附件。"}}
        except httpx.RequestError:
            result = {"error": {"code": "WORKER_UNAVAILABLE", "message": "未收到材料 Worker 响应；提交时请用相同 submission_key 重试。"}}
        if key:
            result.setdefault("submission_key", key)
        self.status = result.get("status") or (result.get("error") or {}).get("code") or "已取回材料结果"
        return Message(text=json.dumps(result, ensure_ascii=False))
