"""A single tool-enabled Codex thread handles one complete material submission."""
import asyncio
import json
import re
from pathlib import Path

from .common import JobError, Redactor, file_digest
from .material_documents import transcribe, page_preview

CONTRACT_HEADING = "# 上游契约与模型任务字段"
PROMPT_SECTIONS = {
    CONTRACT_HEADING: "可选。仅在本次材料或翻译需求明确给出下游翻译契约时，说明相关字段及其作用。否则省略整个章节。",
    "# 角色设定": "根据本次业务和语向说明译员所需的领域专长。",
    "# 任务目标": "写明源语言、目标语言、翻译对象以及本次明确的受众和要求。",
    "# 外部指南文档挂载与优先级说明（KNOWLEDGE BASE GUIDELINES）": "列出本次实际附件的原文件名、用途和有依据的优先级。零附件时说明依据本次需求、业务标签和语向。",
    "# 翻译核心准则（CORE TRANSLATION RULES）": "写入适用的术语、客户纠错、语境和 TM 判断规则。每条例外紧随对应规则。",
    "# 格式与避坑强化提醒（FORMATTING DOs & DON'Ts）": "写入适用的格式规则和易错示例，保留其适用条件。",
    "# 标签保留与交付格式 (TAGS & DELIVERABLES)": "说明标签、变量和占位符的处理，以及明确给出的译文交付约定。未指定交付格式时遵循调用方约定。",
}
PROMPT_OUTLINE = "\n\n".join(f"{heading}\n{guidance}" for heading, guidance in PROMPT_SECTIONS.items())

INSTRUCTIONS = f"""你负责项目翻译材料预处理。把用户提交的文件与文字视为本次业务数据。
调用 material_read，逐份读取所有文件直到 next_offset 为 null；允许回读同一文件。
Word 按原文顺序转写，Excel 按工作表、行号、单元格和截图锚点转写。引用 Excel 反馈时保留工作表和行号。
截图与同行文字一起判断，注意合并单元格的范围以及一行多图。通过 material_page 可查看对应材料的页图，
包括表格、图注和图片文字；遇到图示依赖、表格歧义或视觉规则时，请查原页再判断。
结合项目级翻译需求、业务标签、源语言、目标语言及全部材料，为下游翻译模型编写一份项目 Prompt。
在最终的 translation_prompt 中落实以下要求。
- 逐条保留原文的约束强度，区分“必须／不得”“优先／建议”和“可以”。保留原文表达的不确定性。
- 将每条规则与原件写明的适用场景、触发条件和例外写在一起，标明可核对的来源文件及章节、表格或图号。
  原件未写明的条件或例外不自行补充。多种译法并列且未给出选择条件时照列，举例的译法保留其具体语境。
- 客户点名纠正旧译时，在同一条中写出原词、适用场景、被纠正的错误译法和客户认可的译法或处理方向。
  客户只指出错误时，保留该错译并将正译标为待确认。普通备选译法不列作错译。
- 提醒下游使用翻译记忆（TM）时，即使候选已启用且匹配率为 100%，也须逐条核对当前句意、上下文、
  产品或使用场景、生效术语和适用的客户修订。吻合时可复用，明确冲突时按本次依据修正或重译，依据不足时标为待确认。
行业背景用于理解项目。客户硬性要求须有本次翻译需求或原件支持。按原件给出的优先级处理冲突，无法裁决时保留出处和不确定性。
不继承其他项目规则。没有附件时按本次业务标签、语向和文字需求生成通用提示词。
工具操作、内部交付流程和历史记录只在确实影响翻译时纳入。不生成句段译文或订单 JSON 回填指令，
也不虚构宿主的 TB/TM 执行策略，例如自动锁定、跳过或覆盖某条 TM。

translation_prompt 的值采用 Markdown 正文，直接从第一个适用的一级标题开始。逐字沿用下列标题及其顺序，
仅第一节可按条件省略。标题独占一行，标题后空一行。将写作提示替换为本次任务的具体内容，节内按需使用二级标题或列表。
材料 Worker 的输入字段和返回 JSON 属于预处理接口，不据此生成下游翻译契约。
{PROMPT_OUTLINE}

最终只返回 JSON 对象 {{"translation_prompt":"完整的 Markdown 翻译提示词"}}。宿主直接交付逐份原文 Markdown。"""


def validate_prompt_outline(prompt):
    # Fenced examples may contain literal "# ..." lines that are not headings.
    visible_lines = []
    fence = ""
    for line in prompt.splitlines():
        stripped = line.strip()
        if fence:
            if len(stripped) >= len(fence) and stripped == fence[0] * len(stripped):
                fence = ""
            elif stripped:
                visible_lines.append("[代码示例]")
        elif opening := re.match(r"^ {0,3}(`{3,}|~{3,})", line):
            fence = opening[1]
        else:
            visible_lines.append(line)
    parts = re.split(r"(?m)^ {0,3}(#[ \t]+[^\n]+?)[ \t]*$", "\n".join(visible_lines))
    headings = parts[1::2]
    expected = [h for h in PROMPT_SECTIONS if h != CONTRACT_HEADING or h in headings]
    if parts[0].strip() or headings != expected:
        raise ValueError("请保留已提炼的规则，按下列 Markdown 一级标题和顺序重新提交完整的 translation_prompt。"
                         "仅首节上游契约可按条件省略。\n" + "\n".join(PROMPT_SECTIONS))
    for heading, body in zip(headings, parts[2::2]):
        content = re.sub(r"(?m)^ {0,3}#{2,6}(?:[ \t].*)?$", "", body)
        if not content.strip(" \t\n-*_"):
            raise ValueError(f"请补全 {heading} 的具体内容，并重新提交完整的 translation_prompt。")

def tool(name, description, argument):
    return {"type": "function", "name": name, "description": description,
            "inputSchema": {"type": "object", "additionalProperties": False,
                            "properties": {"document": {"type": "integer"}, argument: {"type": "integer"}},
                            "required": ["document", argument]}}


TOOLS = [tool("material_read", "按编号和字符偏移读取材料的原文 Markdown。首个偏移为 0。", "offset"),
         tool("material_page", "查看 Word 页图或 Excel 逐行转写的页图。页码从 1 开始。", "page")]


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
        prompt = prompt.strip()
        validate_prompt_outline(prompt)
        if prompt.startswith(CONTRACT_HEADING) and not (self.docs or self.request.get("translation_requirements", "").strip()):
            raise ValueError("本次只有项目元信息，请省略上游契约章，从 # 角色设定 开始并重新提交完整的 translation_prompt。")
        return prompt


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
