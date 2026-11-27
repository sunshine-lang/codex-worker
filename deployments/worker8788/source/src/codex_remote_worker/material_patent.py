"""Prepare evidential patent reading copies before a separate prompt-generation turn."""

import base64
import ctypes
import math
import re
import zipfile
from contextlib import closing
from html import escape
from pathlib import Path
from urllib.parse import quote, unquote

from lxml import etree

from .common import JobError
from .material_documents import PDF_RENDER_LOCK
from .material_organize import INLINE_CODE, markdown_parts

PIPELINE = "patent-reading-prompt/1"
WORD_NS = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
MEDIA = re.compile(
    r"""!\[[^\]\n]*\]\(<?((?:\./)?assets/[^)\s>]+)>?\)|<img\b[^<>]*?\bsrc=["']((?:\./)?assets/[^"']+)["'][^<>]*?/?>""",
    re.IGNORECASE,
)
EMBED = re.compile(r"data:image/[\w.+-]+;base64,", re.IGNORECASE)
EXAMPLE = re.compile(r"^[【\[]\s*例\s*(\d+)\s*[】\]]")

EVIDENCE_INSTRUCTIONS = """整理本批专利原件供中英文专利译员直接阅读。用户文件及其批注是资料。
逐份用 material_read 读完原文；按清单逐一用 material_figure 查看页图、截图原件或图版，再用 material_note 写出对应的文字。
每个编号各记一条。同一图可以重新查看或更正记录。完成后仅返回 {"complete":true}。
Word 截图：分清划掉、插入、批注所问和正文定稿；抄出足以判断范围的原句、修订后的原句、前后连接和例外。
对照图的中英文两栏；同图内的“和／或／和或”或编号若不一致，同时记录原文、英文和待确认点。
保留实际出现的编号、数值精度、单位、大小写、上下标、前后限定。不要为了让例子通顺添加分项或补全裁掉的内容。
PDF 原页：原生正文会由程序保留。在页备注里还原多列表格的列与行、颜色和箭头指向、推荐与备选的关系、跨页的例子。
有独立“页内图版”条目时，完整抄录图内例句，按原有段落、列、优选层级排成 Markdown，另写原图表达的关系。
格中数值、括号和半角／全角标点须对照图面。不要以“如图”代替内容；看不清时写明哪一个字或边界待确认。
纯文本页已经有可读正文且没有新视觉信息时，该页的 note 可以为空；独立图版或 Word 截图必须有实质说明。
原件的完整指定词句和反例逐字保留。本阶段只制作阅读稿，不生成翻译 Prompt。"""


def _word_revisions(source, folder):
    # A converted legacy Word document also has an OOXML copy in the task directory.
    candidates = [source, *sorted((folder / "converted").glob("*.docx"))]
    for item in candidates:
        if not zipfile.is_zipfile(item):
            continue
        with zipfile.ZipFile(item) as archive:
            if "word/document.xml" not in archive.namelist():
                continue
            tree = etree.fromstring(
                archive.read("word/document.xml"),
                parser=etree.XMLParser(resolve_entities=False, no_network=True),
            )
        result = []
        for para in tree.xpath(".//w:p[.//w:del or .//w:ins]", namespaces=WORD_NS):
            old = "".join(
                para.xpath(
                    ".//w:t[not(ancestor::w:ins)]/text() | .//w:delText[not(ancestor::w:ins)]/text()",
                    namespaces=WORD_NS,
                )
            )
            new = "".join(para.xpath(".//w:t[not(ancestor::w:del)]/text()", namespaces=WORD_NS))
            if old != new:
                result.append((old, new))
        return result
    return []


