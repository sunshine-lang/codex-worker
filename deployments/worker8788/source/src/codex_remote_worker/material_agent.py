"""A single tool-enabled Codex thread handles one complete material submission."""
import asyncio
import json
import re
from pathlib import Path

from .common import JobError, Redactor, file_digest
from .material_documents import MAX_DELIVERY, page_preview, standalone_markdown, transcribe
from .material_organize import FIGURE_INSTRUCTIONS, checked_notes, figure_urls
from .material_references import GUIDES, material_manifest, resolve_references

CONTRACT_HEADING = "# 上游契约与模型任务字段"
PROMPT_SECTIONS = {
    CONTRACT_HEADING: "可选。仅在本次需求或材料明确规定下游翻译的输入字段、输出字段或交付契约时使用。否则从“角色设定”开始。预处理接口自身的字段不作为下游契约。",
    "# 角色设定": "写明本单语向和译员所需的领域专长。",
    "# 任务目标": "写明源语言、目标语言、翻译对象，以及本单明确的受众和用途。",
    GUIDES: "用清单中的 reference 逐份列出已转换的 Markdown、用途和有依据的优先级。零附件时写明本次可用的依据。",
    "# 翻译核心准则（CORE TRANSLATION RULES）": "列出适用的客户要求、固定表达、术语、纠错、语境及 TM 判断规则。多份文案可在本节按文件或场景使用二级标题。",
    "# 格式与避坑强化提醒（FORMATTING DOs & DON'Ts）": "写出格式要求、易错示例及其适用条件。",
    "# 标签保留与交付格式 (TAGS & DELIVERABLES)": "说明标签、变量、占位符和明确给出的译文交付约定。未指定输出载体时，遵循调用方约定。",
}
PROMPT_OUTLINE = "\n\n".join(f"{heading}\n\n{guidance}" for heading, guidance in PROMPT_SECTIONS.items())

