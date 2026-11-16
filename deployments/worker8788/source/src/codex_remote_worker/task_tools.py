"""Task-local UTF-8 file tools. Executed by the configured tool Python, not a model."""
from __future__ import annotations

import argparse
from bisect import bisect_right
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid
from zipfile import ZipFile

JSON_CONTRACT = 'siemens-json/1'
JSON_CONTRACTS = (JSON_CONTRACT, 'siemens-json/2')
READ_OUTPUT_BYTES = 8000  # Leave room below the CLI's observed 10 KB tool-output cap.
CONTEXT_VERSION = 'worker-context/1'

csv.field_size_limit(32 * 1024**2)


def json_text(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def output_bytes(value):
    return (json_text(value) + '\n').encode('utf-8')


def output_budget(value):
    if not 512 <= value <= READ_OUTPUT_BYTES:
        raise ValueError('Output budget must be 512..8000 UTF-8 bytes')
    return value


def bounded_text(text, start, stop, build, budget):
    """Largest complete Unicode prefix whose entire JSON envelope fits."""
    low, high = start, min(stop, start + budget)
    while low < high:
        middle = (low + high + 1) // 2
        if len(output_bytes(build(middle))) <= budget:
            low = middle
        else:
            high = middle - 1
    if low == start:
        raise ValueError('Output budget cannot fit a content character and metadata')
    return build(low)


def atomic_bytes(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        temporary.write_bytes(data)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def csv_fields(text):
    """Locate raw CSV cells, including quoted commas, escapes and newlines."""
    records, row = [], []
    start = index = 0
    quoted = False
    while index < len(text):
        char = text[index]
        if char == '"':
            if quoted and index + 1 < len(text) and text[index + 1] == '"':
                index += 2
                continue
            if quoted:
                quoted = False
            elif index == start:
                quoted = True
        elif not quoted and char == ',':
            row.append((start, index))
            start = index + 1
        elif not quoted and char in '\r\n':
            row.append((start, index))
            records.append(row)
            row = []
            index += 2 if char == '\r' and text[index:index + 2] == '\r\n' else 1
            start = index
            continue
        index += 1
    if quoted:
        raise ValueError('CSV has an unterminated quoted cell')
    if start < len(text) or row:
        row.append((start, len(text)))
        records.append(row)
    return records


def quoted_cell(value):
    if any(char in value for char in ',"\r\n'):
        return '"' + value.replace('"', '""') + '"'
    return value


class TaskTools:
    def __init__(self, manifest):
        self.manifest_path = Path(manifest).resolve()
        self.meta = json.loads(self.manifest_path.read_text(encoding='utf-8-sig'))
        self.root = self.manifest_path.parent.parent.resolve()
        if Path(self.meta['work_root']).resolve() != self.root:
            raise ValueError('Task manifest root mismatch')
        self.snapshot = json.loads(self.path(self.meta['snapshot']).read_text(encoding='utf-8-sig'))

    def path(self, value):
        path = Path(value)
        if not path.is_absolute():
            path = self.root / path
        path = path.resolve()
        if not path.is_relative_to(self.root):
            raise ValueError('Task path is outside the workspace')
        return path

    def prepare(self):
        result = {}
        for role in ('project_style', 'general_style'):
            self.step = 'prepare:' + role
            entry = self.meta['styles'][role]
            output = self.path(entry['text_path'])
            if entry.get('cached') and output.is_file():
                text = output.read_text(encoding='utf-8-sig')
            else:
                source = self.path(entry['source_path'])
                suffix = source.suffix.lower()
                if suffix in ('.txt', '.md'):
                    text = source.read_text(encoding='utf-8-sig')
                elif suffix == '.docx':
                    with ZipFile(source) as archive:
                        entries = archive.infolist()
                        if len(entries) > 10000 or sum(item.file_size for item in entries) > 256 * 1024**2:
                            raise ValueError('DOCX expansion exceeds limit')
                    from docx import Document
                    document = Document(source)
                    chunks = [p.text for p in document.paragraphs]
                    for table in document.tables:
                        chunks.extend('\t'.join(cell.text for cell in row.cells) for row in table.rows)
                    text = '\n'.join(chunks)
                elif suffix == '.doc':
                    directory = output.parent / ('convert-' + role)
                    directory.mkdir(parents=True, exist_ok=True)
                    profile = directory / 'profile'
                    command = [self.meta['doc_converter'], '-env:UserInstallation=' + profile.as_uri(),
                               '--headless', '--nologo', '--nodefault', '--norestore',
                               '--convert-to', 'txt:Text (encoded):UTF8', '--outdir', str(directory), str(source)]
                    done = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                          stderr=subprocess.PIPE, timeout=35, check=False)
                    converted = directory / (source.stem + '.txt')
                    if done.returncode != 0 or not converted.is_file():
                        raise ValueError('DOC conversion failed')
                    text = converted.read_text(encoding='utf-8-sig')
                else:
                    raise ValueError('Unsupported guide type')
            if not text.strip():
                raise ValueError('Prepared guide is empty')
            encoded = text.encode('utf-8')
            if len(encoded) > self.meta['max_guide_bytes']:
                raise ValueError('Prepared guide exceeds limit')
            atomic_bytes(output, encoded)
            result[role] = {'text_path': str(output), 'bytes': len(encoded),
                            'sha256': hashlib.sha256(encoded).hexdigest(), 'cached': bool(entry.get('cached'))}
        report = {'styles': result, 'rows': len(self.snapshot['rows']), 'context': self.prepare_context()}
        atomic_bytes(self.manifest_path.with_name('prepared.json'), json.dumps(report, ensure_ascii=False).encode('utf-8'))
        return report

    def inspect(self):
        if self.snapshot.get('contract') in JSON_CONTRACTS:
            result = {'work_root': str(self.root), 'input_contract': self.snapshot['contract'],
                    'phase': self.snapshot['phase'], 'rows': len(self.snapshot['rows']),
                    'source_path': str(self.path(self.meta['source_path'])),
                    'styles': self.meta['styles'], 'delivery_mode': 'final_message_json'}
            if self.snapshot['contract'] == 'siemens-json/2':
                result.update(project_name=self.snapshot['project_name'], request_id=self.snapshot['request_id'],
                              model_rows=len(self.snapshot['rows']), tm_reused_rows=len(self.snapshot['tm_reuse']))
            return result
        return {'work_root': str(self.root), 'headers': self.snapshot['headers'],
                'rows': len(self.snapshot['rows']), 'styles': self.meta['styles'],
                'translations_path': str(self.path(self.meta['translations'])),
                'output_csv': str(self.path(self.meta['output_csv']))}

    def source(self, offset=0, limit=20):
        if offset < 0 or limit < 1 or limit > 100:
            raise ValueError('Use non-negative offset and limit 1..100')
        if self.snapshot.get('contract') in JSON_CONTRACTS:
            return self.snapshot['rows'][offset:offset + limit]
        selected = []
        for record, row in zip(self.snapshot['records'][offset:offset + limit], self.snapshot['rows'][offset:offset + limit]):
            selected.append({**record, 'fields': dict(zip(self.snapshot['headers'], row))})
        return selected

    def read(self, role, offset=0, limit=200, max_output_bytes=READ_OUTPUT_BYTES, char_offset=0):
        """Line pagination, with a character cursor for a single oversized line."""
        budget = output_budget(max_output_bytes)
        if role not in ('project_style', 'general_style') or offset < 0 or limit < 1 or char_offset < 0:
            raise ValueError('Choose a guide, non-negative cursor and positive line limit')
        with self.path(self.meta['styles'][role]['text_path']).open(encoding='utf-8-sig', newline='') as stream:
            text = stream.read()
        positions = [0]
        for line in text.splitlines(keepends=True):
            positions.append(positions[-1] + len(line))
        total = len(positions) - 1
        if offset > total or (offset == total and char_offset) or (offset < total and char_offset >= positions[offset+1]-positions[offset]):
            raise ValueError('Guide cursor is outside the text')
        start = positions[offset] + char_offset
        stop = positions[min(offset + limit, total)]
        def build(end):
            next_offset = bisect_right(positions, end) - 1
            return {'kind': 'guide', 'role': role, 'offset': offset, 'char_offset': char_offset,
                    'next_offset': next_offset, 'next_char_offset': end - positions[next_offset],
                    'total_lines': total, 'has_more': end < len(text), 'text': text[start:end]}
        if start == stop:
            result = build(stop)
            if len(output_bytes(result)) > budget:
                raise ValueError('Guide metadata exceeds output budget')
            return result
        return bounded_text(text, start, stop, build, budget)

    def source_page(self, offset=0, limit=20, max_output_bytes=READ_OUTPUT_BYTES, char_offset=0):
        budget = output_budget(max_output_bytes)
        total = len(self.snapshot['rows'])
        if offset < 0 or offset > total or not 1 <= limit <= 100 or char_offset < 0 or (offset == total and char_offset):
            raise ValueError('Source cursor or row limit is invalid')
        def page(rows, end):
            return {'kind': 'source', 'offset': offset, 'next_offset': end, 'next_char_offset': 0,
                    'total_rows': total, 'has_more': end < total, 'rows': rows}
        selected = self.source(offset, limit)
        rows = []
        if not char_offset:
            for row in selected:
                if len(output_bytes(page(rows + [row], offset + len(rows) + 1))) > budget:
                    break
                rows.append(row)
            if rows or not selected:
                result = page(rows, offset + len(rows))
                if len(output_bytes(result)) > budget:
                    raise ValueError('Source metadata exceeds output budget')
                return result
        # Preserve even a single huge business record; never silently drop fields.
        text = json_text(selected[0])
        if char_offset >= len(text):
            raise ValueError('Source character cursor is outside the row')
        def fragment(end):
            complete = end == len(text)
            result = page([], offset + int(complete))
            result.update(next_char_offset=0 if complete else end,
                          fragment={'format': 'json', 'row_index': offset, 'char_offset': char_offset,
                                    'end_char_offset': end, 'total_chars': len(text), 'text': text[char_offset:end]})
            return result
        return bounded_text(text, char_offset, len(text), fragment, budget)

    def prepare_context(self):
        """Combine task metadata, complete guides and model rows into bounded pages."""
        budget = READ_OUTPUT_BYTES
        info = self.inspect()
        task = {key: info[key] for key in ('input_contract', 'phase', 'rows', 'project_name',
                'request_id', 'model_rows', 'tm_reused_rows', 'delivery_mode') if key in info}
        task.setdefault('input_contract', 'table-csv/1')
        pages = []
        def empty():
            # Reserve digits before the final count is known.
            return {'kind': 'context', 'version': CONTEXT_VERSION, 'page': len(pages),
                    'page_count': 999999999, 'task': task, 'parts': []}
        current = empty()
        def flush():
            nonlocal current
            if current['parts']:
                pages.append(current)
                current = empty()
        def add_text(kind, text, **metadata):
            nonlocal current
            start = 0
            while start < len(text):
                def build(end):
                    part = {'kind': kind, **metadata, 'offset': start, 'end_offset': end,
                            'total_chars': len(text), 'text': text[start:end]}
                    return {**current, 'parts': current['parts'] + [part]}
                try:
                    candidate = bounded_text(text, start, len(text), build, budget)
                except ValueError:
                    if not current['parts']:
                        raise
                    flush()
                    continue
                current = candidate
                start = current['parts'][-1]['end_offset']
                if start < len(text):
                    flush()
        for role in ('project_style', 'general_style'):
            with self.path(self.meta['styles'][role]['text_path']).open(encoding='utf-8-sig', newline='') as stream:
                add_text('guide', stream.read(), role=role)
        for offset in range(0, len(self.snapshot['rows']), 100):
            for index, row in enumerate(self.source(offset, 100), offset):
                part = {'kind': 'source', 'row_index': index, 'row': row}
                candidate = {**current, 'parts': current['parts'] + [part]}
                if len(output_bytes(candidate)) <= budget:
                    current = candidate
                    continue
                flush()
                candidate = {**current, 'parts': [part]}
                if len(output_bytes(candidate)) <= budget:
                    current = candidate
                else:
                    add_text('source_fragment', json_text(row), row_index=index, format='json')
        flush()
        directory = self.manifest_path.parent / 'context'
        entries = []
        for number, page in enumerate(pages):
            page['page_count'] = len(pages)
            encoded = output_bytes(page)
            if len(encoded) > budget:
                raise ValueError('Context page exceeds output budget')
            filename = f'{number:06d}.json'
            atomic_bytes(directory / filename, encoded)
            entries.append({'page': number, 'filename': filename, 'bytes': len(encoded),
                            'sha256': hashlib.sha256(encoded).hexdigest()})
        index = {'version': CONTEXT_VERSION, 'page_count': len(pages), 'max_output_bytes': budget, 'pages': entries}
        atomic_bytes(directory / 'index.json', output_bytes(index))
        return {key: index[key] for key in ('version', 'page_count', 'max_output_bytes')}

    def context(self, page=0):
        directory = self.manifest_path.parent / 'context'
        index = json.loads((directory / 'index.json').read_text(encoding='utf-8'))
        if page < 0 or page >= index['page_count']:
            raise ValueError('Context page is outside the prepared input')
        entry = index['pages'][page]
        data = self.path(directory / entry['filename']).read_bytes()
        if len(data) != entry['bytes'] or len(data) > READ_OUTPUT_BYTES or hashlib.sha256(data).hexdigest() != entry['sha256']:
            raise ValueError('Prepared context integrity check failed')
        return json.loads(data)

    def build(self, translations_path=None):
        if self.snapshot.get('contract') in JSON_CONTRACTS:
            raise ValueError('JSON tasks deliver the final JSON message; CSV build is not applicable')
        translations_path = self.path(translations_path or self.meta['translations'])
        values = json.loads(translations_path.read_text(encoding='utf-8-sig'))
        if isinstance(values, dict):
            ids = [r['id'] for r in self.snapshot['records']]
            if set(values) != set(ids):
                raise ValueError('Translation IDs must match every source row exactly')
            values = [values[key] for key in ids]
        if (not isinstance(values, list) or len(values) != len(self.snapshot['rows'])
                or not all(isinstance(value, str) for value in values)):
            raise ValueError('Provide one translation string per source row')
        source = self.path(self.meta['source_path'])
        target = self.snapshot['headers'].index('zh-CN')
        raw_preserved = False
        if source.suffix.lower() == '.csv':
            raw = source.read_bytes()
            text = raw.decode('utf-8-sig')
            actual = list(csv.reader(io.StringIO(text, newline=''), strict=True))
            if actual[0] == self.snapshot['headers']:
                spans = csv_fields(text)
                if len(spans) != len(values) + 1 or any(len(row) != len(actual[0]) for row in spans):
                    raise ValueError('Source CSV field mapping is inconsistent')
                for row, value in reversed(list(zip(spans[1:], values))):
                    start, end = row[target]
                    text = text[:start] + quoted_cell(value) + text[end:]
                result = (b'\xef\xbb\xbf' if raw.startswith(b'\xef\xbb\xbf') else b'') + text.encode('utf-8')
                raw_preserved = True
        if not raw_preserved:
            stream = io.StringIO(newline='')
            writer = csv.writer(stream, lineterminator='\r\n')
            writer.writerow(self.snapshot['headers'])
            for original, value in zip(self.snapshot['rows'], values):
                row = ['' if item is None else str(item) for item in original]
                row[target] = value
                writer.writerow(row)
            result = b'\xef\xbb\xbf' + stream.getvalue().encode('utf-8')
        output = self.path(self.meta['output_csv'])
        if len(result) > self.meta['max_output_bytes']:
            raise ValueError('Output exceeds limit')
        atomic_bytes(output, result)
        # Validate field values independently of the raw-cell preservation path.
        parsed = list(csv.reader(io.StringIO(result.decode('utf-8-sig'), newline=''), strict=True))
        expected = [[('' if v is None else str(v)) for v in row] for row in self.snapshot['rows']]
        for row, value in zip(expected, values):
            row[target] = value
        if parsed != [self.snapshot['headers'], *expected]:
            output.unlink(missing_ok=True)
            raise ValueError('Built output failed structural round-trip')
        return {'output_csv': str(output), 'rows': len(values), 'original_fields_unchanged': True,
                'non_target_csv_bytes_preserved': raw_preserved, 'bytes': len(result)}


def main():
    parser = argparse.ArgumentParser(description='Task-local UTF-8 file operations')
    parser.add_argument('command', choices=('prepare', 'inspect', 'source', 'read', 'context', 'build', 'smoke'))
    parser.add_argument('argument', nargs='?')
    parser.add_argument('--offset', type=int, default=0)
    parser.add_argument('--limit', type=int)
    parser.add_argument('--char-offset', type=int, default=0)
    parser.add_argument('--max-output-bytes', type=int, default=READ_OUTPUT_BYTES)
    parser.add_argument('--page', type=int, default=0)
    parser.add_argument('--manifest', type=Path, default=Path(__file__).with_name('manifest.json'))
    args = parser.parse_args()
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='strict', newline='\n')
    tools = None
    try:
        tools = TaskTools(args.manifest)
        if args.command == 'prepare':
            result = tools.prepare()
        elif args.command == 'inspect':
            result = tools.inspect()
        elif args.command == 'source':
            result = tools.source_page(args.offset, args.limit if args.limit is not None else 20,
                                       args.max_output_bytes, args.char_offset)
        elif args.command == 'read':
            result = tools.read(args.argument, args.offset, args.limit if args.limit is not None else 200,
                                args.max_output_bytes, args.char_offset)
        elif args.command == 'context':
            result = tools.context(args.page)
        elif args.command == 'smoke':
            prepared = tools.prepare()
            if tools.snapshot.get('contract') in JSON_CONTRACTS:
                result = tools.inspect()
                result['marker'] = 'RUNTIME_JSON_HELPER_OK'
            else:
                values = ['中文测试 ' + str(index) for index in range(len(tools.snapshot['rows']))]
                atomic_bytes(tools.path(tools.meta['translations']), json.dumps(values, ensure_ascii=False).encode('utf-8'))
                result = tools.build()
                result['marker'] = 'RUNTIME_HELPER_OK'
            result['context'] = prepared['context']
            result['utf8'] = '中文测试'
        else:
            result = tools.build(args.argument)
        print(json_text(result))
        return 0
    except Exception as error:
        # Never dump input rows, arbitrary exception strings, or process output.
        print(json.dumps({'error': 'TASK_FILE_OPERATION_FAILED', 'exception_type': type(error).__name__,
                          'step': getattr(tools, 'step', args.command)}), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