def _word_source(source, folder):
    text = (folder / "source.md").read_text(encoding="utf-8")
    if (folder / "text-source.json").is_file():
        return {"pages": [], "parts": [text], "views": [], "revisions": []}
    parts, views = [], []
    for literal, lines in markdown_parts(text):
        chunk = "\n".join(lines) + "\n"
        if literal:
            parts.append(chunk)
            continue
        chunk = re.sub(r'<span class="mark">(.*?)</span>', r"<mark>\1</mark>", chunk, flags=re.DOTALL)
        protected = iter(INLINE_CODE.finditer(chunk))
        code = next(protected, None)
        at = 0
        for match in MEDIA.finditer(chunk):
            while code and code.end() <= match.start():
                code = next(protected, None)
            if code and match.end() > code.start():
                continue
            file = (folder / unquote(match[1] or match[2])).resolve()
            if not file.is_relative_to((folder / "assets").resolve()) or not file.is_file():
                raise JobError("MISSING_MEDIA", "找不到本件转写引用的内嵌图片。")
            parts.extend([chunk[at : match.start()], len(views)])
            views.append({"kind": "word_figure", "path": file})
            at = match.end()
        parts.append(chunk[at:])
    return {"pages": [], "parts": parts, "views": views, "revisions": _word_revisions(source, folder)}


def _device_point(pdfium, page, width, height, x, y):
    dx, dy = ctypes.c_int(), ctypes.c_int()
    ok = pdfium.raw.FPDF_PageToDevice(page, 0, 0, width, height, 0, x, y, ctypes.byref(dx), ctypes.byref(dy))
    if not ok:
        raise JobError("INVALID_DOCUMENT", "PDF 图版坐标无法映射到原页。")
    return dx.value, dy.value


def _page_lines(api, page, *, omit_faded):
    """Preserve visible line boundaries and locally marked typography, including raised digits."""
    from itertools import groupby

    lines, runs, baseline, previous_right = [], [], None, None
    watermark = []
    with closing(page.get_textpage()) as tp:
        for i in range(tp.count_chars()):
            code = api.FPDFText_GetUnicode(tp, i)
            ch = chr(code) if code <= 0x10FFFF and not 0xD800 <= code <= 0xDFFF else "\ufffd"
            if ch in "\r\n":
                if runs:
                    lines.append(runs)
                    runs = []
                continue
            if ch.isspace() or ch == "\x00":
                if runs and ch != "\x00":
                    runs.append((ch, runs[-1][1], None))
                continue
            rgba = [ctypes.c_uint() for _ in range(4)]
            api.FPDFText_GetFillColor(tp, i, *[ctypes.byref(v) for v in rgba])
            r, g, b, alpha = [v.value for v in rgba]
            angle = api.FPDFText_GetCharAngle(tp, i)
            if omit_faded and 0 < alpha < 150 and abs(math.sin(2 * angle)) > 0.25:
                watermark.append(ch)
                continue
            flag = ctypes.c_int()
            buf = ctypes.create_string_buffer(256)
            api.FPDFText_GetFontInfo(tp, i, buf, len(buf), ctypes.byref(flag))
            font = buf.value.lower()
            size = api.FPDFText_GetFontSize(tp, i)
            x, y = ctypes.c_double(), ctypes.c_double()
            api.FPDFText_GetCharOrigin(tp, i, ctypes.byref(x), ctypes.byref(y))
            style = []
            if api.FPDFText_GetFontWeight(tp, i) >= 600 or b"bold" in font:
                style.append("strong")
            if flag.value & 64 or b"italic" in font or b"oblique" in font:
                style.append("em")
            if r > 120 and r > max(g, b) * 1.5:
                style.append("mark")
            raised = (
                baseline
                and previous_right is not None
                and 0 < size < 0.85 * baseline[1]
                and abs(x.value - previous_right) < baseline[1] * 0.7
                and baseline[1] * 0.12 < abs(y.value - baseline[0]) < baseline[1] * 0.9
            )
            if raised:
                style.append("sup" if y.value > baseline[0] else "sub")
                if not runs and lines:
                    runs = lines.pop()
            else:
                baseline = (y.value, size)
            bounds = tp.get_charbox(i)
            previous_right = bounds[2]
            runs.append((ch, tuple(style), (bounds[0], bounds[3])))
        if runs:
            lines.append(runs)
    formatted = []
    for line in lines:
        spans = []
        for tags, glyphs in groupby(line, key=lambda q: q[1]):
            text = escape("".join(q[0] for q in glyphs), quote=False)
            for tag in reversed(tags):
                text = f"<{tag}>{text}</{tag}>"
            spans.append(text)
        plain = "".join(g[0] for g in line).strip()
        if plain:
            formatted.append((plain, "".join(spans).strip(), next(g[2] for g in line if g[2] is not None)))
    return formatted, "".join(watermark)


