"""Resolve model citations against this submission's prepared Markdown files."""
import re
import subprocess
import unicodedata
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

from lxml import html

from .common import JobError
from .material_documents import pandoc_executable

GUIDES = "# 外部指南文档挂载与优先级说明（KNOWLEDGE BASE GUIDELINES）"


def material_manifest(documents):
    manifest = []
    for i, doc in enumerate(documents):
        prefix = f"{i + 1:02d}-"
        # A valid Windows input name can exceed the 255-byte Unix component limit.
        stem = Path(doc["source_filename"]).stem.encode("utf-8")[:255 - len(prefix) - len(".md")].decode("utf-8", "ignore")
        manifest.append({**doc, "markdown_filename": prefix + stem + ".md", "reference": f"[材料](material:{i})"})
    return manifest


def markdown_link(name):
    # A code-span label preserves repeated spaces, brackets, entities and other literal filename characters.
    ticks = "`" * (1 + max((len(m[0]) for m in re.finditer(r"`+", name)), default=0))
    return f"[{ticks}{name}{ticks}]({quote(name, safe='')})"


def resolve_references(prompt, docs):
    # GFM source positions let us edit only real links, never a similar string in a code example.
    try:
        rendered = subprocess.run([pandoc_executable(), "--sandbox", "-f", "gfm+sourcepos", "-t", "html5",
                                   "--wrap=none"], input=prompt.encode("utf-8"), capture_output=True,
                                  check=True, timeout=60).stdout.decode("utf-8")
    except (OSError, subprocess.SubprocessError) as exc:
        raise JobError("REFERENCE_CHECK_FAILED", "无法核对项目 Prompt 的材料链接。") from exc
    root = html.fragment_fromstring(rendered, create_parent="main")
    names = {doc["markdown_filename"] for doc in docs}
    material_labels = names | {doc["source_filename"] for doc in docs}
    references = {f"material:{i}": doc for i, doc in enumerate(docs)}
    catalog = set()
    edits = []
    lines = prompt.split("\n")
    in_guides = False
    for el in root.iter():
        if el.tag == "h1" and all(p.tag in ("main", "div", "section") for p in el.iterancestors()):
            in_guides = el.text_content() == GUIDES[2:]
        if el.tag not in ("a", "img"):
            continue
        target = el.get("href" if el.tag == "a" else "src", "")
        uri = urlsplit(target)
        if (not target or target.startswith("#") or uri.scheme.lower() in ("https", "http", "mailto")
                or (el.tag == "img" and uri.scheme == "data")):
            label = el.text_content().strip("《》")
            if label in material_labels or ((not target or target.startswith("#")) and label.lower().endswith(".md")):
                raise ValueError("本单材料链接必须指向交付清单中的同名 Markdown。")
            continue
        if uri.scheme == "material":
            doc = references.get(target)
            pos = re.fullmatch(r"(\d+):(\d+)-(\d+):(\d+)", el.get("data-pos", ""))
            if el.tag != "a" or doc is None or not pos:
                raise ValueError("材料引用无效。请从本单清单复制 reference，再提交完整 Prompt。")
            line, start, last, end = map(int, pos.groups())
            if (line != last or not 1 <= line <= len(lines) or not 1 <= start < end
                    or lines[line-1][start-1:end-1] != doc["reference"]):
                raise ValueError("请将清单中的 reference 原样写在普通正文中，每个引用保持在同一行。")
            name = doc["markdown_filename"]
            edits.append((line-1, start-1, end-1, markdown_link(name)))
        else:
            name = unquote(uri.path)
            if (el.tag != "a" or uri.scheme or uri.netloc or uri.query or uri.fragment
                    or name not in names or el.text_content() != name):
                raise ValueError("存在无法与交付文件对应的本地链接。请用本单 reference，未提供的外部资料用文字注明。")
        if in_guides:
            catalog.add(name)
    if missing := names - catalog:
        raise ValueError("请在外部指南一节逐份用清单 reference 列出材料及用途。缺少：" + "、".join(sorted(missing)))

    # Only the caller's material citations are renamed. Quoted source text and external titles survive verbatim.
    for el in root.xpath(".//a | .//pre | .//blockquote"):
        el.clear(keep_tail=True)
    def other_title(match):
        if match[2] != "下游交付物" and match[1] in material_labels:
            raise ValueError(f"《{match[1]}》已在本单材料清单中，请使用对应 reference。")
        return ""

    visible = re.sub(r"《([^《》\n]+)》\s*[（(]((?:本次)?未(?:随单)?提供|下游交付物)[）)]",
                     other_title, root.text_content())
    visible = " ".join(unicodedata.normalize("NFC", visible).casefold().split())
    for doc in docs:
        source = " ".join(unicodedata.normalize("NFC", doc["source_filename"]).casefold().split())
        if (Path(source).suffix and source in visible) or f"《{source}》" in visible:
            raise ValueError(f"正文仍以原件名 {doc['source_filename']} 引用材料。请改用对应 reference；逐字引文放入引用块或代码块。")
    if re.search(r"[\w)\]-]\.md(?![a-z0-9_])|\]\(material:", visible):
        raise ValueError("材料文件名必须用清单 reference 引用。未提供的资料用《原名》（未提供），指定交付物用《原名》（下游交付物）。")
    for line, start, end, replacement in sorted(edits, reverse=True):
        lines[line] = lines[line][:start] + replacement + lines[line][end:]
    return "\n".join(lines)
