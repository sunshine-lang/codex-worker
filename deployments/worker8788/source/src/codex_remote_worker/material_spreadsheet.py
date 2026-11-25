"""Turn Excel material rows and their embedded drawing anchors into a paginatable Word view."""
from __future__ import annotations

from collections import defaultdict
from datetime import date, time
from io import BytesIO
import posixpath
from urllib.parse import unquote
import zipfile

from docx import Document
from docx.shared import Inches, RGBColor
from lxml import etree
from openpyxl import load_workbook
from openpyxl.cell.rich_text import CellRichText, TextBlock
from openpyxl.utils import get_column_letter, range_boundaries
from openpyxl.utils.exceptions import InvalidFileException
from PIL import Image, UnidentifiedImageError

from .common import JobError


SHEET = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
RID = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
DRAW = "{http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing}"
ART = "{http://schemas.openxmlformats.org/drawingml/2006/main}"


def xml(archive, name):
    root = etree.fromstring(archive.read(name), etree.XMLParser(resolve_entities=False, no_network=True))
    if root.getroottree().docinfo.doctype:
        raise JobError("INVALID_DOCUMENT", "Excel 材料含不支持的 DTD。")
    return root


def relationships(archive, owner):
    directory, filename = posixpath.split(owner)
    part = posixpath.join(directory, "_rels", filename + ".rels")
    return {r.get("Id"): r for r in xml(archive, part)} if part in archive.NameToInfo else {}


def linked_part(rels, rid, owner, kind):
    rel = rels[rid]
    if rel.get("TargetMode") == "External":
        raise JobError("EXTERNAL_RESOURCE", "请先把 Excel 的截图嵌入工作簿。")
    if not rel.get("Type", "").endswith("/" + kind):
        raise JobError("INVALID_DOCUMENT", "Excel 附件引用类型与工作表不一致。")
    target = unquote(rel.get("Target"))
    return posixpath.normpath(posixpath.join(posixpath.dirname(owner), target)).lstrip("/")


def screenshots(archive, sheet, part):
    by_row = defaultdict(list)
    sheet_rels = relationships(archive, part)
    for drawing in sheet.findall(SHEET + "drawing"):
        path = linked_part(sheet_rels, drawing.get(RID + "id"), part, "drawing")
        rels = relationships(archive, path)
        for anchor in xml(archive, path):
            point, picture = anchor.find(DRAW + "from"), anchor.find(DRAW + "pic")
            if point is None or picture is None:
                raise JobError("UNSUPPORTED_DRAWING", "Excel 绘图需为具有单元格锚点的内嵌图片。")
            row = int(point.findtext(DRAW + "row")) + 1
            col = int(point.findtext(DRAW + "col")) + 1
            if not (1 <= row <= 1048576 and 1 <= col <= 16384):
                raise JobError("INVALID_DOCUMENT", "Excel 图片锚点超出工作表范围。")
            blip = picture.find(".//" + ART + "blip")
            if blip is None or not blip.get(RID + "embed"):
                raise JobError("EXTERNAL_RESOURCE", "Excel 图片缺少内嵌图像。")
            media = linked_part(rels, blip.get(RID + "embed"), path, "image")
            transform = picture.find(".//" + ART + "xfrm")
            if transform is not None and any(transform.get(k) not in (None, "0", "false") for k in ("rot", "flipH", "flipV")):
                raise JobError("UNSUPPORTED_DRAWING", "请先将已旋转或翻转的截图另存为嵌入图片。")
            rect = picture.find(".//" + ART + "srcRect")
            crop = tuple(int(rect.get(k, 0)) if rect is not None else 0 for k in ("l", "t", "r", "b"))
            by_row[row].append((int(point.findtext(DRAW + "rowOff", "0")), col,
                                int(point.findtext(DRAW + "colOff", "0")), media, crop))
    for pictures in by_row.values():
        pictures.sort(key=lambda pic: pic[:3])
    return by_row


def visible_image(raw, crop):
    with Image.open(BytesIO(raw)) as im:
        w, h = im.size
        if any(crop):
            left, top, right, bottom = (edge / 100000 for edge in crop)
            box = (max(0, round(w * left)), max(0, round(h * top)),
                   min(w, round(w * (1 - right))), min(h, round(h * (1 - bottom))))
            if box[2] <= box[0] or box[3] <= box[1]:
                raise ValueError("empty Excel picture crop")
            im = im.crop(box)
        if any(crop) or im.format not in ("PNG", "JPEG"):
            encoded = BytesIO()
            im.convert("RGBA").save(encoded, format="PNG")
            raw = encoded.getvalue()
        return raw, im.size


def add_value(paragraph, value, number_format):
    if isinstance(value, CellRichText):
        for item in value:
            if not isinstance(item, TextBlock):
                paragraph.add_run(str(item))
                continue
            run = paragraph.add_run(item.text)
            font = item.font
            run.bold, run.italic = font.b, font.i
            if font.u:
                run.underline = True
            if font.strike:
                run.font.strike = True
            if font.vertAlign in ("superscript", "subscript"):
                setattr(run.font, font.vertAlign, True)
            if font.color and font.color.type == "rgb":
                run.font.color.rgb = RGBColor.from_string(font.color.rgb[-6:])
    elif isinstance(value, (date, time)):
        paragraph.add_run(value.isoformat())
    else:
        paragraph.add_run(str(value))
        if type(value) in (int, float) and number_format not in ("General", "0", "@"):
            paragraph.add_run(f"（Excel 格式：{number_format}）")


