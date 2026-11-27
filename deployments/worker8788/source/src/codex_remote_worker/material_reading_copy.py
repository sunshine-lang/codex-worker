"""Generate a project prompt from the exact reading copies returned to the caller."""
from .common import JobError, atomic_write, canonical, digest, write_json
from .material_agent import MaterialSession, PROJECT_RULES, PROMPT_OUTLINE, TOOLS, run_material_io
from .material_documents import MAX_DELIVERY
from .material_references import resolve_references


def freeze_reading_copies(root, documents, markdowns):
    folder = root / "reading-copy"
    folder.mkdir(exist_ok=True)
    files = []
    total = 0
    for doc, markdown in zip(documents, markdowns, strict=True):
        body = markdown.encode("utf-8")
        total += len(body)
        if total > MAX_DELIVERY:
            raise JobError("LIMIT_EXCEEDED", "阅读稿合计超过交付限制。")
        atomic_write(folder / doc["markdown_filename"], body)
        files.append({"source_filename": doc["source_filename"], "source_sha256": doc["sha256"],
                      "markdown_filename": doc["markdown_filename"], "sha256": digest(body), "bytes": len(body)})
    snapshot = {"files": files, "sha256": digest(canonical(files))}
    write_json(folder / "snapshot.json", snapshot)
    return snapshot


class ReadingCopyPromptSession(MaterialSession):
    tools = (TOOLS[0],)
    instructions = f"""为本单下游译员编写项目翻译 Prompt。项目需求与本次材料整理稿是依据。
按清单用 material_read 逐份分段读到末尾。原图已在对应位置整理为文字说明。
区分客户规定和示例，保留纠错前后、适用场景、例外及待确认点。引用清单的 reference 并注明原图号或材料位置。
清单里的 reference 会自动显示为可点击的交付文件名。目录与正文都直接使用它，例如：
- [材料](material:0) 原图 1：面板按钮的客户纠错及其适用范围。
每个出处只写 reference 和位置。不要在旁边再抄原件名、.md 文件名或“原件《…》”说明。
逐字复制下面适用的一级标题，保留英文大小写和标点。
零附件时根据业务、语向和项目需求生成基础 Prompt。输出一个含 translation_prompt 的 JSON 对象。
{PROJECT_RULES}
{PROMPT_OUTLINE}"""

    def __init__(self, root, job, touch, snapshot):
        super().__init__(root, job, touch)
        self.snapshot = snapshot
        self.prepared_count = len(self.docs)

    def read_source(self, index):
        entry = self.snapshot["files"][index]
        data = (self.root / "reading-copy" / entry["markdown_filename"]).read_bytes()
        if digest(data) != entry["sha256"]:
            raise JobError("INPUT_CHANGED", "阅读稿内容与生成 Prompt 的定稿不一致。")
        return data.decode("utf-8")

    def call(self, name, args):
        if name != "material_read":
            raise ValueError("此阶段只能读取本单已定稿的 Markdown。")
        return super().call(name, args)

    def publish(self, prompt, metadata):
        if not self.reading_complete or not isinstance(prompt, str) or not prompt.strip():
            raise JobError("INCOMPLETE_DELIVERY", "Prompt 或定稿阅读记录不完整。")
        try:
            prompt = resolve_references(prompt, self.docs)
        except ValueError as exc:
            raise JobError("INCOMPLETE_DELIVERY", str(exc)) from exc
        if sum(f["bytes"] for f in self.snapshot["files"]) + len(prompt.encode("utf-8")) > MAX_DELIVERY:
            raise JobError("LIMIT_EXCEEDED", "Prompt 与阅读稿合计超过交付限制。")
        docs = [{"source_filename": d["source_filename"], "markdown_filename": d["markdown_filename"],
                 "markdown": self.read_source(i)} for i, d in enumerate(self.docs)]
        write_json(self.root / "result.json", {"translation_prompt": prompt, "documents_markdown": docs, **metadata})

    async def generate_and_publish(self, settings, agent, **metadata):
        self.touch(reading_copy_sha256=self.snapshot["sha256"], stage="generating_prompt",
                   recent_action="已定稿，正在根据本单整理稿生成 Prompt", current_file=None)
        prompt = await agent(settings, self.root, self)
        self.update("validating_result", "正在逐份核对 Prompt 输入与交付文件")
        await run_material_io(self.publish, prompt, metadata)
