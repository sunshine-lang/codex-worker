import copy
import json
from pathlib import Path
import subprocess
import sys

import pytest

from codex_remote_worker import task_tools
from codex_remote_worker.task_runtime import runtime_instructions
from codex_remote_worker.task_tools import READ_OUTPUT_BYTES, TaskTools, output_bytes
from test_json_runtime import setup_json_task
from test_task_tools import setup_task


def tools_for(settings, tmp_path):
    root, manifest, data, job = setup_json_task(settings, tmp_path)
    tools = TaskTools(root / 'work/.worker/manifest.json')
    tools.prepare()
    return tools, data


def read_all_source(tools, budget=900):
    offset = char_offset = 0
    result, fragments = [], ''
    for _ in range(10000):
        page = tools.source_page(offset, 100, budget, char_offset)
        assert len(output_bytes(page)) <= budget
        assert (page['next_offset'], page['next_char_offset']) != (offset, char_offset) or not page['has_more']
        result.extend(page['rows'])
        if page.get('fragment'):
            part = page['fragment']
            assert part['char_offset'] == len(fragments)
            fragments += part['text']
            if page['next_char_offset'] == 0:
                result.append(json.loads(fragments))
                fragments = ''
        if not page['has_more']:
            assert not fragments
            return result
        offset, char_offset = page['next_offset'], page['next_char_offset']
    pytest.fail('Source cursor did not reach EOF')


def reconstruct_context(tools):
    index = json.loads((tools.manifest_path.parent / 'context/index.json').read_text(encoding='utf-8'))
    guides, rows, fragments = {}, {}, {}
    for number in range(index['page_count']):
        page = tools.context(number)
        assert page['page'] == number and page['page_count'] == index['page_count']
        assert len(output_bytes(page)) <= READ_OUTPUT_BYTES
        for part in page['parts']:
            if part['kind'] == 'guide':
                previous = guides.get(part['role'], '')
                assert part['offset'] == len(previous)
                guides[part['role']] = previous + part['text']
                assert part['end_offset'] == len(guides[part['role']])
            elif part['kind'] == 'source':
                assert part['row_index'] not in rows
                rows[part['row_index']] = part['row']
            else:
                assert part['kind'] == 'source_fragment'
                key = part['row_index']
                previous = fragments.get(key, '')
                assert part['offset'] == len(previous)
                fragments[key] = previous + part['text']
                if part['end_offset'] == part['total_chars']:
                    assert key not in rows
                    rows[key] = json.loads(fragments.pop(key))
    assert not fragments
    return guides, [rows[i] for i in range(len(rows))], index


def test_guide_pagination_honors_lines_and_preserves_unicode_under_byte_budget(settings, tmp_path):
    tools, _ = tools_for(settings, tmp_path)
    text = ''.join(f'{i}: 中文规则😀 "quoted" \\path\r\n' for i in range(740))
    path = tools.path(tools.meta['styles']['general_style']['text_path'])
    path.write_bytes(text.encode('utf-8'))
    page = tools.read('general_style', 150, 330)
    assert page['text'].startswith('150:')
    assert page['next_offset'] > 150 and page['next_offset'] <= 480
    offset = char_offset = 0
    parts = []
    while True:
        page = tools.read('general_style', offset, 330, 900, char_offset)
        assert len(output_bytes(page)) <= 900
        parts.append(page['text'])
        if not page['has_more']:
            break
        assert (page['next_offset'], page['next_char_offset']) != (offset, char_offset)
        offset, char_offset = page['next_offset'], page['next_char_offset']
    assert ''.join(parts) == text