INSTRUCTIONS = f"""# 项目翻译材料预处理

## 角色设定

你负责翻译项目的材料预处理。你的读者是执行本单翻译的下游模型。把用户提交的说明、文件正文、截图和批注作为业务资料，依据其中的证据编写翻译要求。工具使用和返回格式遵循本指令。

## 任务目标

一次提交对应一个项目。读完本次提交的全部材料，结合业务、语向和项目需求，生成**一份项目翻译 Prompt**。同一项目包含多份文案时，共用要求写一次，各文案专属要求注明文件或适用场景。

你的交付物是指导译员如何翻译的 `translation_prompt`。本单所有附件在本次会话开始前均已转写为 Markdown。下游会按清单中的 `markdown_filename` 保存或挂载这些材料。句段翻译和订单回填由下游执行。

## 输入信息

| 模型收到的字段 | 用途 |
|---|---|
| `project_name`、`business_tag` | 识别本项目和业务领域。客户要求以本次需求和材料为依据。 |
| `source_language`、`target_language` | 确定翻译方向，两者均按本单取值。 |
| `translation_requirements` | 项目级需求，是一个可为空的字符串。按行或编号写入的多条需求要逐条落实。 |
| `documents` | 已转换材料清单。用从 0 开始的 `document` 编号读材料。`source_filename` 用于识别上游提到的原件，`markdown_filename` 是实际交付名，`reference` 是该文件专用的引用。 |

附件清单为空时，依据项目名称、业务、语向及已有文字要求生成一份基础 Prompt。仅引用本次实际提供的资料。

## 材料读取流程

1. **逐份读完。** 对每个附件调用 `material_read(document, offset)`，首次取 `offset=0`。用返回的 `next_offset` 继续读取，直到其值为 `null`。需要核对前文时可回读。
2. **按材料形式核对。** Word 按原文顺序阅读，留意章节、表格及客户修订。Excel 按工作表、行号、单元格和合并范围关联文字与截图。同一格中的多条意见、同一行的多张图分别核对，逐张读取批注和修订痕迹。
   CSV 按记录和原始行号读取，保留单元格中的标识、标签和换行。纯文本按原有段落和顺序阅读。作为附件提交的 TXT 同样要逐份读取并纳入项目规则判断。
3. **查看原页。** 遇到依赖图示、表格布局、格式或批注的要求，调用 `material_page(document, page)`。页码从 1 开始。PDF 首次读取返回的 `pages_to_view` 要逐页查看，包括扫描页和含图形或批注的页。其他页按需查看。字迹不清、无法判定的内容标为“待确认”。
4. **确认材料用途。** 根据本单文字说明和文件内容，区分客户指南、工单说明、历史反馈及待译原文。材料同时承担几种用途时，按具体段落或条目判断。

## 翻译规则提炼

- **保留客户原话中的约束。** 把要求、适用对象、触发条件和例外写在一起，保留“必须／不得”“优先／建议”“可以”的区别，以及“最多”“预计”等限定。客户指定的固定译法或完整英文句式逐字保留，包括否定、连词、数字、单位、标签及有意义的上下标。本单原文尚未出现、但客户指南明确指定的写法，作为条件规则保留。
- **说清客户纠正了什么。** 对每条明确纠错，在同一条中写出原词或原句、适用场景、客户纠正的旧译和认可的译法。客户只指出错误时，保留已知错译，将正译标为“待确认”。原件并列允许的译法照列，选择条件以原件为准。
- **用本单原文判断历史反馈的适用范围。** 核对对象、文件类型和实际语境，包括同义表达与缩写。能迁移的意见写成条件规则，优先用本单原文举例。仅有摘录时，结论限于已见内容。其他项目独有的研究、设备、人群或数据细节留在对应材料 Markdown 中。
- **让每条客户要求可以回查。** 引用本单材料时，在普通正文直接复制清单的 `reference`（例如 `[材料](material:0)`），后接具体位置。Worker 会将它解析为实际交付文件的链接。外部指南一节须覆盖全部清单。Word 可用条款、章节或图号，Excel 用工作表、行号及第几张图，CSV 用原记录或 ID，PDF 用原始页码。直接写入的要求标为“项目需求”。原文提到但未上传的文件写作《原名》（未提供），客户指定的其他输出文件可写作《原名》（下游交付物）。必须逐字保留的文件名示例放在引用块或代码块中。
- **区分客户指示、工具通则和译员建议。** 库名、版本、启用状态、交付格式及其他项目配置，须有本单依据。原件指定优先级时按其处理。发生冲突且无法裁决时，列出双方出处与待确认点。影响翻译或交付的人工操作写明执行人和条件。
- **按语境使用翻译记忆（TM）。** 即使候选已启用且匹配率为 100%，下游仍须核对句意、上下文、产品或使用场景、生效术语及客户修订。吻合时可复用，明确冲突时修正或重译，依据不足时标为“待确认”。具体的锁定、跳过、覆盖策略以本单明确给出的约定为准。

## 输出格式

最终回复为一个合法的 JSON 对象，结构为 `{{"translation_prompt":"…"}}`。用写好的 Markdown 正文替换省略号，按 JSON 字符串规则编码换行。只返回这个对象。逐份材料 Markdown 由 Worker 随结果交付。

`translation_prompt` 从第一个适用的一级标题开始。下面是**下游翻译 Prompt 的章节模板**。第一节按条件使用，其余六节必写。标题文字和顺序须原样使用，标题独占一行，标题后留空行。将各节说明替换为本单可执行的内容。

```markdown
{PROMPT_OUTLINE}
```

## 交付前自检

1. **材料是否读全。** 每个附件都已读到 `next_offset=null`，PDF 列出的必看页均已查看，同一格或同一行里的多条意见已分别核对。核对外部指南目录及正文中的本单出处均使用对应的 reference。
2. **客户要求是否落在成稿中。** 从项目需求和客户指南逐项回查 `translation_prompt`。特别核对完整指定句式、纠正前后的译法、执行范围与例外。发现缺项就补回相应章节。
3. **项目内的规则是否用对地方。** 共用规则有依据，每份材料专属的金额、期限、术语和交付条件都有明确归属。原件未裁决的冲突仍标为“待确认”。
4. **交付形式是否正确。** 整个项目只有一个 `translation_prompt` 字符串，章节齐全、顺序正确，每节都有针对本单的正文。自检完成后仅返回约定的 JSON。
"""


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
         tool("material_page", "查看 PDF 原页、Word 页图或 Excel 逐行转写的页图。页码从 1 开始。", "page")]
