"""Lossless text/structure path and original-page previews for the materials task."""
import base64
from contextlib import closing
import io
import json
import math
import mimetypes
import os
import re
import shutil
import subprocess
import threading
import zipfile
from pathlib import Path
from urllib.parse import unquote

from lxml import etree

from .common import JobError, write_json

DOCUMENT_TYPES = ("doc", "docx", "xlsx", "pdf")
MAX_PACKAGE = 256 * 1024 * 1024
MAX_DELIVERY = 96 * 1024 * 1024
PDF_RENDER_LOCK = threading.Lock()
MEDIA_URL = re.compile(r'(?P<prefix>!?\[[^\]\n]*\]\(|\bsrc=["\x27])(?P<path>(?:\./)?assets/[^)"\x27\s>]+)')
PDF_PAGE_IMAGE = re.compile(r"^(?P<prefix>!\[第 \d+ 页原图\]\()(?P<path>assets/page-\d+\.png)(?=\)$)", re.MULTILINE)


def office_executable():
    program_files = Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
    candidates = [os.getenv("MATERIALS_SOFFICE"), shutil.which("soffice"),
                  program_files / "LibreOffice/program/soffice.com",
                  program_files / "LibreOffice/program/soffice.exe"]
    for path in candidates:
        if path and Path(path).is_file():
            return str(path)
    raise JobError("MISSING_CONVERTER", "需要 LibreOffice；可用 MATERIALS_SOFFICE 指定 soffice 路径。", 503)


def pandoc_executable():
    if program := shutil.which("pandoc"):
        return program
    import pypandoc
    return pypandoc.get_pandoc_path()


