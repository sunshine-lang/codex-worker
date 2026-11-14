from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip('lfx', reason='Node checks require the Langflow development environment')


def test_missing_binding_cannot_be_silently_treated_as_empty_inputs(tmp_path, monkeypatch):
    path = Path(__file__).parents[1] / 'integrations/langflow/inputs.py'
    namespace = {'__name__': 'remote_inputs_test'}
    exec(compile(path.read_text(), str(path), 'exec'), namespace)
    monkeypatch.setitem(namespace, 'component_file_access_scopes', lambda node: [])
    monkeypatch.setitem(namespace, 'enforce_local_file_access', lambda path, **kwargs: Path(path))
    node = SimpleNamespace(user_id='user', graph=SimpleNamespace(flow_id='flow'), source_file='', prompt_file='',
        project_style=str(tmp_path / 'missing.md'), general_style='', get_full_path=lambda value: value)
    with pytest.raises(ValueError, match='项目风格指南'):
        namespace['RemoteTranslationInputs'].bundle_files(node)
    node.project_style = ''
    assert namespace['RemoteTranslationInputs'].bundle_files(node).data['files'] == {}