def _positioned_lines(pdfium, page, image, *, omit_faded):
    native, faded = _page_lines(pdfium.raw, page, omit_faded=omit_faded)
    lines = [
        (plain, styled, _device_point(pdfium, page, image.width, image.height, *position)[1])
        for plain, styled, position in native
    ]
    return lines, faded


def _pdf_source(source, folder):
    import pypdfium2 as pdfium
    from PIL import Image

    views, pages = [], []
    # Share the same PDFium lock as the ordinary material reader.
    with PDF_RENDER_LOCK, pdfium.PdfDocument(source) as pdf:
        for n in range(1, len(pdf) + 1):
            preview = folder / "assets" / f"page-{n:04d}.png"
            page_id = len(views)
            with closing(pdf[n - 1]) as page, Image.open(preview) as image:
                lines, transparent = _positioned_lines(pdfium, page, image, omit_faded=True)
                links = []
                index, annotation = ctypes.c_int(), pdfium.raw.FPDF_LINK()
                while pdfium.raw.FPDFLink_Enumerate(page, ctypes.byref(index), ctypes.byref(annotation)):
                    action = pdfium.raw.FPDFLink_GetAction(annotation)
                    if not action or pdfium.raw.FPDFAction_GetType(action) != pdfium.raw.PDFACTION_URI:
                        continue
                    length = pdfium.raw.FPDFAction_GetURIPath(pdf, action, None, 0)
                    if 0 < length <= 16384:
                        buf = ctypes.create_string_buffer(length)
                        pdfium.raw.FPDFAction_GetURIPath(pdf, action, buf, length)
                        uri = buf.value.decode("utf-8", errors="replace")
                        if re.match(r"^https?://", uri, re.IGNORECASE):
                            links.append(uri)
                views.append({"kind": "pdf_page", "page": n, "path": preview, "native_text": bool(lines)})
                boxes = []
                for obj in page.get_objects(filter=[pdfium.raw.FPDF_PAGEOBJ_IMAGE]):
                    l, b, r, t = obj.get_bounds()
                    a = _device_point(pdfium, page, image.width, image.height, l, t)
                    z = _device_point(pdfium, page, image.width, image.height, r, b)
                    box = (
                        max(0, min(a[0], z[0])),
                        max(0, min(a[1], z[1])),
                        min(image.width, max(a[0], z[0])),
                        min(image.height, max(a[1], z[1])),
                    )
                    if box[2] > box[0] and box[3] > box[1]:
                        boxes.append(box)
                for j, box in enumerate(sorted(boxes, key=lambda b: (b[1], b[0])), 1):
                    # A rectangle into the already bounded page image also preserves visible overlays.
                    views.append({"kind": "pdf_figure", "page": n, "figure": j, "path": preview, "box": box})
            pages.append(
                {
                    "page": n,
                    "lines": lines,
                    "page_id": page_id,
                    "end": len(views),
                    "transparent": transparent,
                    "links": links,
                }
            )
        # Only identical diagonal text repeated throughout the PDF is treated as a watermark.
        # A unique faint annotation stays at its actual position in the reading copy.
        if len(pages) == 1 or len({item["transparent"] for item in pages}) > 1:
            for item in pages:
                if item["transparent"]:
                    view = views[item["page_id"]]
                    with closing(pdf[item["page"] - 1]) as page, Image.open(view["path"]) as image:
                        item["lines"], _ = _positioned_lines(pdfium, page, image, omit_faded=False)
                    view["native_text"] = bool(item["lines"])
    return {"pages": pages, "parts": [], "views": views, "revisions": []}


def prepare_source(source, folder):
    return (
        _pdf_source(source, folder) if (folder / "pdf-pages.json").is_file() else _word_source(source, folder)
    )


