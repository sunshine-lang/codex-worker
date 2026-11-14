"""Siemens checks adapted from the existing local workflow; no model calls."""
import csv
import re
from collections import Counter
from pathlib import Path
from zipfile import ZipFile, BadZipFile

from .common import JobError

def make_records(headers, rows, source_column='en', target_column='zh-CN'):
    if len(headers) != len(set(headers)) or any(not isinstance(h, str) or not h.strip() for h in headers):
        raise ValueError('表头必须为非空且不重复的文本。')
    if source_column not in headers:
        raise ValueError(f'找不到源文列：{source_column}')
    headers = list(headers)
    rows = [list(row) for row in rows]
    if target_column not in headers:
        headers.append(target_column)
        rows = [row + [None] for row in rows]
    source_index, target_index = headers.index(source_column), headers.index(target_column)
    if any(len(row) != len(headers) for row in rows):
        raise ValueError('数据行列数与表头不一致。')
    tm_indexes = [i for i, name in enumerate(headers) if name.startswith('TM参考')]
    tb_index = headers.index('生效术语') if '生效术语' in headers else None
    context_index = headers.index('Context') if 'Context' in headers else None
    records = []
    for i, row in enumerate(rows):
        source = '' if row[source_index] is None else str(row[source_index])
        tb = row[tb_index] if tb_index is not None else ''
        terms = []
        for entry in str(tb or '').split(' | '):
            match = re.fullmatch(r'(.+) \((.+)\)', entry.strip())
            if match:
                terms.append({'target': match[1], 'source': match[2]})
        context = str(row[context_index] or '') if context_index is not None else ''
        records.append({'id': f'row-{i+2}', 'excel_row': i+2, 'source': source,
                        'context': context, 'context_origin': 'provided' if context else 'adjacent_rows',
                        'terms': terms, 'tm': [str(row[j]) for j in tm_indexes if row[j]],
                        'original_target': row[target_index]})
    return headers, rows, records

def check_row(record, translation):
    en = record['source']
    issues = []
    def add(code, message, severity='error'):
        issues.append({'id':record['id'], 'excel_row':record['excel_row'], 'rule':code, 'severity':severity, 'message':message})
    if en.strip() and not translation.strip():
        add('empty', '缺少译文。')
    spans = []
    for term in sorted(record['terms'], key=lambda t:len(t['source']), reverse=True):
        for match in re.finditer(r'(?<![A-Za-z0-9])'+re.escape(term['source'])+r'(?![A-Za-z0-9])',en,re.I):
            if any(a <= match.start() and b >= match.end() for a,b in spans):
                continue
            spans.append(match.span())
            # A spaced slash declares glossary alternatives. Bare slashes, e.g.
            # 输入/输出, remain part of one exact term rather than two alternatives.
            targets = [value.strip() for value in re.split(r'\s+/\s+', term['target']) if value.strip()]
            def present(target):
                prefix = r'(?<![A-Za-z0-9])' if re.match(r'[A-Za-z0-9]', target) else ''
                suffix = r'(?![A-Za-z0-9])' if re.search(r'[A-Za-z0-9]$', target) else ''
                return re.search(prefix + re.escape(target) + suffix, translation) is not None
            if not any(present(target) for target in targets):
                add('term', f"缺少生效术语：{term['source']} → {term['target']}")
    placeholders = r'\{[^{}]*\}|%(?:\d+\$)?[sdif]|<[^<>]+>'
    if Counter(re.findall(placeholders,en)) != Counter(re.findall(placeholders,translation)):
        add('placeholder', '占位符或标签发生变化。')
    footnotes = lambda t: Counter(re.findall(r'\b\d+\)', re.sub(r'\([^()]*\)', '', t)))
    if footnotes(en) != footnotes(translation):
        add('footnote', '脚注标记发生变化。')
    source_numbers = re.sub(r'\b\d+(?:st|nd|rd|th)\b', '', en, flags=re.I)
    numbers = lambda t: Counter(re.findall(r'\d+(?:\.\d+)*',t))
    missing = numbers(source_numbers) - numbers(translation)
    if missing:
        add('number', '缺少原文数值：' + ', '.join(missing))
    negatives = lambda t: Counter(re.findall(r'(?:^|[：:(（,/])\s*-\s*(\d+(?:\.\d+)?)',t))
    if negatives(en) - negatives(translation):
        add('number_sign', '原文负数的负号丢失。')
    # A voltage range is a quantity, not a product identifier; keep numeric QA above.
    voltage = r'(?<![A-Za-z0-9])(\d+(?:\.\d+)?)\s*(kV|mV|V)\s*\.{2,}\s*(\d+(?:\.\d+)?)(?:\s*(kV|mV|V))?(?![A-Za-z0-9])'
    identifier_source = re.sub(voltage, lambda m: f"{m[1]} {m[2]} ... {m[3]} {m[4] or ''}", source_numbers)
    for match in re.finditer(voltage, source_numbers):
        unit = match[2]
        chinese = {'V': '伏', 'kV': '千伏', 'mV': '毫伏'}[unit]
        if not re.search(r'(?<![A-Za-z])'+re.escape(unit)+r'(?![A-Za-z])',translation) and chinese not in translation:
            add('unit', '电压范围缺少单位：'+unit)
    tokens = re.findall(r'(?<!\w)[A-Za-z0-9]+(?:[-.][A-Za-z0-9.]+)*(?!\w)', identifier_source)
    codes = [t for t in tokens if re.search('[A-Za-z]',t) and re.search(r'\d',t)]
    for code, count in Counter(codes).items():
        if len(re.findall(r'(?<![A-Za-z0-9])'+re.escape(code)+r'(?![A-Za-z0-9])',translation)) != count:
            add('identifier', '型号、版本或标识发生变化：' + code)
    for token in re.findall(r'[A-Za-z0-9_.+-]+@[A-Za-z0-9.-]+|6SL[\w.\-]+', en):
        if token not in translation:
            add('identifier', '订货号或邮箱发生变化：' + token)
    if re.search(r'[\u4e00-\u9fff][A-Za-z0-9]|[A-Za-z0-9][\u4e00-\u9fff]',translation):
        add('spacing','中文与英文/数字之间缺少空格。')
    if re.search(r'[\u4e00-\u9fff]',translation) and re.search(r'[,;"\u0027]',translation):
        add('punctuation','中文语境中出现英文逗号、分号或直引号。')
    if translation.count('“') != translation.count('”') or translation.count('（') != translation.count('）'):
        add('punctuation','中文引号或括号不配对。')
    if re.search(r'\b\d+\s+Temp\.\s+\d+',en):
        add('source_ambiguity','原文温度输入标记含混，需核对源文；不得擅自删除或猜测第二个数字。','warning')
    return issues


