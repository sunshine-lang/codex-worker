import csv
import io
import json
from pathlib import Path
import subprocess
import sys

import pytest

from codex_remote_worker.common import digest
from codex_remote_worker.qa import read_source, check_csv, check_row
from codex_remote_worker.task_runtime import layout
from codex_remote_worker.task_tools import TaskTools, csv_fields


def setup_task(settings, tmp_path, source=b'\xef\xbb\xbfen,zh-CN,reference\r\nHello,,"x,y"\r\n'):
    root = tmp_path / 'task'
    for folder in ('work', 'control', 'logs', 'input'):
        (root / folder).mkdir(parents=True)
    settings.doc_converter.write_bytes(b'converter')
    work = root / 'work'
    (work / 'source.csv').write_bytes(source)
    inputs = {'source_file': {'filename': 'source.csv'}}
    for role in ('project_style', 'general_style'):
        content = ('中文指南 ' + role).encode('utf-8')
        (work / (role + '.md')).write_bytes(content)
        inputs[role] = {'filename': role + '.md', 'sha256': digest(content)}
    snapshot = read_source(work / 'source.csv')
    job = {'inputs': inputs, 'output_filename': 'source_ZH-CN.csv'}
    manifest = layout(settings, job, root, snapshot, cache=False)
    return root, manifest, snapshot


@pytest.mark.parametrize('source', [
    b'\xef\xbb\xbfen,zh-CN,reference\r\n"Hello, world","","a""b"\r\n',
    b'en,zh-CN,reference\n"Hello\nworld",old,"x\ry"\n',
    b'en,zh-CN,reference\r\nHello,,last',
    b'en,zh-CN,reference\nHello,,\n',
])
def test_builder_preserves_untouched_raw_cells(settings, tmp_path, source):
    root, manifest, snapshot = setup_task(settings, tmp_path, source)
    before = source.decode('utf-8-sig')
    original_spans = csv_fields(before)
    translations = root / 'work' / '.worker' / 'translations.json'
    translations.write_text(json.dumps(['中文，含 "引号"\n第二行'], ensure_ascii=False), encoding='utf-8-sig')
    tools = TaskTools(root / 'work' / '.worker' / 'manifest.json')
    result = tools.build()
    assert result['non_target_csv_bytes_preserved']
    output = Path(result['output_csv']).read_bytes()
    assert output.startswith(b'\xef\xbb\xbf') == source.startswith(b'\xef\xbb\xbf')
    after = output.decode('utf-8-sig')
    output_spans = csv_fields(after)
    for index in (0, 2):
        a, b = original_spans[1][index]
        c, d = output_spans[1][index]
        assert before[a:b] == after[c:d]
    assert check_csv(snapshot, Path(result['output_csv']))['original_fields_unchanged']


def test_builder_adds_missing_target_column_and_preserves_values(settings, tmp_path):
    root, manifest, snapshot = setup_task(settings, tmp_path, b'en,reference\nHello,original\n')
    path = root / 'work' / '.worker' / 'translations.json'
    path.write_text(json.dumps({'row-2': '你好'}), encoding='utf-8')
    tools = TaskTools(path.with_name('manifest.json'))
    result = tools.build()
    assert not result['non_target_csv_bytes_preserved']
    with Path(result['output_csv']).open(encoding='utf-8-sig', newline='') as stream:
        assert list(csv.reader(stream)) == [['en', 'reference', 'zh-CN'], ['Hello', 'original', '你好']]


@pytest.mark.parametrize('value', [[], ['one', 'extra'], {'row-9': 'wrong'}, [None]])
def test_incomplete_or_misaligned_translations_are_rejected(settings, tmp_path, value):
    root, manifest, snapshot = setup_task(settings, tmp_path)
    path = root / 'work' / '.worker' / 'translations.json'
    path.write_text(json.dumps(value), encoding='utf-8')
    with pytest.raises(ValueError):
        TaskTools(path.with_name('manifest.json')).build()
    assert not Path(manifest['output_csv']).exists()