def write_row(doc, archive, sheet_name, index, cells, images, merged, formulas, remaining):
    used = 0
    doc.add_heading(f"第 {index} 行", level=2)
    for cell in cells:
        loc = cell.coordinate
        value = cell.value
        if value is None:
            expression = formulas[loc].text
            if not expression:
                raise JobError("INVALID_DOCUMENT", f"{sheet_name}!{loc} 缺少公式缓存和可读公式。")
            value = "=" + expression
        used += len(str(value).encode("utf-8"))
        if used > remaining:
            raise JobError("LIMIT_EXCEEDED", "Excel 转写内容超过交付预算。")
        paragraph = doc.add_paragraph()
        paragraph.add_run(merged.get(loc, loc) + "　")
        add_value(paragraph, value, cell.number_format)
    for _, col, _, media, crop in images:
        coordinate = f"{get_column_letter(col)}{index}"
        image, (w, h) = visible_image(archive.read(media), crop)
        used += len(image)
        if used > remaining:
            raise JobError("LIMIT_EXCEEDED", "Excel 截图内容超过交付预算。")
        label = f"{sheet_name} · {coordinate} 截图"
        caption = doc.add_paragraph(label)
        caption.paragraph_format.keep_with_next = True
        width = min(6, w / 96, 6 * w / h)
        shape = doc.add_picture(BytesIO(image), width=Inches(width))
        shape._inline.docPr.set("descr", label)
    return used


def write_sheet(doc, archive, ws, remaining):
    used = 0
    populated = False
    # openpyxl's read-only sheet records the resolved OOXML part, including nonstandard names.
    part = ws._worksheet_path
    sheet = xml(archive, part)
    pictures = screenshots(archive, sheet, part)
    merged = {}
    for entry in sheet.iter(SHEET + "mergeCell"):
        span = entry.get("ref")
        col, row, _, _ = range_boundaries(span)
        merged[f"{get_column_letter(col)}{row}"] = span
    formulas = {c.get("r"): c.find(SHEET + "f") for c in sheet.iter(SHEET + "c")
                if c.find(SHEET + "f") is not None}
    if any("DISPIMG(" in (f.text or "").upper() for f in formulas.values()):
        raise JobError("UNSUPPORTED_DRAWING", "单元格图片需先转换为有行锚点的内嵌图片。")
    doc.add_heading(f"工作表 {ws.title}", level=1)
    ws.reset_dimensions()
    for i, row in enumerate(ws.rows, 1):
        cells = [cell for cell in row if cell.value is not None or getattr(cell, "coordinate", None) in formulas]
        images = pictures.pop(i, [])
        if cells or images:
            populated = True
            used += write_row(doc, archive, ws.title, i, cells, images, merged, formulas, remaining - used)
    for i, images in sorted(pictures.items()):
        populated = True
        used += write_row(doc, archive, ws.title, i, [], images, merged, formulas, remaining - used)
    return used, populated


def workbook_view(source, folder, max_package, max_delivery):
    """Read sparse worksheets; each picture occurrence gets a sheet/row/column label."""
    doc = Document()
    used = 0
    populated = False
    try:
        with zipfile.ZipFile(source) as archive:
            entries = archive.infolist()
            if len(entries) > 12000 or sum(e.file_size for e in entries) > max_package:
                raise JobError("LIMIT_EXCEEDED", "Excel 文档解包后过大。")
            if "xl/workbook.xml" not in archive.NameToInfo or len(entries) != len(archive.NameToInfo):
                raise JobError("INVALID_DOCUMENT", "需要结构完整的 XLSX 工作簿。")
            for name in archive.namelist():
                if name.startswith(("xl/richData/", "xl/cellimages", "xl/vbaProject")):
                    raise JobError("UNSUPPORTED_DOCUMENT", "此工作簿含不支持的单元格图片或宏。")
                if name.endswith(".rels"):
                    for rel in xml(archive, name):
                        kind = rel.get("Type", "")
                        if name.lower().endswith(".vml.rels") and kind.endswith("/image"):
                            raise JobError("UNSUPPORTED_DRAWING", "Excel 含旧式 VML 图片，请先改为单元格锚点截图。")
                        if rel.get("TargetMode") == "External" and not kind.endswith("/hyperlink"):
                            raise JobError("EXTERNAL_RESOURCE", "请嵌入 Excel 引用的外部资源。")
            workbook = load_workbook(source, read_only=True, rich_text=True, data_only=True, keep_links=False)
            try:
                if len(workbook.worksheets) != len(workbook.sheetnames):
                    raise JobError("UNSUPPORTED_DRAWING", "Excel 材料含独立图表页，请提供带单元格锚点的截图。")
                for ws in workbook.worksheets:
                    consumed, has_rows = write_sheet(doc, archive, ws, max_delivery - used)
                    used += consumed
                    populated = populated or has_rows
            finally:
                workbook.close()
        if not populated:
            raise JobError("EMPTY_DOCUMENT", "Excel 工作簿没有可读单元格或截图。")
        path = folder / "spreadsheet.docx"
        doc.save(path)
        return path
    except (zipfile.BadZipFile, etree.XMLSyntaxError, InvalidFileException, UnidentifiedImageError,
            Image.DecompressionBombError, KeyError, ValueError, OSError, TypeError, IndexError) as exc:
        raise JobError("INVALID_DOCUMENT", "Excel 表格、图片或锚点无法转写。") from exc
