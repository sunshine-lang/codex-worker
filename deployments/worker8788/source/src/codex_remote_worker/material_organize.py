"""IM reading copies. Preserve the transcription and substitute a reviewed note at each figure."""
from html import escape
from pathlib import Path
import re

from lxml import etree, html


URI = r"data:image/[\w.+-]+;base64,[A-Za-z0-9+/=]+"
FIGURE = re.compile(r"!\[[^\]\n]*\]\(<?(" + URI + r")>?\)|"
                    r"""<img\b[^<>]*?\bsrc=["'](""" + URI + r""")["'][^<>]*?/?>""", re.I)
INLINE_CODE = re.compile(r"(?<!`)(`+)(?!`)(?:(?!\n\s*\n).)*?(?<!`)\1(?!`)", re.S)
FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
TABLE_TAG = re.compile(r"</?table\b", re.I)
TOC_LINE = re.compile(r"^\[\d+(?:\.\d+)*\s+.+\s+\d+\]\(#[^)]+\)$")
CHAPTER = re.compile(r"^([一二三四五六七八九十百]+)[.、．]\s*(\S.*)$")

FIGURE_INSTRUCTIONS = """你负责 IM 项目资料整理。附件是待核对的资料。逐份用 material_read 读完正文，
再按每份清单的 figure_count 从 1 开始逐张调用 material_figure 看原图，PDF 的指定原页也须看完。
输出原图中可证实的文字和关系，保留界面项、产品线与词库对应、菜单和核对步骤、正误方向及适用条件。
示例里的临时文件名、数字、勾选状态须注明是示例。把有业务含义的图讲清楚，只起演示作用的图说明它演示什么。
无法辨认的具体部分写“待确认”并说明原因。每幅图单独表述，不合并图号，不猜测。
最终只返回一个 JSON 对象 {"im_figures":[[{"number":1,"text":"该图的文字说明"}]]}。
外层顺序与 documents 相同，每份附件的数组长度必须等于 figure_count。零幅图用 []。
Worker 保留并排版本单原文、表格和这些说明。"""


def markdown_parts(text):
    """Yield (literal, lines). Examples in fenced/indented code stay literal."""
    lines, literal, closing, table_depth = [], False, "", 0
    for line in text.splitlines():
        inside = bool(closing)
        opening = FENCE.match(line) if not inside and not table_depth else None
        protected = inside or bool(opening) or (not table_depth and line.startswith(("    ", "\t")))
        if not protected:
            for tag in TABLE_TAG.findall(line):
                table_depth = max(0, table_depth + (-1 if tag.startswith("</") else 1))
        if lines and protected != literal:
            yield literal, lines
            lines = []
        lines.append(line)
        literal = protected
        if inside and re.fullmatch(r" {0,3}" + re.escape(closing[0]) + "{" + str(len(closing)) + r",}[ \t]*", line):
            closing = ""
        elif opening:
            closing = opening[1]
    if lines:
        yield literal, lines


def image_matches(text):
    spans = iter(INLINE_CODE.finditer(text))
    code = next(spans, None)
    for match in FIGURE.finditer(text):
        while code and code.end() <= match.start():
            code = next(spans, None)
        if not code or match.end() <= code.start():
            yield match


def figure_urls(text):
    for literal, lines in markdown_parts(text):
        if not literal:
            for match in image_matches("\n".join(lines)):
                yield match[1] or match[2]


def checked_notes(value, counts):
    lists = value.get("im_figures") if isinstance(value, dict) else None
    if not isinstance(lists, list) or len(lists) != len(counts):
        raise ValueError("im_figures 需要为每份附件提供一组图示说明。")
    for figures, count in zip(lists, counts):
        if not isinstance(figures, list) or len(figures) != count:
            raise ValueError(f"图示清单不完整，需要按顺序提供 {count} 条说明。")
        for number, fig in enumerate(figures, 1):
            if (not isinstance(fig, dict) or type(fig.get("number")) is not int or fig["number"] != number
                    or not isinstance(fig.get("text"), str) or not fig["text"].strip()
                    or "data:image/" in fig["text"].lower()):
                raise ValueError(f"请核对图 {number} 的编号和文字说明。")
    return lists