def test_helper_is_independent_of_shell_cwd_and_exposes_original_columns(settings, tmp_path, monkeypatch):
    root, manifest, snapshot = setup_task(settings, tmp_path)
    monkeypatch.chdir(tmp_path)
    tools = TaskTools(root / 'work' / '.worker' / 'manifest.json')
    assert tools.source()[0]['fields']['reference'] == 'x,y'
    prepared = tools.prepare()
    assert prepared['rows'] == 1
    assert '中文指南' in Path(prepared['styles']['general_style']['text_path']).read_text(encoding='utf-8')
    with pytest.raises(ValueError):
        tools.path(tmp_path / 'outside.json')


def test_standalone_builder_accepts_worker_supported_large_cells(settings, tmp_path):
    root, manifest, snapshot = setup_task(settings, tmp_path, b'en,zh-CN,reference\nHello,,' + b'x' * 150000 + b'\n')
    path = root / 'work' / '.worker' / 'translations.json'
    path.write_text(json.dumps(['你好']), encoding='utf-8')
    done = subprocess.run([sys.executable, '-X', 'utf8', str(path.with_name('task_tools.py')), 'build'],
                          cwd=tmp_path, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)
    assert done.returncode == 0, done.stderr.decode('utf-8', errors='replace')
    assert json.loads(done.stdout)['rows'] == 1


@pytest.mark.parametrize('translation', ['交流', 'AC', '交流电流'])
def test_glossary_accepts_a_declared_alternative(translation):
    record = {'id': 'row-2', 'excel_row': 2, 'source': 'AC',
              'terms': [{'source': 'AC', 'target': '交流 / AC / 交流电流'}]}
    assert not any(i['rule'] == 'term' for i in check_row(record, translation))
    assert any(i['rule'] == 'term' for i in check_row(record, '直流'))
    assert any(i['rule'] == 'term' for i in check_row(record, 'BACnet'))


def test_slash_inside_a_single_term_is_not_split():
    record = {'id': 'row-2', 'excel_row': 2, 'source': 'I/O',
              'terms': [{'source': 'I/O', 'target': '输入/输出'}]}
    assert any(i['rule'] == 'term' for i in check_row(record, '输入'))
    assert not any(i['rule'] == 'term' for i in check_row(record, '输入/输出'))


def test_guide_cache_is_content_addressed_and_checks_its_hash(settings, tmp_path):
    root, manifest, snapshot = setup_task(settings, tmp_path)
    entry = manifest['styles']['general_style']
    cache = settings.data_root / 'cache' / 'guide-text' / (entry['cache_key'] + '.json')
    cache.parent.mkdir(parents=True)
    text = '已转换的中文指南'
    cache.write_text(json.dumps({'text': text, 'sha256': digest(text.encode('utf-8'))}), encoding='utf-8')
    job = {'inputs': {'source_file': {'filename': 'source.csv'},
                     **{role: {'filename': role + '.md', 'sha256': digest(('中文指南 ' + role).encode('utf-8'))}
                        for role in ('project_style', 'general_style')}}, 'output_filename': 'source_ZH-CN.csv'}
    hit = layout(settings, job, root, snapshot)
    assert hit['styles']['general_style']['cached'] is True
    cache.write_text(json.dumps({'text': 'tampered', 'sha256': 'wrong'}), encoding='utf-8')
    miss = layout(settings, job, root, snapshot)
    assert miss['styles']['general_style']['cached'] is False
    cache.write_text(json.dumps({'text': 123, 'sha256': 'wrong'}), encoding='utf-8')
    assert layout(settings, job, root, snapshot)['styles']['general_style']['cached'] is False
    job['inputs']['general_style']['sha256'] = 'new-input'
    changed = layout(settings, job, root, snapshot)
    assert changed['styles']['general_style']['cache_key'] != entry['cache_key']
