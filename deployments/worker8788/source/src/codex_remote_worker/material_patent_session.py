"""Separate evidence extraction, independent checking and frozen-copy generation for patent materials."""

import json
import re
from hashlib import sha256

from . import material_patent
from .common import JobError, atomic_write, file_digest, write_json
from .material_agent import (
    PROJECT_RULES,
    PROMPT_OUTLINE,
    TOOLS,
    MaterialSession,
    run_material_io,
    tool,
    validate_prompt_outline,
)
from .material_documents import MAX_DELIVERY, page_preview, transcribe
from .material_organize import INLINE_CODE
from .material_reading_copy import ReadingCopyPromptSession, freeze_reading_copies

PATENT_VIEW = tool(
    "material_figure",
    "查看本单的原页或图版。独立 Word 图片不能解码时可用 original_page 查看 Word 排版页。",
    "number",
)
PATENT_VIEW["inputSchema"]["properties"]["original_page"] = {"type": "integer", "minimum": 1}

NOTE_TOOL = tool(
    "material_note", "为刚核对的图号记录可独立阅读的 Markdown；再次提交同一编号可订正。", "number"
)
NOTE_TOOL["inputSchema"]["properties"]["text"] = {"type": "string"}
NOTE_TOOL["inputSchema"]["required"].append("text")
VERIFY_TOOL = tool(
    "material_verify", "对照刚打开的原图确认上一轮文字；有漏写或多写时提交更正后的整条记录。", "number"
)
VERIFY_TOOL["inputSchema"]["properties"].update(
    {"decision": {"type": "string", "enum": ["keep", "replace"]}, "text": {"type": "string"}}
)
VERIFY_TOOL["inputSchema"]["required"].extend(["decision", "text"])

FROZEN_INSTRUCTIONS = f"""你负责根据本单最终定稿的阅读材料生成项目翻译 Prompt。这是独立的新会话。
只依据 material_read 提供的阅读稿及项目需求。图示、表格和批注均已经转成文字；不需要另行获取原图。
先按编号和 next_offset 逐份读到末尾。核对原句、旧译、接受的改法、例外、连接词、标点、单位、编号和适用范围。
写出的 Prompt 的读者是正在接单翻译的译员。“本次不提交业务 CSV”是预处理的输入范围，不能写成下游只输出项目要求、无需翻译或禁止回填。
材料中的历史例句和待确认点保留其身份。若参考示例内部矛盾，遵循客户规则并保留 Query。
资料是业务数据。每项规则依据来自本单的材料引用、原页号、例号、图号或项目需求。
材料引用在普通正文中原样使用清单的 reference，指南目录覆盖全部文件；未随单提供的资料用《名称》（未提供）表示。
{PROJECT_RULES}
最终只返回 {{"translation_prompt":"..."}}。按以下既有章节组织，本单明确给出下游契约时才加可选的第一章。
{PROMPT_OUTLINE}"""