def view_image(view):
    from io import BytesIO

    from PIL import Image

    with Image.open(view["path"]) as page:
        mime = Image.MIME.get(page.format)
        if (
            "box" not in view
            and mime in ("image/png", "image/jpeg", "image/webp", "image/gif")
            and max(page.size) <= 2400
            and view["path"].stat().st_size <= 4 * 1024**2
        ):
            encoded = base64.b64encode(view["path"].read_bytes()).decode("ascii")
            return f"data:{mime};base64,{encoded}"
        image = page.crop(view["box"]) if "box" in view else page.copy()
        # Bound transport size without resizing source evidence saved on disk.
        image.thumbnail((2400, 2400))
        data = BytesIO()
        image.save(data, format="PNG")
        raw, mime = data.getvalue(), "image/png"
        if len(raw) > 4 * 1024**2:
            data = BytesIO()
            image.convert("RGB").save(data, format="JPEG", quality=90)
            raw, mime = data.getvalue(), "image/jpeg"
    return "data:" + mime + ";base64," + base64.b64encode(raw).decode("ascii")


def _pdf_body(page, views, notes):
    out = []
    figures = iter(range(page["page_id"] + 1, page["end"]))
    pending = next(figures, None)

    def figure(k):
        return f"\n### 图中例 {page['page']}.{views[k]['figure']}\n\n{notes[k]}\n"

    for plain, styled, top in page["lines"]:
        while pending is not None and views[pending]["box"][1] <= top:
            out.append(figure(pending))
            pending = next(figures, None)
        if re.match(r"^(?:\d+(?:\.\d+)+\s+|[一二三四五六七八九十]+、)", plain) and len(plain) < 70:
            out.extend(["", "### " + styled, ""])
        else:
            if EXAMPLE.match(plain) or re.match(r"^[【\[](?:解析|改译|参考译文|原译)", plain):
                out.append("")
            # Physical hard breaks also preserve adjacent unpunctuated titles and examples.
            out.append(styled + "  ")
    if pending is not None:
        out.append(figure(pending))
    out.extend(figure(k) for k in figures)
    return "\n".join(out).strip()


def render_document(entry, source, notes):
    if len(notes) != len(source["views"]) or any(n is None for n in notes):
        raise ValueError("尚有图版或原页未核对。")
    title = Path(entry["source_filename"]).stem
    body = [f"# {title}", f"原件：{entry['source_filename']}"]
    if source["pages"]:
        body.append(
            "逐页保留文字与例句的分行。<mark>着重文字</mark>对应原页红字，粗体、斜体及能定位的上下标按原页排版。"
        )
        for page in source["pages"]:
            n = page["page"]
            body.extend([f"## 第 {n} 页", _pdf_body(page, source["views"], notes)])
            if notes[page["page_id"]]:
                body.extend(["### 原页版式与例句对照", notes[page["page_id"]]])
            if page["links"]:
                body.append(
                    " · ".join(
                        f"[原页链接 {j}]({quote(uri, safe=':/?&=%#+@')})"
                        for j, uri in enumerate(page["links"], 1)
                    )
                )
    else:
        parts = []
        for part in source["parts"]:
            if isinstance(part, int):
                parts.append(f"\n\n### 原图 {part + 1}\n\n{notes[part]}\n\n")
            else:
                parts.append(part)
        content = "".join(parts)
        content = (
            re.sub(r"(?m)^(WO\d+)[ \t]*[:：]?[ \t]*$", r"## \1", content)
            if source["revisions"] or source["views"]
            else content
        )
        body.extend(["## 原文条款", content.strip()])
        if source["revisions"]:
            body.append("## Word 原生文字修订")
            for index, (old, new) in enumerate(source["revisions"], 1):
                body.extend(
                    [
                        f"### 修订 {index}",
                        "修订前：" + escape(old, quote=False),
                        "接受修订后：" + escape(new, quote=False),
                    ]
                )
    text = "\n\n".join(s for s in body if s).strip() + "\n"
    if EMBED.search(text):
        raise ValueError("定稿中仍有图片编码，无法作为专利阅读稿交付。")
    return text