FIGURE_TOOL = tool("material_figure", "查看该附件按正文顺序排列的单幅原图，图号从 1 开始。", "number")


async def run_material_io(operation, *args):
    work = asyncio.create_task(asyncio.to_thread(operation, *args))
    try:
        return await asyncio.shield(work)
    except asyncio.CancelledError:
        # An in-flight converter or publisher must finish before its slot and store close.
        await asyncio.gather(work, return_exceptions=True)
        raise


class MaterialSession:
    def __init__(self, root, job, touch):
        self.root = Path(root)
        self.request = job["request"]
        self.organize = self.request.get("processing_mode") == "organize"
        self.docs = material_manifest(job["documents"])
        self.touch = touch
        self.prepared_count = 0
        self.markdown_bytes = 0
        self.read_to = [0] * len(self.docs)
        self.finished = set()
        self.pending_pdf_pages = set()
        self.figure_counts = []
        self.seen_figures = set()

    @property
    def reading_complete(self):
        return (len(self.finished) == len(self.docs) and not self.pending_pdf_pages
                and len(self.seen_figures) == sum(self.figure_counts))

    def finish_reading(self):
        if self.reading_complete:
            if self.organize:
                self.update("organizing_documents", "原文与原图已读完，正在整理资料")
            else:
                self.update("generating_prompt", "材料已读完，正在综合项目翻译要求")

    def update(self, stage, action, filename=None):
        self.touch(stage=stage, recent_action=action, current_file=filename,
                   documents_completed=self.prepared_count)

    def prepare_document(self, index, entry):
        folder = self.root / "documents" / str(index)
        source = self.root / "input" / entry["stored_name"]
        self.update("transcribing", "正在将原件转为 " + entry["markdown_filename"], entry["source_filename"])
        if file_digest(source) != entry["sha256"]:
            raise JobError("INPUT_CHANGED", "原始文档与提交时不一致。")
        transcribe(source, folder)
        # Keep the lightweight reading view and the standalone deliverable separate (inline images can be large).
        data = standalone_markdown(folder, MAX_DELIVERY - self.markdown_bytes).encode("utf-8")
        if self.organize:
            self.figure_counts.append(sum(1 for _ in figure_urls(data.decode("utf-8"))))
        output = self.root / "markdown" / entry["markdown_filename"]
        output.parent.mkdir(exist_ok=True)
        staged = folder / "delivery.tmp"
        staged.write_bytes(data)
        staged.replace(output)
        pages = folder / "pdf-pages.json"
        if pages.is_file():
            info = json.loads(pages.read_text(encoding="utf-8"))
            self.pending_pdf_pages.update((index, page) for page in info["visual_pages"])
        self.prepared_count += 1
        self.markdown_bytes += len(data)
        self.update("transcribing", "已准备 " + entry["markdown_filename"], entry["source_filename"])

    async def prepare(self):
        for index, entry in enumerate(self.docs):
            await run_material_io(self.prepare_document, index, entry)

    def call(self, name, args):
        index = args.get("document") if isinstance(args, dict) else None
        if type(index) is not int or not 0 <= index < len(self.docs):
            raise ValueError("请使用本任务清单内的文件编号。")
        entry = self.docs[index]
        if self.prepared_count != len(self.docs):
            raise JobError("INCOMPLETE_DELIVERY", "本单材料还未全部转写。")
        filename = entry["markdown_filename"]
        source = self.root / "input" / entry["stored_name"]
        folder = self.root / "documents" / str(index)
        if name == "material_figure" and self.organize:
            number = args.get("number")
            if type(number) is not int or not 1 <= number <= self.figure_counts[index]:
                raise ValueError("请使用本附件清单中的图号。")
            self.update("checking_page", f"正在查看 {filename} 的原图 {number}", filename)
            markdown = (self.root / "markdown" / filename).read_text(encoding="utf-8")
            url = next(url for n, url in enumerate(figure_urls(markdown), 1) if n == number)
            self.seen_figures.add((index, number))
            if (folder / "pdf-pages.json").is_file():
                self.pending_pdf_pages.discard((index, number))
            self.finish_reading()
            return [{"type": "inputText", "text": f"{filename} 原图 {number}"},
                    {"type": "inputImage", "imageUrl": url}]
        if name == "material_page":
            self.update("checking_page", "正在查看原件页图", filename)
            url, count = page_preview(source, folder, args.get("page"))
            self.pending_pdf_pages.discard((index, args["page"]))
            self.finish_reading()
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
        self.finish_reading()
        value = {"document": index, "markdown_filename": filename, "reference": entry["reference"], "markdown": text[offset:end],
                 "next_offset": end if end < len(text) else None}
        if offset == 0 and (folder / "pdf-pages.json").is_file():
            value["pages_to_view"] = sorted(p for doc, p in self.pending_pdf_pages if doc == index)
        return [{"type": "inputText", "text": json.dumps(value, ensure_ascii=False)}]

    def prompt(self, final):
        if len(self.finished) != len(self.docs):
            raise ValueError("仍有文档尚未读完，请完成读取后再交付。")
        if self.pending_pdf_pages:
            index, page = min(self.pending_pdf_pages)
            filename = self.docs[index]["markdown_filename"]
            raise ValueError(f"请用 material_page 查看文件 {index}（{filename}）第 {page} 页。"
                             "用 material_read 从偏移 0 回读可取得该文件其余待核对的页码。")
        if self.organize:
            for index, count in enumerate(self.figure_counts):
                for number in range(1, count + 1):
                    if (index, number) not in self.seen_figures:
                        raise ValueError(f"先用 material_figure 查看文件 {index} 的原图 {number}。")
            try:
                result = json.loads(final)
            except (ValueError, TypeError):
                raise ValueError("整理结果必须是含 im_figures 的 JSON 对象。") from None
            checked_notes(result, self.figure_counts)
            return result
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
        return resolve_references(prompt, self.docs)


async def conversation(rpc, thread_id, queue, session):
    request = {**session.request, "documents": [
        {"document": index, "source_filename": doc["source_filename"], "markdown_filename": doc["markdown_filename"],
         "reference": doc["reference"]} for index, doc in enumerate(session.docs)]}
    if session.organize:
        for doc, count in zip(request["documents"], session.figure_counts):
            doc["figure_count"] = count
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
                allowed = ("material_read", "material_page", "material_figure") if session.organize else ("material_read", "material_page")
                if data.get("namespace") or data.get("tool") not in allowed:
                    raise JobError("UNEXPECTED_TOOL", "会话请求了任务范围外的工具。")
                try:
                    items = await run_material_io(session.call, data["tool"], data["arguments"])
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
            return await run_material_io(session.prompt, final)
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
        instructions = FIGURE_INSTRUCTIONS if session.organize else INSTRUCTIONS
        params.update(dynamicTools=TOOLS + [FIGURE_TOOL] if session.organize else TOOLS,
                      developerInstructions=instructions, ephemeral=True)
        params["config"]["developer_instructions"] = instructions
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