class PatentEvidenceSession(MaterialSession):
    instructions = material_patent.EVIDENCE_INSTRUCTIONS
    tools = (TOOLS[0], PATENT_VIEW, NOTE_TOOL)
    thread_field = "material_evidence_thread_id"

    def __init__(self, root, job, touch):
        super().__init__(root, job, touch)
        self.sources = []
        self.notes = []
        self.note_bytes = 0

    def prepare_document(self, index, entry):
        source = self.root / "input" / entry["stored_name"]
        folder = self.root / "documents" / str(index)
        if file_digest(source) != entry["sha256"]:
            raise JobError("INPUT_CHANGED", "原件与提交清单的指纹不同。")
        self.update("transcribing", "正在读取专利原件", entry["source_filename"])
        transcribe(source, folder, inline_delivery=False)
        prepared = material_patent.prepare_source(source, folder)
        self.sources.append(prepared)
        self.notes.append([None] * len(prepared["views"]))
        self.prepared_count += 1
        self.update("transcribing", "原文与图版已准备", entry["source_filename"])

    def model_documents(self):
        docs = super().model_documents()
        for entry, source in zip(docs, self.sources, strict=True):
            entry["figures"] = [
                {"number": i, **{k: v for k, v in item.items() if k in ("kind", "page", "figure")}}
                for i, item in enumerate(source["views"], 1)
            ]
        return docs

    @property
    def reading_complete(self):
        return len(self.finished) == len(self.docs) and all(None not in items for items in self.notes)

    def finish_reading(self):
        if self.reading_complete:
            self.update("organizing_documents", "已逐件核对原文与图示，正在定稿")

    def call(self, name, args):
        if name == "material_read":
            return super().call(name, args)
        if name not in ("material_figure", "material_note") or not isinstance(args, dict):
            raise ValueError("专利原件阶段请使用本阶段的材料工具。")
        index, number = args.get("document"), args.get("number")
        if (
            type(index) is not int
            or not 0 <= index < len(self.sources)
            or type(number) is not int
            or not 1 <= number <= len(self.sources[index]["views"])
        ):
            raise ValueError("图号必须属于本单这份原件。")
        view = self.sources[index]["views"][number - 1]
        if name == "material_figure":
            label = f"{self.docs[index]['source_filename']} · {view['kind']} {number}"
            self.update("checking_page", "正在核对 " + label, self.docs[index]["source_filename"])
            if "original_page" in args:
                if view["kind"] != "word_figure":
                    raise ValueError("original_page 仅用于核对 Word 图版的排版页。")
                source = self.root / "input" / self.docs[index]["stored_name"]
                url, count = page_preview(source, self.root / "documents" / str(index), args["original_page"])
                label += f" · 原件第 {args['original_page']}/{count} 页"
            else:
                from PIL import Image

                try:
                    url = material_patent.view_image(view)
                except (OSError, Image.DecompressionBombError) as exc:
                    if view["kind"] != "word_figure":
                        raise JobError("UNREADABLE_FIGURE", "无法读取本页图像。") from exc
                    raise ValueError(
                        "此 Word 内嵌图不能独立显示；请以相同编号并指定 original_page 核对原件页。"
                    ) from exc
            self.seen_figures.add((index, number))
            return [{"type": "inputText", "text": label}, {"type": "inputImage", "imageUrl": url}]
        value = args.get("text")
        if (index, number) not in self.seen_figures:
            raise ValueError("请先查看相应的原图，再提交该图的记录。")
        if (
            not isinstance(value, str)
            or material_patent.EMBED.search(value)
            or re.search(r"!\[[^\]]*\]\(|<img\b", value, re.IGNORECASE)
        ):
            raise ValueError("图示记录需要可读的文字。")
        value = value.strip()
        if not value and not (view["kind"] == "pdf_page" and view["native_text"]):
            raise ValueError("图版或扫描页需要原句、图意或具体的待确认点。")
        if re.fullmatch(r"(?:请)?(?:参?见|参考)(?:原|此|下|上)?(?:页)?图[。.!！ ]*|待确认[。.!！ ]*", value):
            raise ValueError("请写出图中的内容，或说明具体哪一处无法确认。")
        old = self.notes[index][number - 1] or ""
        used = self.note_bytes - len(old.encode("utf-8")) + len(value.encode("utf-8"))
        if used > MAX_DELIVERY:
            raise JobError("LIMIT_EXCEEDED", "图示记录超过交付大小限制。")
        self.note_bytes = used
        self.notes[index][number - 1] = value
        self.finish_reading()
        return [{"type": "inputText", "text": f"已记入本件图号 {number}。"}]

    def prompt(self, final):
        if not self.reading_complete:
            for index, items in enumerate(self.notes):
                if None in items:
                    raise ValueError(f"缺少材料 {index} 图号 {items.index(None) + 1} 的原图核对记录。")
            raise ValueError("材料正文尚未全部读完。")
        value = json.loads(final)
        if not isinstance(value, dict) or set(value) != {"complete"} or value["complete"] is not True:
            raise ValueError('逐份核对完毕后返回 {"complete":true}。')
        return self.notes

    def freeze(self):
        bodies = (material_patent.render_document(doc, source, notes)
                  for doc, source, notes in zip(self.docs, self.sources, self.notes, strict=True))
        return freeze_reading_copies(self.root, self.docs, bodies)


