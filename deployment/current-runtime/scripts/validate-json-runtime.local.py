"""Fixed synthetic JSON helper check under the real service account; no model or jobs."""
import asyncio
import json
from pathlib import Path
import shutil
import sys
import uuid

sys.path.insert(0, str(Path(r'C:\CodexRemoteWorker\src')))
from codex_remote_worker.common import digest, write_json
from codex_remote_worker.config import load_settings
from codex_remote_worker.doctor import capture
from codex_remote_worker.json_delivery import CONTRACT
from codex_remote_worker.runner import config_arguments
from codex_remote_worker.task_runtime import layout


async def main():
    install = Path(r'C:\CodexRemoteWorker')
    config = json.loads((install / 'config.local.json').read_text(encoding='utf-8-sig'))
    settings = load_settings(install / 'config.local.json', Path(config['data_root']) / 'private/credentials.local.json')
    preflight = (settings.data_root / 'preflight').resolve()
    root = (preflight / ('json-compat-' + uuid.uuid4().hex)).resolve()
    assert root.is_relative_to(preflight) and root.name.startswith('json-compat-')
    try:
        for name in ('work', 'control', 'logs'):
            (root / name).mkdir(parents=True)
        snapshot = {'contract': CONTRACT, 'phase': 'translate', 'rows': [{
            'row_id': '00001', 'record': {'source_language': 'en', 'target_language': 'zh-CN',
            'source_text': 'Hello', 'target_text': '', 'context': None, 'tb_list': [], 'tm_list': [],
            'original_business_fields': {'code': '00001', 'null': None, 'flag': False}}}]}
        write_json(root / 'work' / 'input.json', snapshot)
        write_json(root / 'control' / 'source.json', snapshot)
        inputs = {'source_file': {'filename': 'input.json'}}
        for role in ('project_style', 'general_style'):
            text = '中文指南\nPreserve original business fields.\n'
            path = root / 'work' / (role + '.md')
            path.write_text(text, encoding='utf-8')
            inputs[role] = {'filename': path.name, 'sha256': digest(path.read_bytes())}
        layout(settings, {'inputs': inputs, 'output_filename': 'input_result.json'}, root, snapshot, cache=False)
        base = [str(settings.codex_executable), *config_arguments(settings, root), 'sandbox',
                '--permission-profile', 'translation', '--include-managed-config', '--sandbox-state-disable-network',
                '--cd', str(root / 'work'), '--', str(settings.tool_python), '-X', 'utf8',
                str(root / 'work' / '.worker' / 'task_tools.py')]
        smoke = json.loads(await capture(base + ['smoke'], settings, root))
        rows = json.loads(await capture(base + ['source'], settings, root))
        assert smoke['marker'] == 'RUNTIME_JSON_HELPER_OK' and smoke['utf8'] == '中文测试'
        assert smoke['delivery_mode'] == 'final_message_json' and rows == snapshot['rows']
        assert not list(root.rglob('*.csv'))
        result = {'passed': True, 'model_called': False, 'jobs_created': 0,
                  'checks': ['native_json_helper', 'unicode_guides', 'original_fields_preserved', 'no_csv_output']}
        write_json(settings.data_root / 'json-runtime-verification.json', result)
        print(json.dumps(result))
    finally:
        if root.is_relative_to(preflight) and root.name.startswith('json-compat-'):
            shutil.rmtree(root, ignore_errors=True)


asyncio.run(main())