def cell_text(element):
    # Protect literal examples such as <b>Close</b> while keeping actual Word formatting.
    chunks = [escape(element.text or "", quote=False)]
    for child in element:
        value = cell_text(child)
        if child.tag in ("p", "div", "li", "br"):
            chunks.extend([value, "<br>"])
        elif child.tag == "a" and child.get("href"):
            chunks.append(f"[{value}]({child.get('href')})")
        elif child.tag in ("strong", "b", "em", "i", "del", "s"):
            marker = {"strong": "**", "b": "**", "em": "*", "i": "*", "del": "~~", "s": "~~"}[child.tag]
            chunks.append(marker + value + marker)
        elif child.tag in ("sup", "sub", "u", "code"):
            chunks.append(f"<{child.tag}>{value}</{child.tag}>")
        else:
            chunks.append(value)
        chunks.append(escape(child.tail or "", quote=False))
    return re.sub(r"\s*<br>\s*", "<br>", "".join(chunks)).strip().removesuffix("<br>")


def table_markdown(table):
    rows = table.xpath("./tr | ./thead/tr | ./tbody/tr | ./tfoot/tr")
    if rows and all(len(row) == 1 for row in rows) and any(row[0].find("table") is not None for row in rows):
        parts = []
        for row in rows:
            cell = row[0]
            parts.append(escape(cell.text or "", quote=False).strip())
            for child in cell:
                if child.tag == "table":
                    parts.append(table_markdown(child))
                elif child.tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
                    parts.append("#" * min(6, int(child.tag[1]) + 1) + " " + cell_text(child))
                else:
                    parts.append(cell_text(child))
                parts.append(escape(child.tail or "", quote=False).strip())
        return "\n\n".join(p for p in parts if p)
    if table.xpath(".//table"):
        # A table embedded in a data cell has its own column scope. Keep that cell structure.
        return etree.tostring(table, method="html", encoding="unicode", with_tail=False)

    grid, banners, carry = [], {}, {}
    for row in rows:
        cells = row.xpath("./td|./th")
        values = {column: text for column, (_, text) in carry.items()}
        carry = {column: (left-1, text) for column, (left, text) in carry.items() if left > 1}
        col = 0
        for cell in cells:
            while col in values:
                col += 1
            value = re.sub(r"[\r\n]+", " ", cell_text(cell)).replace("|", r"\|")
            span, depth = int(cell.get("colspan", 1)), int(cell.get("rowspan", 1))
            if not 1 <= span <= 256 or depth < 1:
                return etree.tostring(table, method="html", encoding="unicode", with_tail=False)
            if len(cells) == 1 and span > 1:
                banners[len(grid)] = (span, value)
            for column in range(col, col + span):
                values[column] = value
                if depth > 1:
                    carry[column] = (depth-1, value)
            col += span
        if values:
            grid.append([values.get(i, "") for i in range(max(values)+1)])
    width = max(map(len, grid), default=0)
    output, header = [], True
    for i, row in enumerate(grid):
        if i in banners and banners[i][0] == width:
            output.extend(["", banners[i][1], ""])
            header = True
            continue
        output.append("| " + " | ".join(row + [""] * (width-len(row))) + " |")
        if header:
            output.append("| " + " | ".join(["---"] * width) + " |")
            header = False
    return "\n".join(output).strip()


