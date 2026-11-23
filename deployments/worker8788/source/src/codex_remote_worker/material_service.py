"""Independent material task API. Uses the dev Worker's durable FIFO and Codex transport."""
import argparse
import asyncio
import base64
from contextlib import asynccontextmanager
import hmac
import json
import logging
from pathlib import Path
import re
import tempfile
import uuid

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse

from .common import JobError, canonical, digest, now, safe_name, write_json
from .material_agent import MaterialSession, run_agent
from .material_documents import MAX_DELIVERY, standalone_markdown
from .store import InstanceLock, Store

LOG = logging.getLogger(__name__)
MAX_INPUT = 32 * 1024 * 1024
MAX_BODY = (MAX_INPUT * 4 // 3) + 1024 * 1024
TASK_ID = re.compile(r"mat_[0-9a-f]{32}\Z")
FIELDS = ("project_name", "business_tag", "source_language", "target_language")


def public(job):
    fields = ("project_name", "status", "stage", "documents_completed", "documents_total",
              "current_file", "recent_action", "execution_slot", "created_at", "updated_at", "error")
    return {"task_id": job["job_id"], "submission_key": job["request_key"],
            **{field: job[field] for field in fields if field in job}}


def accept(store, root, obj, capacity):
    if not isinstance(obj, dict):
        raise JobError("INVALID_INPUT", "请求需要是 JSON 对象。")
    request = {}
    for field in FIELDS:
        value = obj.get(field)
        if not isinstance(value, str) or not value.strip():
            raise JobError("INVALID_INPUT", f"{field} 需要非空文本。")
        request[field] = value.strip()
    request["translation_requirements"] = obj.get("translation_requirements", "")
    if not isinstance(request["translation_requirements"], str):
        raise JobError("INVALID_INPUT", "translation_requirements 需要文本。")
    try:
        key = str(uuid.UUID(str(obj["submission_key"]))) if "submission_key" in obj else str(uuid.uuid4())
    except ValueError as exc:
        raise JobError("INVALID_INPUT", "submission_key 需要是 UUID。") from exc
    docs = obj.get("documents", [])
    if not isinstance(docs, list):
        raise JobError("INVALID_INPUT", "documents 需要是附件数组。")
    task = "mat_" + uuid.uuid4().hex
    manifest = []
    total = 0
    with tempfile.TemporaryDirectory(dir=root / "staging") as temp:
        stage = Path(temp) / task
        (stage / "input").mkdir(parents=True)
        for index, obj_doc in enumerate(docs):
            if not isinstance(obj_doc, dict) or not isinstance(obj_doc.get("source_filename"), str):
                raise JobError("INVALID_DOCUMENT", "每份附件需要 source_filename。")
            name = safe_name(obj_doc["source_filename"])
            suffix = Path(name).suffix.lower()
            if suffix not in (".doc", ".docx"):
                raise JobError("UNSUPPORTED_DOCUMENT", f"首版仅支持 Word 原件：{name}")
            encoded = obj_doc.get("content_base64")
            if not isinstance(encoded, str) or len(encoded) > 4 * ((MAX_INPUT - total + 2) // 3):
                raise JobError("INVALID_DOCUMENT", f"附件编码无效或过大：{name}")
            try:
                raw = base64.b64decode(encoded, validate=True)
            except ValueError as exc:
                raise JobError("INVALID_DOCUMENT", f"附件不是有效的 Base64：{name}") from exc
            total += len(raw)
            if not raw or total > MAX_INPUT:
                raise JobError("INVALID_DOCUMENT", f"附件为空或总量超过 32 MiB：{name}")
            stored = f"{index:04d}{suffix}"
            (stage / "input" / stored).write_bytes(raw)
            manifest.append({"source_filename": name, "stored_name": stored, "sha256": digest(raw)})
        timestamp = now()
        record = {"job_id": task, "request_key": key, "fingerprint": digest(canonical([request, manifest])),
                  "request": request, "project_name": request["project_name"], "documents": manifest,
                  "status": "queued", "stage": "queued", "documents_completed": 0, "documents_total": len(docs),
                  "created_at": timestamp, "updated_at": timestamp, "recent_action": "已收妥本次项目材料"}
        return store.accept(record, stage, root / "tasks" / task, capacity)


def publish_result(folder, job, prompt):
    remaining = MAX_DELIVERY - len(prompt.encode("utf-8"))
    docs = []
    for index, doc in enumerate(job["documents"]):
        markdown = standalone_markdown(folder / "documents" / str(index), remaining)
        docs.append({"source_filename": doc["source_filename"], "markdown": markdown})
        remaining -= len(markdown.encode("utf-8"))
    write_json(folder / "result.json", {"translation_prompt": prompt, "documents_markdown": docs})


def create_material_app(settings, agent=run_agent):
    root = Path(settings.data_root)
    slots = settings.max_concurrent_jobs

    async def run_slot(store, slot, wake):
        while True:
            wake.clear()
            job = store.claim(max_active=slots)
            if not job:
                await wake.wait()
                continue
            task = job["job_id"]
            folder = root / "tasks" / task
            def touch(**values):
                store.update(task, expected={"running"}, **values)
            session = MaterialSession(folder, job, touch)
            try:
                touch(execution_slot=slot, recent_action="已取得执行通道")
                async with asyncio.timeout(7200):
                    prompt = await agent(settings, folder, session)
                if not isinstance(prompt, str) or not prompt.strip() or len(session.finished) != len(job["documents"]):
                    raise JobError("INCOMPLETE_DELIVERY", "Prompt 或逐份原文核对尚未完成。")
                session.update("validating_result", "正在核对并发布本次产物")
                publication = asyncio.create_task(asyncio.to_thread(publish_result, folder, job, prompt))
                try:
                    await asyncio.shield(publication)
                except asyncio.CancelledError:
                    await publication
                    raise
                touch(status="completed", stage="completed", recent_action="交付文件已保存", current_file=None)
            except asyncio.CancelledError:
                # Startup reconciles an atomically published result or marks this task interrupted.
                raise
            except Exception as exc:
                if isinstance(exc, JobError):
                    error = exc.public()
                elif isinstance(exc, TimeoutError):
                    error = {"code": "TIMEOUT", "message": "材料任务超过执行时限。"}
                else:
                    LOG.exception("Material task %s failed", task)
                    error = {"code": "EXECUTION_FAILED", "message": "材料处理失败，请按任务号查询服务日志。"}
                touch(status="failed", stage="failed", error=error, recent_action="任务失败")
            finally:
                wake.set()

    @asynccontextmanager
    async def lifespan(app):
        root.mkdir(parents=True, exist_ok=True)
        lock = InstanceLock(root / ".materials.lock")
        try:
            for sub in ("tasks", "staging"):
                (root / sub).mkdir(exist_ok=True)
            store = Store(root / "materials.sqlite")
            try:
                for job in store.unfinished():
                    task = job["job_id"]
                    if (root / "tasks" / task / "result.json").is_file():
                        store.update(task, status="completed", stage="completed", current_file=None, recent_action="已恢复交付文件")
                    else:
                        store.update(task, status="interrupted", stage="interrupted", recent_action="Worker 在运行中中断",
                                     error={"code": "INTERRUPTED", "message": "已保留原任务记录；重新处理请使用新提交键。"})
                wake = asyncio.Event()
                app.state.store, app.state.wake = store, wake
                workers = [asyncio.create_task(run_slot(store, i + 1, wake)) for i in range(slots)]
                try:
                    yield
                finally:
                    for worker in workers:
                        worker.cancel()
                    await asyncio.gather(*workers, return_exceptions=True)
            finally:
                store.close()
        finally:
            lock.close()

    app = FastAPI(title="Project materials Worker", lifespan=lifespan, docs_url=None, redoc_url=None)

    @app.exception_handler(JobError)
    async def job_error(_request, exc):
        return JSONResponse({"error": exc.public()}, status_code=exc.http_status)

    @app.middleware("http")
    async def authenticate(request, call_next):
        expected = "Bearer " + settings.service_token.get_secret_value()
        if not hmac.compare_digest(request.headers.get("Authorization", "").encode(), expected.encode()):
            return JSONResponse({"error": {"code": "UNAUTHORIZED", "message": "材料 Worker 令牌无效。"}}, status_code=401)
        return await call_next(request)

    @app.get("/v1/ready")
    async def ready():
        return {"ready": True, "service": "materials", "execution_slots": slots,
                "execution_slot_unit": "whole_submission", "document_types": ["doc", "docx"]}

    @app.post("/v1/material-tasks")
    async def submit(request: Request):
        raw = bytearray()
        async for block in request.stream():
            if len(raw) + len(block) > MAX_BODY:
                raise JobError("LIMIT_EXCEEDED", "请求超过接收大小。", 413)
            raw.extend(block)
        try:
            obj = json.loads(raw)
        except (ValueError, UnicodeError) as exc:
            raise JobError("INVALID_INPUT", "请求需要 UTF-8 JSON。") from exc
        job, replay = await asyncio.to_thread(accept, app.state.store, root, obj, settings.queue_capacity)
        app.state.wake.set()
        return JSONResponse(public(job), status_code=200 if replay else 202)

    def lookup(task_id):
        if not TASK_ID.fullmatch(task_id):
            raise JobError("NOT_FOUND", "任务编号无效。", 404)
        return app.state.store.get(task_id)

    @app.get("/v1/material-tasks/{task_id}")
    async def status(task_id: str):
        return public(lookup(task_id))

    @app.get("/v1/material-tasks/{task_id}/result")
    async def result(task_id: str):
        job = lookup(task_id)
        if job["status"] != "completed":
            value = public(job)
            value.setdefault("error", {"code": "RESULT_NOT_READY", "message": "当前任务尚无可交付结果。"})
            return JSONResponse(value, status_code=409)
        return FileResponse(root / "tasks" / task_id / "result.json", media_type="application/json")
    return app


def main():
    import uvicorn
    from .config import load_settings
    from .material_documents import office_executable, pandoc_executable
    parser = argparse.ArgumentParser(description="Start the independent materials Worker")
    parser.add_argument("--config", required=True, type=Path)
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding="utf-8-sig"))
    root = Path(config["data_root"])
    settings = load_settings(args.config, root / "private/credentials.local.json")
    source = args.config.parent / "model-instance.path"
    if source.is_file():
        from .app_server_service import trial_settings
        existing = trial_settings(Path(source.read_text(encoding="utf-8-sig").strip()))
        if existing.codex_home != settings.codex_home:
            raise RuntimeError("Configured model instance and materials worker refer to different Codex homes")
        settings = settings.model_copy(update={"provider_env": {**existing.provider_env, **settings.provider_env}})
    if not settings.codex_executable.is_file():
        raise RuntimeError("Codex executable in the Worker configuration does not exist")
    if settings.authentication_mode in ("chatgpt", "codex_auto"):
        from .subscription import require_configured_login
        require_configured_login(settings)
    office_executable()
    pandoc_executable()
    uvicorn.run(create_material_app(settings), host=settings.bind_host, port=settings.port,
                access_log=False, workers=1)


if __name__ == "__main__":
    main()