def run_converter(argv, directory):
    try:
        result = subprocess.run(argv, cwd=directory, capture_output=True, timeout=120, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise JobError("CONVERSION_FAILED", "文档转换程序无法运行或超时。") from exc
    if result.returncode:
        raise JobError("CONVERSION_FAILED", f"文档转换失败，退出码 {result.returncode}。")


def office_convert(source, folder, target):
    output = folder / (source.stem + "." + target)
    if not output.is_file():
        folder.mkdir(parents=True, exist_ok=True)
        profile = (folder / "office-profile").resolve().as_uri()
        run_converter([office_executable(), "-env:UserInstallation=" + profile, "--headless",
                       "--norestore", "--convert-to", target, "--outdir", str(folder), str(source)], folder)
    if not output.is_file() or not output.stat().st_size:
        raise JobError("CONVERSION_FAILED", "LibreOffice 没有生成预期文件。")
    return output


def checked_docx(path, folder):
    try:
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
            if len(entries) > 12000 or sum(i.file_size for i in entries) > MAX_PACKAGE:
                raise JobError("LIMIT_EXCEEDED", "Word 文档解包后过大。")
            if "word/document.xml" not in archive.namelist():
                raise JobError("INVALID_DOCUMENT", "文件缺少 Word 正文。")
            changes = {}
            parser = etree.XMLParser(resolve_entities=False, no_network=True)
            for entry in entries:
                if entry.filename.endswith(".rels"):
                    rels = etree.fromstring(archive.read(entry), parser)
                    external = [r for r in rels if r.get("TargetMode") == "External" and not r.get("Type", "").endswith("/hyperlink")]
                    if not external:
                        continue
                    # Word often keeps an Outlook cid link alongside the real embedded image.
                    part = Path(entry.filename)
                    owner = (part.parent.parent / part.stem).as_posix()
                    if owner not in archive.namelist():
                        raise JobError("EXTERNAL_RESOURCE", "Word 引用了未嵌入的外部资源。")
                    xml = etree.fromstring(archive.read(owner), parser)
                    embedded = {r.get("Id") for r in rels if r.get("TargetMode") != "External" and r.get("Type", "").endswith("/image")}
                    for rel in external:
                        for elem in xml.iter():
                            for key, value in list(elem.attrib.items()):
                                if value != rel.get("Id"):
                                    continue
                                if key.endswith("}link") and elem.get(key[:-4] + "embed") in embedded:
                                    del elem.attrib[key]
                                else:
                                    raise JobError("EXTERNAL_RESOURCE", "Word 引用了未嵌入的外部资源，请嵌入原件后提交。")
                        rels.remove(rel)
                    changes[entry.filename] = etree.tostring(rels, encoding="UTF-8", xml_declaration=True)
                    changes[owner] = etree.tostring(xml, encoding="UTF-8", xml_declaration=True)
            if changes:
                clean = folder / "embedded.docx"
                with zipfile.ZipFile(clean, "w", compression=zipfile.ZIP_DEFLATED) as output:
                    for entry in entries:
                        output.writestr(entry.filename, changes[entry.filename] if entry.filename in changes else archive.read(entry))
                return clean
            return path
    except (ValueError, RuntimeError, OSError, zipfile.BadZipFile, etree.XMLSyntaxError) as exc:
        raise JobError("INVALID_DOCUMENT", "文件不是可读取的 Word 文档。") from exc


def transcribe_pdf(source, folder):
    import pypdfium2 as pdfium

    assets = folder / "assets"
    assets.mkdir(exist_ok=True)
    output = folder / "source.md"
    visual_pages = []
    graphics = [pdfium.raw.FPDF_PAGEOBJ_IMAGE, pdfium.raw.FPDF_PAGEOBJ_PATH, pdfium.raw.FPDF_PAGEOBJ_SHADING]
    used = 0
    try:
        with PDF_RENDER_LOCK, pdfium.PdfDocument(source) as document, output.open("w", encoding="utf-8", newline="\n") as md:
            count = len(document)
            if not count:
                raise JobError("EMPTY_DOCUMENT", "PDF 没有可读取的页面。")
            document.init_forms()
            for number in range(1, count + 1):
                with closing(document[number - 1]) as page, closing(page.get_textpage()) as text_page:
                    text = text_page.get_text_bounded(errors="replace").replace("\r\n", "\n").replace("\r", "\n").strip("\n")
                    if (not text.strip() or "\ufffd" in text or pdfium.raw.FPDFPage_GetAnnotCount(page)
                            or next(page.get_objects(filter=graphics), None) is not None):
                        visual_pages.append(number)
                    width, height = page.get_size()
                    if not all(math.isfinite(side) and side > 0 for side in (width, height)):
                        raise JobError("INVALID_DOCUMENT", f"PDF 第 {number} 页的尺寸无效。")
                    relative = f"assets/page-{number:04d}.png"
                    scale = min(2.5, 2000 / max(width, height))
                    with closing(page.render(scale=scale, draw_annots=True)) as bitmap:
                        bitmap.to_pil().save(folder / relative, format="PNG")
                if text.strip():
                    # Literal blocks preserve customer tags and Markdown-looking source text.
                    body = "\n".join("    " + line for line in text.split("\n"))
                else:
                    body = "本页无可提取的文字，内容见原页图。"
                section = f"## 第 {number} 页\n\n{body}\n\n![第 {number} 页原图]({relative})\n\n"
                image_size = (folder / relative).stat().st_size
                used += len(section.encode("utf-8")) + len("data:image/png;base64,") + 4 * ((image_size + 2) // 3)
                if used > MAX_DELIVERY:
                    raise JobError("LIMIT_EXCEEDED", "含 PDF 页图的 Markdown 交付内容超过 96 MiB。")
                md.write(section)
    except pdfium.PdfiumError as exc:
        if exc.err_code == pdfium.raw.FPDF_ERR_PASSWORD:
            raise JobError("INVALID_DOCUMENT", "PDF 需要打开密码，请提供可直接打开的副本。") from exc
        raise JobError("INVALID_DOCUMENT", "无法完整读取 PDF，请检查文件是否损坏。") from exc
    write_json(folder / "pdf-pages.json", {"page_count": count, "visual_pages": visual_pages})
    return output


def transcribe(source, folder):
    folder.mkdir(parents=True, exist_ok=True)
    suffix = source.suffix.lower()
    if suffix == ".pdf":
        return transcribe_pdf(source, folder)
    docx = source
    if suffix == ".doc":
        with source.open("rb") as stream:
            if stream.read(8) != bytes.fromhex("d0cf11e0a1b11ae1"):
                raise JobError("INVALID_DOCUMENT", "文件不是有效的旧版 Word 文档。")
        docx = office_convert(source, folder / "converted", "docx")
    elif suffix == ".xlsx":
        from .material_spreadsheet import workbook_view
        docx = workbook_view(source, folder, MAX_PACKAGE, MAX_DELIVERY)
    elif suffix != ".docx":
        raise JobError("UNSUPPORTED_DOCUMENT", "没有该文件类型的转写工具。")
    docx = checked_docx(docx, folder)
    output = folder / "source.md"
    run_converter([pandoc_executable(), "-f", "docx", "-t", "gfm", "--wrap=none",
                   "--sandbox", "--extract-media=assets", "-o", str(output), str(docx)], folder)
    if not output.is_file() or not output.read_text(encoding="utf-8").strip():
        raise JobError("EMPTY_DOCUMENT", "未提取到可交付的 Markdown 内容。")
    return output


def standalone_markdown(folder, remaining=MAX_DELIVERY):
    text = (folder / "source.md").read_text(encoding="utf-8")
    assets = (folder / "assets").resolve()
    used = len(text.encode("utf-8"))
    if used > remaining:
        raise JobError("LIMIT_EXCEEDED", "Markdown 交付内容超过 96 MiB。")
    def inline(match):
        nonlocal used
        path = (folder / unquote(match["path"])).resolve()
        if not path.is_relative_to(assets) or not path.is_file():
            raise JobError("MISSING_MEDIA", "Markdown 中的图片缺少对应文件。")
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        used += 4 * ((path.stat().st_size + 2) // 3) + len(mime) + 14
        if used > remaining:
            raise JobError("LIMIT_EXCEEDED", "含图片的 Markdown 交付内容超过 96 MiB。")
        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
        return match["prefix"] + "data:" + mime + ";base64," + encoded
    # PDF text is literal, even when it contains an example link to assets/.
    references = PDF_PAGE_IMAGE if (folder / "pdf-pages.json").is_file() else MEDIA_URL
    return references.sub(inline, text)


def page_preview(source, folder, page):
    if source.suffix.lower() == ".pdf":
        count = json.loads((folder / "pdf-pages.json").read_text(encoding="utf-8"))["page_count"]
        if type(page) is not int or not 1 <= page <= count:
            raise ValueError(f"页码应在 1 到 {count} 之间。")
        data = (folder / "assets" / f"page-{page:04d}.png").read_bytes()
        return "data:image/png;base64," + base64.b64encode(data).decode("ascii"), count
    import pypdfium2 as pdfium
    docx = folder / "embedded.docx"
    preview_source = folder / "spreadsheet.docx" if source.suffix.lower() == ".xlsx" else source
    pdf = office_convert(docx if docx.exists() else preview_source, folder / "preview", "pdf")
    with PDF_RENDER_LOCK, pdfium.PdfDocument(str(pdf)) as document:
        if type(page) is not int or not 1 <= page <= len(document):
            raise ValueError(f"页码应在 1 到 {len(document)} 之间。")
        obj = document[page-1]
        try:
            bitmap = obj.render(scale=min(2, 1500 / max(obj.get_size())))
            try:
                buf = io.BytesIO()
                bitmap.to_pil().convert("RGB").save(buf, format="JPEG", quality=85)
                return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("ascii"), len(document)
            finally:
                bitmap.close()
        finally:
            obj.close()