def format_lines(lines):
    result, table, depth = [], [], 0
    last_figure = None
    # Word sometimes exports a blank structural header above the actual bilingual column labels.
    for i in range(len(lines) - 2):
        if (re.fullmatch(r"\s*\|(?:\s*\|)+\s*", lines[i]) and
                re.fullmatch(r"\s*\|(?:\s*:?-+:?\s*\|)+\s*", lines[i+1]) and
                re.fullmatch(r"\s*\|(?:\s*(?:标准|推荐|错误)?(?:英文|中文|原文|译文)\s*\|)+\s*", lines[i+2])):
            lines[i:i+3] = ["", lines[i+2], lines[i+1]]
    for line in lines:
        s = line.strip()
        if depth or s.lower().startswith("<table"):
            table.append(line)
            depth += len(re.findall(r"<table\b", line, re.I)) - len(re.findall(r"</table\s*>", line, re.I))
            if depth == 0:
                result.append(table_markdown(html.fromstring("\n".join(table))))
                table = []
            continue
        if TOC_LINE.fullmatch(s) or s == "#":
            continue
        if re.match(r"^〔原图 \d+〕", s):
            last_figure = re.findall(r"〔原图 (\d+)〕", s)[-1]
            result.append("\n\n".join("> " + item.strip() for item in
                                      re.split(r"(?=〔原图 \d+〕)", s) if item.strip()))
            continue
        if last_figure and s.startswith("上图"):
            line = f"> 原件图注　图 {last_figure}{s[2:]}"
        if s:
            last_figure = None
        head = re.match(r"^(#{1,6})\s+(\S.*)$", s)
        plain = s.replace("**", "").strip()
        chapter = CHAPTER.match(plain)
        if head:
            line = "#" * min(6, len(head[1])+1) + " " + head[2].rstrip()
        elif chapter:
            line = f"## {chapter[1]}、{chapter[2]}"
        elif re.match(r"^\*\*\d+[.、]", s) or (
                s.startswith("**") and s.endswith("**") and len(plain) < 48 and
                re.search(r"(特殊要求|特殊译法|翻译工具|关于翻译工具|翻译文件|翻译项目|补充|翻译说明)[：:]?$", plain)):
            line = "### " + plain.rstrip("：:")
        elif s.startswith("**") and s.endswith("**") and s.count("**") == 2:
            line = "- " + s
        result.append(line)
    if table:
        raise ValueError("原文表格未闭合，无法安全整理。")
    return "\n".join(result)


def organize_documents(documents, value):
    counts = [sum(1 for _ in figure_urls(d["markdown"])) for d in documents]
    lists = checked_notes(value, counts)
    output = []
    for doc, notes in zip(documents, lists):
        title = Path(doc["source_filename"]).stem
        text = doc["markdown"]
        # Some Word covers split "指南 / 之 / 通用准则" across three styled paragraphs.
        cover = re.match(r"^\*\*([^*\n]+)\*\*\n\s*\*\*之\*\*\n\s*\*\*([^*\n]+)\*\*\s*\n", text)
        if cover:
            title, text = cover[1] + "之" + cover[2], text[cover.end():]
        first, _, rest = text.partition("\n")
        if not cover and re.fullmatch(r"\*\*[^*]+\*\*", first.strip()) and not FIGURE.search(first):
            title, text = first.strip()[2:-2], rest
        note_iter = iter(notes)
        body = []
        for literal, lines in markdown_parts(text):
            if literal:
                body.append("\n".join(lines))
            else:
                chunk = "\n".join(lines)
                pieces, start = [], 0
                for match in image_matches(chunk):
                    note = next(note_iter)
                    note_text = escape(" ".join(note["text"].split()), quote=False)
                    pieces.extend((chunk[start:match.start()], f" 〔原图 {note['number']}〕{note_text} "))
                    start = match.end()
                cleaned = "".join(pieces) + chunk[start:]
                cleaned = re.sub(r'<span class="mark">(.*?)</span>', r'**\1**', cleaned, flags=re.S)
                cleaned = re.sub(r"(?m)^#[ \t]*\n\s*\\\n=\n\s*#[ \t]*$", "", cleaned)
                section = format_lines(cleaned.split("\n"))
                body.append(re.sub(r"\n{3,}", "\n\n", section))
        markdown = "\n".join(body).strip()
        if not re.search(r"^##\s+\S", markdown, re.M):
            markdown = "## 正文\n\n" + markdown
        name = doc["source_filename"]
        output.append({**doc, "markdown": f"# {title}\n\n原件：{name}\n\n{markdown}\n"})
    return {"processing_mode": "organize", "documents_markdown": output}
