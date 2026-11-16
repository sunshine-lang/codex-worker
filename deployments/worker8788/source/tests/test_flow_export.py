import ast
import json
from pathlib import Path


def test_export_has_standalone_nodes_no_credentials_or_local_bindings():
    root = Path(__file__).parents[1] / 'integrations' / 'langflow'
    flow = json.loads((root / 'Codex_Remote_Translation.json').read_text())
    nodes = flow['data']['nodes']
    assert len(nodes) == 3 and len(flow['data']['edges']) == 2
    templates = {n['id']: n['data']['node']['template'] for n in nodes}
    runner = templates['RemoteCodexTranslation-worker']
    assert not runner['service_token'].get('value')
    assert not runner['service_url'].get('value')
    assert not {'model_id', 'reasoning_effort', 'web_search'} & runner.keys()
    assert runner['action']['value'] == '自动运行'
    assert runner['action']['advanced'] is True
    assert runner['action']['options'] == ['自动运行', '重新翻译', '取消任务', '检查连接']
    assert runner['allow_insecure_http']['value'] is False
    assert runner['service_token']['required'] is False
    assert runner['bundle']['required'] is False
    for role in ['source_file', 'prompt_file', 'project_style', 'general_style']:
        field = templates['RemoteTranslationInputs-files'][role]
        assert not field.get('value') and not field.get('file_path')
    for stem, node in [('inputs', 'RemoteTranslationInputs-files'), ('runner', 'RemoteCodexTranslation-worker')]:
        expected = (root / (stem + '.py')).read_text()
        if stem == 'runner':
            expected = (root / 'client_core.py').read_text() + '\n\n' + expected
        code = templates[node]['code']['value']
        assert code == expected == (root / (stem + '.standalone.py')).read_text()
        ast.parse(code)
        assert '/Users/sunshine' not in code
        assert 'subprocess' not in code