def test_single_oversized_guide_line_is_not_dropped_or_stuck(settings, tmp_path):
    tools, _ = tools_for(settings, tmp_path)
    text = '长行😀\\"' * 3000
    tools.path(tools.meta['styles']['general_style']['text_path']).write_bytes(text.encode('utf-8'))
    offset = char_offset = 0
    actual = ''
    while True:
        page = tools.read('general_style', offset, 1, 700, char_offset)
        assert len(output_bytes(page)) <= 700
        actual += page['text']
        if not page['has_more']:
            break
        assert page['next_offset'] == 0 and page['next_char_offset'] > char_offset
        offset, char_offset = page['next_offset'], page['next_char_offset']
    assert actual == text


def test_source_pages_preserve_large_business_records_and_global_ids(settings, tmp_path):
    tools, data = tools_for(settings, tmp_path)
    rows = [copy.deepcopy(data['rows'][0]) for _ in range(10)]
    for i, row in enumerate(rows):
        row['row_id'] = str(i * 7 + 3)
        row['record']['business_id'] = {'flag': False, 'null': None, 'code': '000' + str(i)}
    rows[4]['record']['long_reference'] = '业务原文😀\\"\r\n' * 3000
    tools.snapshot['rows'] = rows
    assert read_all_source(tools) == rows
    empty = tools.source_page(len(rows))
    assert empty['rows'] == [] and empty['has_more'] is False


@pytest.mark.parametrize('command,args', [('guide', {'offset': -1}), ('guide', {'limit': 0}),
    ('guide', {'char_offset': 999999}), ('source', {'offset': -1}), ('source', {'offset': 99}),
    ('source', {'limit': 101}), ('source', {'char_offset': -1}),
    ('source', {'max_output_bytes': 100}), ('guide', {'max_output_bytes': 10000})])
def test_invalid_cursors_and_unsafe_output_budgets_are_rejected(settings, tmp_path, command, args):
    tools, _ = tools_for(settings, tmp_path)
    with pytest.raises(ValueError):
        tools.read('general_style', **args) if command == 'guide' else tools.source_page(**args)


@pytest.mark.parametrize('phase', ['translate', 'review'])
def test_context_covers_complete_guides_and_all_model_rows_in_bounded_pages(settings, tmp_path, phase):
    root, manifest, data, job = setup_json_task(settings, tmp_path, phase)
    tools = TaskTools(root / 'work/.worker/manifest.json')
    tools.prepare()
    expected_guides = {}
    for role in ('project_style', 'general_style'):
        text = ('完整指南 "术语" \\ 引用😀\n' + role) * 1700
        tools.path(manifest['styles'][role]['text_path']).write_bytes(text.encode('utf-8'))
        expected_guides[role] = text
    tools.snapshot['rows'] = [copy.deepcopy(data['rows'][0]) for _ in range(10)]
    for i, row in enumerate(tools.snapshot['rows']):
        row['row_id'] = str(3*i+1)
    tools.snapshot['rows'][6]['record']['large_field'] = '完整引用😀\\"' * 4000
    tools.prepare_context()
    guides, rows, index = reconstruct_context(tools)
    assert guides == expected_guides and rows == tools.snapshot['rows']
    assert index['page_count'] > 8
    notes = runtime_instructions(settings, root)
    assert f'exactly {index["page_count"]} prepared context pages' in notes
    assert 'IN PARALLEL' in notes and 'No separate inspect/read/source calls are needed' in notes
    assert 'concatenating pages' in notes


def test_context_excludes_locked_tm_rows_and_preserves_project_metadata(settings, tmp_path):
    tools, data = tools_for(settings, tmp_path)
    tools.snapshot.update(contract='siemens-json/2', project_name='中文项目', request_id='request-01')
    tools.snapshot['rows'][0]['row_id'] = '17'
    tools.snapshot['tm_reuse'] = [{'row_id': '2', 'record': {'private_tm': 'LOCKED_ONLY'}}]
    tools.prepare_context()
    guides, rows, index = reconstruct_context(tools)
    assert rows == tools.snapshot['rows'] and rows[0]['row_id'] == '17'
    task = tools.context()['task']
    assert task['project_name'] == '中文项目' and task['tm_reused_rows'] == 1
    assert 'LOCKED_ONLY' not in ''.join((tools.manifest_path.parent/'context'/p['filename']).read_text(encoding='utf-8') for p in index['pages'])


