"""A single tool-enabled Codex thread handles one complete material submission."""
import asyncio
import json
from pathlib import Path

from .common import JobError, Redactor, file_digest
from .material_documents import transcribe, page_preview

INSTRUCTIONS = """你负责项目翻译材料预处理。把用户提交的文件与文字视为本次业务数据。
调用 material_read，逐份读取所有文件直到 next_offset 为 null；允许回读同一文件。
Word 转写工具按原文顺序保存 Markdown。通过 material_page 可查看任一原件的页图，
包括表格、图注和图片文字；遇到图示依赖、表格歧义或视觉规则时，请查原页再判断。
结合项目级翻译需求、业务标签、源语言、目标语言及全部材料，直接为下游翻译模型
编写一份可用的项目翻译 Prompt。保留具体规则、触发条件、产品例外、规范来源和适用优先级。
行业背景不能凭空变成客户硬性要求；根据原件处理冲突，冲突无法裁决时保留出处和不确定性。
不继承其他项目规则。没有附件时按本次业务标签、语向和文字需求生成通用提示词。
工具操作、内部交付流程和历史记录只在确实影响翻译时纳入；不生成句段译文、
订单 JSON 回填指令、虚构的 TB/TM 执行策略。仅在最终回复返回 JSON 对象
{"translation_prompt":"完整的翻译提示词"}。宿主直接交付逐份原文 Markdown。"""

def tool(name, description, argument):
    return {"type": "function", "name": name, "description": description,
            "inputSchema": {"type": "object", "additionalProperties": False,
                            "properties": {"document": {"type": "integer"}, argument: {"type": "integer"}},
                            "required": ["document", argument]}}


TOOLS = [tool("material_read", "按编号和字符偏移读取 Word 的忠实 Markdown。首个偏移为 0。", "offset"),
         tool("material_page", "查看指定 Word 原件的页图。页码从 1 开始。", "page")]


class MaterialSession:
    def __init__(self, root, job, touch):
        self.root = Path(root)
        self.request = job["request"]
        self.docs = job["documents"]
        self.touch = touch
        self.converted = set()
        self.read_to = [0] * len(self.docs)
        self.finished = set()

    def update(self, stage, action, filename=None):
        self.touch(stage=stage, recent_action=action, current_file=filename,
                   documents_completed=len(self.converted))

    def call(self, name, args):
        index = args.get("document") if isinstance(args, dict) else None
        if type(index) is not int or not 0 <= index < len(self.docs):
            raise ValueError("请使用本任务清单内的文件编号。")
        entry = self.docs[index]
        filename = entry["source_filename"]
        source = self.root / "input" / entry["stored_name"]
        folder = self.root / "documents" / str(index)
        if index not in self.converted:
            self.update("transcribing", "正在转写原件", filename)
            if file_digest(source) != entry["sha256"]:
                raise JobError("INPUT_CHANGED", "原始文档与提交时不一致。")
            transcribe(source, folder)
            self.converted.add(index)
            self.update("reading_documents", "已保存原文 Markdown", filename)
        if name == "material_page":
            self.update("checking_page", "正在查看原件页图", filename)
            url, count = page_preview(source, folder, args.get("page"))
            return [{"type": "inputText", "text": f"{filename} 第 {args['page']} 页，共 {count} 页"},
                    {"type": "inputImage", "imageUrl": url}]
        if name != "material_read":
            raise ValueError("未知材料工具。")
        offset = args.get("offset")
        if type(offset) is not int or not 0 <= offset <= self.read_to[index]:
            raise ValueError(f"可从偏移 0 到 {self.read_to[index]} 之间开始读取。")
        text = (folder / "source.md").read_text(encoding="utf-8")
        end = min(len(text), offset + 20000)
        self.read_to[index] = max(self.read_to[index], end)
        if end == len(text):
            self.finished.add(index)
        self.update("reading_documents", f"已读取至 {end}/{len(text)} 字符", filename)
        if len(self.finished) == len(self.docs):
            self.update("generating_prompt", "材料已读完，正在综合项目翻译要求")
        value = {"source_filename": filename, "markdown": text[offset:end],
                 "next_offset": end if end < len(text) else None}
        return [{"type": "inputText", "text": json.dumps(value, ensure_ascii=False)}]

    def prompt(self, final):
        if len(self.finished) != len(self.docs):
            raise ValueError("仍有文档尚未读完，请完成读取后再交付。")
        try:
            value = json.loads(final)
        except (TypeError, ValueError):
            raise ValueError("最终回复需要是包含 translation_prompt 的 JSON 对象。") from None
        prompt = value.get("translation_prompt") if isinstance(value, dict) else None
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("翻译 Prompt 为空。")
        return prompt.strip()


