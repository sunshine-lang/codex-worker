import json

import pytest

from codex_remote_worker.app_server_text import (
    TextAppServerRunner, context_message, deliver_text_result, output_schema, text_thread_parameters,
)
from codex_remote_worker.common import JobError, write_json
from codex_remote_worker.qa import check_csv, read_source
from codex_remote_worker.task_runtime import layout
from codex_remote_worker.task_tools import TaskTools
from test_json_delivery import packet, reply


def prepared(settings, tmp_path, snapshot=None):
    root = tmp_path / 'job'
    (root / 'work/outputs').mkdir(parents=True)
    (root / 'control').mkdir()
    settings.doc_converter.write_bytes(b'fixture')
    (root / 'work/project.md').write_text('PROJECT guide must be present', encoding='utf-8')
    (root / 'work/general.md').write_text('GENERAL guide must be present', encoding='utf-8')
    if snapshot is None:
        (root / 'work/input.csv').write_text('en,zh-CN,Context\nHello,,context\n', encoding='utf-8')
        snapshot = read_source(root / 'work/input.csv')
    write_json(root / 'control/source.json', snapshot)
    job = {'inputs': {'project_style': {'filename': 'project.md', 'sha256': 'p'},
        'general_style': {'filename': 'general.md', 'sha256': 'g'},
        'source_file': {'filename': 'input.csv'}}, 'output_filename': 'result.csv'}
    layout(settings, job, root)
    TaskTools(root / 'work/.worker/manifest.json').prepare()
    return root, snapshot


def test_host_context_preserves_prompt_guides_and_row_metadata(settings, tmp_path):
    source = packet()
    source['rows'][0]['record']['tb_list'] = [{'source_text': 'Hello', 'target_text': '你好'}]
    source['tm_reuse'] = [{'private_locked_row': 'already complete'}]
    root, _ = prepared(settings, tmp_path, source)
    message, schema = context_message(root, b'\xef\xbb\xbf Exact instructions\r\n')
    data = json.loads(message)
    assert data['uploaded_prompt'] == ' Exact instructions\r\n'
    assert data['source']['rows'] == source['rows']
    assert 'tm_reuse' not in data['source']
    assert 'PROJECT' in data['guides']['project_style']
    assert 'GENERAL' in data['guides']['general_style']
    assert schema['additionalProperties'] is False


def test_host_build_preserves_original_csv_and_validates_rows(settings, tmp_path):
    root, snapshot = prepared(settings, tmp_path)
    deliver_text_result(root, b'{"rows":[{"row_id":"row-2","target_text":"Hello"}]}')
    output = root / 'work/outputs/result.csv'
    assert output.read_bytes() == (root / 'work/input.csv').read_bytes().replace(b'Hello,,', b'Hello,Hello,')
    assert check_csv(snapshot, output)['original_fields_unchanged']


@pytest.mark.parametrize('rows', [[], [{'row_id': 'wrong', 'target_text': 'x'}],
    [{'row_id': 'row-2', 'target_text': 'x'}, {'row_id': 'row-2', 'target_text': 'y'}],
    [{'row_id': 'row-2', 'target_text': 123}]])
def test_table_rejects_incomplete_or_invalid_delivery(settings, tmp_path, rows):
    root, _ = prepared(settings, tmp_path)
    with pytest.raises(JobError):
        deliver_text_result(root, json.dumps({'rows': rows}).encode())
    assert not (root / 'work/outputs/result.csv').exists()


@pytest.mark.parametrize('phase', ['translate', 'review'])
def test_json_delivery_preserves_existing_validation(settings, tmp_path, phase):
    root, snapshot = prepared(settings, tmp_path, packet(phase))
    deliver_text_result(root, json.dumps(reply(phase)).encode())
    with pytest.raises(JobError):
        deliver_text_result(root, b'{"rows":[]}')
    assert ('decision' in output_schema(snapshot)['properties']['rows']['items']['properties']) == (phase == 'review')


def test_personal_tools_disabled_and_environment_never_enabled(settings, tmp_path):
    with (settings.codex_home / 'config.toml').open('a') as f:
        f.write('\n[mcp_servers.personal]\ncommand="untrusted.exe"\n')
    root, _ = prepared(settings, tmp_path)
    parameters = text_thread_parameters(settings, root)
    assert parameters['environments'] == [] and parameters['runtimeWorkspaceRoots'] == []
    assert parameters['dynamicTools'] == [] and parameters['selectedCapabilityRoots'] == []
    assert parameters['config']['mcp_servers."personal".enabled'] is False
    assert parameters['config']['features.shell_tool'] is False
    assert parameters['config']['web_search'] == 'disabled'
    runner = TextAppServerRunner(settings)
    turn = runner.turn_parameters('thread', b'prompt', root)
    assert turn['environments'] == [] and 'outputSchema' in turn
    with pytest.raises(JobError, match='纯文本'):
        runner.check_item({'type': 'commandExecution'})
