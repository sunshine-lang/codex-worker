"""Native /1 and /2 tools probes; synthetic inputs, no models and no jobs."""
import argparse
import asyncio
import copy
import json
from pathlib import Path
import shutil
import uuid

from codex_remote_worker.common import digest, write_json
from codex_remote_worker.config import load_settings
from codex_remote_worker.doctor import capture
from codex_remote_worker.json_delivery import CONTRACT, read_json_source
from codex_remote_worker.runner import config_arguments
from codex_remote_worker.task_runtime import layout
from codex_remote_worker.tm_policy import PROJECT_CONTRACT, select_tm100


async def probe(settings, contract):
    preflight = (settings.data_root / 'preflight').resolve()
    root = preflight / ('json-compat-' + uuid.uuid4().hex)
    try:
        for name in ('work', 'control', 'logs'):
            (root / name).mkdir(parents=True)
        row = {'row_id': '00001', 'record': {'source_language': 'en', 'target_language': 'zh-CN',
            'source_text': 'Hello', 'target_text': '', 'context': None, 'tb_list': [], 'tm_list': [],
            'original_business_fields': {'code': '00001', 'null': None, 'flag': False}}}
        snapshot = {'contract': contract, 'phase': 'translate', 'rows': [row]}
        if contract == PROJECT_CONTRACT:
            row['row_id'] = '1'
            second = copy.deepcopy(row)
            second['row_id'] = '3'
            locked = copy.deepcopy(row)
            locked['row_id'] = '0'
            locked['record']['target_text'] = '你好'
            locked['record']['tm_list'] = [{'source_text': 'Hello', 'target_text': '你好', 'match_rate': 100}]
            locked['selection'] = select_tm100(locked['record'])
            snapshot.update(project_name='原生工具验证', request_id='fixed-json-probe', rows=[row, second], tm_reuse=[locked])
        write_json(root / 'work/input.json', snapshot)
        assert read_json_source(root / 'work/input.json') == snapshot
        write_json(root / 'control/source.json', snapshot)
        inputs = {'source_file': {'filename': 'input.json'}}
        for role in ('project_style', 'general_style'):
            path = root / 'work' / (role + '.md')
            path.write_text('中文指南\nPreserve original business fields.\n', encoding='utf-8')
            inputs[role] = {'filename': path.name, 'sha256': digest(path.read_bytes())}
        layout(settings, {'inputs': inputs, 'output_filename': 'input_result.json'}, root, snapshot, cache=False)
        command = [str(settings.codex_executable), *config_arguments(settings, root), 'sandbox',
            '--permission-profile', 'translation', '--include-managed-config', '--sandbox-state-disable-network',
            '--cd', str(root / 'work'), '--', str(settings.tool_python), '-X', 'utf8',
            str(root / 'work/.worker/task_tools.py')]
        smoke = json.loads(await capture(command + ['smoke'], settings, root))
        source_page = json.loads(await capture(command + ['source'], settings, root))
        rows = source_page['rows']
        assert source_page['has_more'] is False
        context_rows, guide_roles = [], set()
        for number in range(smoke['context']['page_count']):
            raw_page = await capture(command + ['context', '--page', str(number)], settings, root)
            assert len(raw_page.encode('utf-8')) <= 8000
            page = json.loads(raw_page)
            for part in page['parts']:
                if part['kind'] == 'source':
                    context_rows.append(part['row'])
                elif part['kind'] == 'guide':
                    guide_roles.add(part['role'])
        assert context_rows == snapshot['rows'] and guide_roles == {'project_style', 'general_style'}
        assert smoke['marker'] == 'RUNTIME_JSON_HELPER_OK' and smoke['utf8'] == '中文测试'
        assert smoke['delivery_mode'] == 'final_message_json' and rows == snapshot['rows']
        if contract == PROJECT_CONTRACT:
            assert smoke['model_rows'] == 2 and smoke['tm_reused_rows'] == 1
            assert smoke['project_name'] == snapshot['project_name']
            assert [r['row_id'] for r in rows] == ['1', '3']
        assert not list(root.rglob('*.csv'))
        return contract
    finally:
        assert root.is_relative_to(preflight) and root.name.startswith('json-compat-')
        shutil.rmtree(root, ignore_errors=True)


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--credentials-file', type=Path, required=True)
    args = parser.parse_args()
    settings = load_settings(args.config, args.credentials_file)
    contracts = [await probe(settings, contract) for contract in (CONTRACT, PROJECT_CONTRACT)]
    result = {'passed': True, 'model_called': False, 'jobs_created': 0, 'input_contracts': contracts,
        'checks': ['native_json_helper', 'unicode_guides', 'original_fields_preserved', 'no_csv_output',
                   'project_metadata', 'global_noncontiguous_row_ids', 'locked_tm_excluded_from_model_rows',
                   'bounded_context_pages', 'complete_context_input_coverage']}
    write_json(settings.data_root / 'json-runtime-verification.json', result)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    asyncio.run(main())
