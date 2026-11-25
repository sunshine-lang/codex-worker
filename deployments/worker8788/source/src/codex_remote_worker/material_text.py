"""Transcribe plain text and delimited records without interpreting source markup."""
import codecs
import csv
import io
import re
from itertools import islice

from .common import JobError, write_json


def decode_text(raw):
    if raw.startswith((codecs.BOM_UTF32_LE, codecs.BOM_UTF32_BE)):
        encodings = ("utf-32",)
    elif raw.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        encodings = ("utf-16",)
    elif raw.startswith(codecs.BOM_UTF8):
        encodings = ("utf-8-sig",)
    else:
        encodings = ("utf-8", "gb18030")
    for encoding in encodings:
        try:
            text = raw.decode(encoding)
        except UnicodeDecodeError:
            continue
        if re.search(r"[\x00-\x08\x0b\x0e-\x1f\x7f-\x9f]", text):
            break
        if not text.strip():
            raise JobError("EMPTY_DOCUMENT", "文本附件中没有可读取的内容。")
        return text.replace("\r\n", "\n").replace("\r", "\n"), encoding
    raise JobError("UNSUPPORTED_DOCUMENT", "无法识别附件内容或文本编码。文本可另存为 UTF-8 后提交。")


def csv_delimiter(text, suffix):
    if suffix == ".txt":
        return None
    sample = text[:8192]
    try:
        delimiter = csv.Sniffer().sniff(sample, delimiters=",;\t").delimiter
    except csv.Error:
        return "," if suffix == ".csv" else None
    if suffix == ".csv":
        return delimiter
    # Treat an unfamiliar name as a table only when multiple records agree.
    reader = csv.reader(io.StringIO(sample, newline=""), delimiter=delimiter)
    widths = [len(row) for row in islice(reader, 8) if row]
    if len(widths) >= 2 and widths[0] > 1 and len(set(widths)) == 1:
        return delimiter
    return None


def literal_block(text, language):
    closers = re.finditer(r"(?m)^ {0,3}(`{3,})[ \t]*$", text)
    fence = "`" * (1 + max((len(match[1]) for match in closers), default=2))
    return f"{fence}{language}\n{text}\n{fence}\n"


def text_markdown(source, folder, limit):
    text, encoding = decode_text(source.read_bytes())
    delimiter = csv_delimiter(text, source.suffix.lower())
    output = folder / "source.md"
    info = {"format": "csv" if delimiter else "txt", "encoding": encoding}
    used = 0
    with output.open("w", encoding="utf-8", newline="\n") as md:
        def write(block):
            nonlocal used
            used += len(block.encode("utf-8"))
            if used > limit:
                raise JobError("LIMIT_EXCEEDED", "文本材料转写后超过 Markdown 交付大小限制。")
            md.write(block)

        if delimiter is None:
            write(literal_block(text, "text"))
        else:
            csv.field_size_limit(max(csv.field_size_limit(), limit))
            with io.StringIO(text, newline="") as stream:
                reader = csv.reader(stream, delimiter=delimiter, strict=True)
                start, first_line = 0, 1
                try:
                    for index, _row in enumerate(reader, 1):
                        end = stream.tell()
                        last_line = reader.line_num
                        lines = str(first_line) if first_line == last_line else f"{first_line}–{last_line}"
                        write(f"## 记录 {index}（原文件第 {lines} 行）\n\n")
                        # Keep the original quoting, separators, empty cells and embedded newlines.
                        write(literal_block(text[start:end], "csv") + "\n")
                        start, first_line = end, last_line + 1
                except csv.Error as exc:
                    raise JobError("INVALID_DOCUMENT", f"CSV 第 {reader.line_num} 行附近的记录无法解析。") from exc
                info["delimiter"] = delimiter
    write_json(folder / "text-source.json", info)
    return output