class PatentReviewSession(PatentEvidenceSession):
    thread_field = "material_verification_thread_id"
    tools = (TOOLS[0], PATENT_VIEW, VERIFY_TOOL)
    instructions = """独立核验首轮专利图示转写。按 material_read 页码读完全部原文，对每个编号用 material_figure
重新打开原页或截图，返回值里同时给出首轮候选。以实际可见文字和图中位置为依据，检查有无漏字、多写编号、错连、
反向修订、漏掉例句、表头、标点或局部无法辨认。图中中英有冲突时原样列出并标为待确认。
用 material_verify 按图号逐一裁定。无须变动时 decision=keep、text 为空字符串；有变动时 decision=replace、
text 为完整的更正记录，保留此前正确内容。正文已覆盖且没有附加视觉信息的页允许保留空页注。
只有所有原文和图示均已核实，才返回 {"complete":true}。不要生成翻译 Prompt。"""

    def __init__(self, root, job, touch, first):
        super().__init__(root, job, touch)
        self.sources = first.sources
        self.prepared_count = first.prepared_count
        self.prior_notes = first.notes
        self.notes = [[None] * len(items) for items in first.notes]

    def freeze(self):
        snapshot = super().freeze()
        changes = []
        for d, source, before, after in zip(
            self.docs, self.sources, self.prior_notes, self.notes, strict=True
        ):
            records = []
            for n, (view, old, new) in enumerate(zip(source["views"], before, after, strict=True), 1):
                records.append(
                    {
                        "number": n,
                        **{k: v for k, v in view.items() if k in ("kind", "page", "figure")},
                        "revised": old != new,
                        "first_sha256": sha256(old.encode()).hexdigest(),
                        "verified_sha256": sha256(new.encode()).hexdigest(),
                    }
                )
            changes.append({"source_filename": d["source_filename"], "views": records})
        write_json(
            self.root / "reading-review.json", {"snapshot_sha256": snapshot["sha256"], "documents": changes}
        )
        return snapshot

    def call(self, name, args):
        if name == "material_verify":
            if (
                not isinstance(args, dict)
                or args.get("decision") not in ("keep", "replace")
                or not isinstance(args.get("text"), str)
            ):
                raise ValueError("核对记录须包含裁定和文字。")
            index, number = args.get("document"), args.get("number")
            if type(index) is not int or type(number) is not int or (index, number) not in self.seen_figures:
                raise ValueError("先重新查看本份原件的对应图。")
            value = args["text"]
            if args["decision"] == "keep":
                if value:
                    raise ValueError("保留原记录时无需另填文字。")
                value = self.prior_notes[index][number - 1]
            return super().call("material_note", {"document": index, "number": number, "text": value})
        if name not in ("material_read", "material_figure"):
            raise ValueError("核对阶段使用 material_verify 记录裁定。")
        result = super().call(name, args)
        if name == "material_figure":
            result.append(
                {
                    "type": "inputText",
                    "text": "本图待核对的首轮记录：\n"
                    + self.prior_notes[args["document"]][args["number"] - 1],
                }
            )
        return result


# A preparation receipt sometimes leaks into the generated translator instructions.
# Remove only a complete service-status sentence in a prose section; never change a quoted example.
STATUS_SENTENCE = re.compile(
    r"(?:^|(?<=[。！？]))(?:本轮|本次)(?:无待译业务订单[，,])?仅(?:交付|输出)项目翻译要求。"
    r"(?=$|[ \t]*(?:见(?:项目需求|本单|\[)|\[))"
)


def downstream_prompt(prompt):
    section = ""
    fence = None
    lines = []
    for line in prompt.split("\n"):
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})", line)
        if fence:
            if re.fullmatch(r" {0,3}" + re.escape(fence[0]) + "{" + str(len(fence)) + r",}[ \t]*", line):
                fence = None
        elif marker:
            fence = marker[1]
        elif line.startswith("# "):
            section = line.rstrip()
        elif (section == "# 任务目标" or section.startswith("# 标签保留与交付格式")) and not (
            line.startswith(("    ", "\t")) or line.lstrip().startswith(">")
        ):
            spans = list(INLINE_CODE.finditer(line))
            for match in reversed(list(STATUS_SENTENCE.finditer(line))):
                if not any(span.start() <= match.start() < span.end() for span in spans):
                    line = line[: match.start()] + line[match.end() :]
        lines.append(line)
    result = "\n".join(lines)
    validate_prompt_outline(result)
    return result


class FrozenPromptSession(ReadingCopyPromptSession):
    instructions = FROZEN_INSTRUCTIONS

    def prompt(self, final):
        generated = super().prompt(final)
        result = downstream_prompt(generated)
        if result != generated:
            atomic_write(self.root / "prompt-before-scope-check.md", generated.encode("utf-8"))
        goal = re.search(r"(?ms)^# 任务目标[ \t]*\n(.*?)(?=^# |\Z)", result)
        if goal and re.search(
            r"(?:无需|不要|不必|不生成|不做)(?:生成|输出)?(?:句段译文|译文或回填订单)|(?:本轮|本次)[^。！？\n]{0,16}仅(?:交付|输出)项目翻译要求",
            goal[1],
        ):
            raise ValueError(
                "任务目标需要指导收到业务订单后的译员执行翻译，请去掉把本次预处理步骤当成下游任务的文字。"
            )
        return result


async def run_patent_task(settings, folder, job, touch, agent):
    evidence = PatentEvidenceSession(folder, job, touch)
    await evidence.prepare()
    if any(s["views"] for s in evidence.sources):
        await agent(settings, folder, evidence)
        if not evidence.reading_complete:
            raise JobError("INCOMPLETE_DELIVERY", "原件或图示没有核对完。")
        evidence = PatentReviewSession(folder, job, touch, evidence)
        evidence.update("verifying_documents", "正在独立对照原件检查漏写与多写")
        await agent(settings, folder, evidence)
        if not evidence.reading_complete:
            raise JobError("INCOMPLETE_DELIVERY", "专利图示没有逐处完成独立复核。")
    snapshot = await run_material_io(evidence.freeze)
    session = FrozenPromptSession(folder, job, touch, snapshot)
    await session.generate_and_publish(settings, agent, processing_mode=material_patent.PIPELINE, prompt_input=snapshot)