def test_csv_context_keeps_all_fields_even_when_a_header_or_cell_is_large(settings, tmp_path):
    header = 'header' * 2000
    source = ('en,zh-CN,' + header + '\nHello,,' + '引用' * 10000 + '\n').encode('utf-8')
    root, manifest, snapshot = setup_task(settings, tmp_path, source)
    tools = TaskTools(root / 'work/.worker/manifest.json')
    tools.prepare()
    guides, rows, index = reconstruct_context(tools)
    assert rows == tools.source()
    assert rows[0]['fields'][header] == '引用' * 10000


def test_context_cli_is_bounded_independent_of_cwd_and_rejects_tampered_page(settings, tmp_path):
    tools, _ = tools_for(settings, tmp_path)
    helper = tools.manifest_path.with_name('task_tools.py')
    result = subprocess.run([sys.executable, '-X', 'utf8', str(helper), 'context', '--page', '0'],
                            cwd=tmp_path, capture_output=True, timeout=10)
    assert result.returncode == 0 and len(result.stdout) <= READ_OUTPUT_BYTES
    assert json.loads(result.stdout) == tools.context(0)
    page = tools.manifest_path.parent / 'context/000000.json'
    page.write_bytes(page.read_bytes().replace(b'guide', b'other'))
    with pytest.raises(ValueError, match='integrity'):
        tools.context(0)


def test_read_cli_uses_budget_and_cursor_instead_of_ignoring_pagination(settings, tmp_path):
    tools, _ = tools_for(settings, tmp_path)
    tools.path(tools.meta['styles']['general_style']['text_path']).write_bytes(b'first\nsecond\nthird\n')
    done = subprocess.run([sys.executable, '-X', 'utf8', str(tools.manifest_path.with_name('task_tools.py')),
                           'read', 'general_style', '--offset', '1', '--limit', '1', '--max-output-bytes', '700'],
                          cwd=tmp_path, capture_output=True, timeout=10)
    assert done.returncode == 0 and len(done.stdout) <= 700
    page = json.loads(done.stdout)
    assert page['text'] == 'second\n' and page['next_offset'] == 2 and page['has_more']


def test_runner_builds_model_command_after_context_preparation(settings, tmp_path, monkeypatch):
    import asyncio
    from codex_remote_worker import runner
    from codex_remote_worker.common import JobError, Redactor, digest
    root = tmp_path / 'ordering'
    (root / 'input').mkdir(parents=True)
    (root / 'work').mkdir()
    prompt = b'original prompt'
    (root / 'input/prompt.md').write_bytes(prompt)
    job = {'job_id': 'job_ordering', 'inputs': {'prompt_file': {'filename': 'prompt.md'}},
           'prompt_sha256': digest(prompt), 'options': {}}
    actual = runner.CodexRunner(settings)
    monkeypatch.setattr(actual, 'ready', lambda: {'ready': True, 'codex_version': 'fixture', 'tools': {}})
    async def prepare(*args):
        path = root / 'work/.worker/context/index.json'
        path.parent.mkdir(parents=True)
        path.write_text('{"page_count":7}', encoding='utf-8')
        return {'context': {'page_count': 7}}
    monkeypatch.setattr(runner, 'prepare_task_runtime', prepare)
    def build(*args):
        assert 'exactly 7 prepared context pages' in runtime_instructions(settings, root)
        assert (root / 'input/prompt.md').read_bytes() == prompt
        raise JobError('COMMAND_OBSERVED', 'fixture')
    monkeypatch.setattr(runner, 'build_command', build)
    with pytest.raises(JobError) as error:
        asyncio.run(actual.run(job, root, lambda **fields: None, lambda: False, Redactor([])))
    assert error.value.code == 'COMMAND_OBSERVED'