def rows_from_file(path: Path, *, max_rows=10000, max_columns=256):
    rows = []

    def append(values):
        if len(values) > max_columns or len(rows) >= max_rows + 1:
            raise JobError('LIMIT_EXCEEDED', '表格行数或列数超过限制。')
        rows.append(['' if value is None else str(value) for value in values])

    try:
        if path.suffix.lower() == '.xlsx':
            with ZipFile(path) as archive:
                entries = archive.infolist()
                if (len(entries) > 10000 or sum(i.file_size for i in entries) > 256 * 1024**2
                        or any(i.filename.lower().endswith('vbaproject.bin') for i in entries)):
                    raise JobError('INVALID_WORKBOOK', '工作簿过大或包含宏。')
            from openpyxl import load_workbook
            book = load_workbook(path, read_only=True, data_only=False, keep_links=False)
            try:
                if len(book.worksheets) != 1:
                    raise JobError('INVALID_WORKBOOK', '首版只支持一个工作表。')
                sheet = book.active
                if (sheet.max_row or 0) > max_rows + 1 or (sheet.max_column or 0) > max_columns:
                    raise JobError('LIMIT_EXCEEDED', '工作表声明的行列规模超过限制。')
                for row in sheet.iter_rows(values_only=True):
                    append(row)
            finally:
                book.close()
        else:
            csv.field_size_limit(32 * 1024**2)
            with path.open(encoding='utf-8-sig', newline='') as stream:
                for row in csv.reader(stream, strict=True):
                    append(row)
    except JobError:
        raise
    except (ValueError, UnicodeError, BadZipFile, csv.Error, OSError, KeyError) as exc:
        raise JobError('INVALID_TABLE', '表格不是可读取的单工作表 XLSX 或 UTF-8 CSV。') from exc
    if len(rows) < 2:
        raise JobError('INVALID_TABLE', '表格没有数据行。')
    return rows


def read_source(path: Path):
    if path.suffix.lower() == '.json':
        from .json_delivery import read_json_source
        return read_json_source(path)
    values = rows_from_file(path)
    try:
        headers, rows, records = make_records(values[0], values[1:])
    except ValueError as exc:
        raise JobError('INVALID_TABLE', str(exc)) from exc
    return {'headers': headers, 'rows': rows, 'records': records}


def check_csv(snapshot: dict, path: Path):
    output = rows_from_file(path)
    if output[0] != snapshot['headers'] or len(output) != len(snapshot['rows']) + 1:
        raise JobError('INVALID_ARTIFACT', '译文的字段、字段顺序或行数与原表不符。')
    target = snapshot['headers'].index('zh-CN')
    issues = []
    for row, original, record in zip(output[1:], snapshot['rows'], snapshot['records']):
        if len(row) != len(snapshot['headers']):
            raise JobError('INVALID_ARTIFACT', f"第 {record['excel_row']} 行列数不完整。")
        for index, value in enumerate(original):
            if index != target and row[index] != ('' if value is None else str(value)):
                raise JobError('INVALID_ARTIFACT', f"第 {record['excel_row']} 行原始字段被修改。")
        issues.extend(check_row(record, row[target]))
    return {'rows': len(snapshot['rows']), 'errors': sum(i['severity'] == 'error' for i in issues),
            'warnings': sum(i['severity'] == 'warning' for i in issues), 'issues': issues,
            'original_fields_unchanged': True, 'language_quality': 'not_independently_verified'}