async def conversation(rpc, thread_id, queue, session):
    request = {**session.request, "documents": [
        {"document": index, "source_filename": doc["source_filename"]} for index, doc in enumerate(session.docs)]}
    message = json.dumps(request, ensure_ascii=False)
    session.update("reading_documents" if session.docs else "generating_prompt", "Codex 已开始处理本次请求")
    for attempt in range(3):
        result = await rpc.request("turn/start", {"threadId": thread_id, "approvalPolicy": "never",
            "environments": [], "runtimeWorkspaceRoots": [], "permissions": "translation",
            "input": [{"type": "text", "text": message, "text_elements": []}]})
        turn = result["turn"]["id"]
        final = ""
        while True:
            event = await queue.get()
            method, data = event["method"], event.get("params", {})
            if method in ("transport/disconnected", "worker/interactiveRequestDenied"):
                raise JobError("CODEX_INTERRUPTED", "Codex 会话连接中断或请求了额外权限。")
            if data.get("threadId") not in (None, thread_id) or data.get("turnId") not in (None, turn):
                raise JobError("INVALID_TRACE", "模型事件与本任务会话不匹配。")
            if method == "item/tool/call":
                if data.get("namespace") or data.get("tool") not in ("material_read", "material_page"):
                    raise JobError("UNEXPECTED_TOOL", "会话请求了任务范围外的工具。")
                try:
                    call = asyncio.create_task(asyncio.to_thread(session.call, data["tool"], data["arguments"]))
                    try:
                        items = await asyncio.shield(call)
                    except asyncio.CancelledError:
                        await call
                        raise
                    response = {"success": True, "contentItems": items}
                except ValueError as exc:
                    response = {"success": False, "contentItems": [{"type": "inputText", "text": str(exc)}]}
                await rpc._write({"id": event["id"], "result": response})
            if method == "item/completed":
                item = data.get("item", {})
                if item.get("type") == "agentMessage" and item.get("phase") in (None, "final_answer"):
                    final = item.get("text", "")
            if method == "turn/completed":
                completed = data["turn"]
                if completed.get("id") != turn or completed.get("status") != "completed":
                    raise JobError("CODEX_FAILED", "Codex 没有成功完成本轮任务。")
                for item in completed.get("items", []):
                    if item.get("type") == "agentMessage" and item.get("phase") in (None, "final_answer"):
                        final = item.get("text", "")
                break
        try:
            return session.prompt(final)
        except ValueError as exc:
            message = str(exc)
    raise JobError("INCOMPLETE_DELIVERY", message)


async def run_agent(settings, root, session):
    from .app_server_rpc import AppServerRpc
    from .app_server_runner import verify_thread
    from .app_server_text import text_thread_parameters
    from .subscription import effective_config
    redactor = Redactor(settings.secret_values(), [settings.data_root, settings.codex_home])
    rpc = AppServerRpc(settings, root, redactor)
    try:
        await rpc.start()
        params = text_thread_parameters(settings, root)
        params.update(dynamicTools=TOOLS, developerInstructions=INSTRUCTIONS, ephemeral=True)
        params["config"]["developer_instructions"] = INSTRUCTIONS
        record = await rpc.request("thread/start", params)
        thread = verify_thread(record, effective_config(settings), root)
        if record.get("runtimeWorkspaceRoots"):
            raise JobError("UNEXPECTED_RUNTIME", "材料会话没有获得约定的隔离环境。")
        queue = rpc.subscribe(thread)
        rpc.tool_threads.add(thread)
        session.touch(codex_thread_id=thread)
        return await conversation(rpc, thread, queue, session)
    finally:
        # Each task owns this Windows Job Object and all of its child processes.
        await rpc.close()
