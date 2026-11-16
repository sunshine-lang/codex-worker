"""Compile component templates and wire an importable flow; never run node outputs."""
import hashlib
import inspect
import json
from pathlib import Path

from lfx.custom import Component
from lfx.custom.utils import build_custom_component_template
from lfx.components.input_output import chat_output
from lfx.graph.flow_builder import empty_flow, add_component, add_connection, layout_flow


def build():
    root = Path(__file__).resolve().parents[1] / 'integrations' / 'langflow'
    registry = {}
    for filename in ['inputs', 'runner']:
        code = (root / (filename + '.py')).read_text()
        if filename == 'runner':
            code = (root / 'client_core.py').read_text() + '\n\n' + code
        compile(code, filename, 'exec')
        template, instance = build_custom_component_template(Component(_code=code))
        template['custom'] = True
        template['tool_mode'] = False
        registry[instance.name] = template
        (root / (filename + '.standalone.py')).write_text(code)
    code = inspect.getsource(chat_output)
    template, _ = build_custom_component_template(chat_output.ChatOutput(_code=code))
    registry['ChatOutput'] = template
    flow = empty_flow(name='Siemens 英中翻译｜远程 Codex Worker',
        description='上传四份资料后直接运行，自动提交、续查并下载结果。同资料复用已有任务；重新翻译、取消和检查连接在高级设置中。使用 Windows 现有 Codex 配置。')
    add_component(flow, 'RemoteTranslationInputs', registry, component_id='RemoteTranslationInputs-files')
    add_component(flow, 'RemoteCodexTranslation', registry, component_id='RemoteCodexTranslation-worker')
    add_component(flow, 'ChatOutput', registry, component_id='ChatOutput-delivery')
    add_connection(flow, 'RemoteTranslationInputs-files', 'bundle', 'RemoteCodexTranslation-worker', 'bundle')
    add_connection(flow, 'RemoteCodexTranslation-worker', 'result', 'ChatOutput-delivery', 'input_value')
    layout_flow(flow)
    path = root / 'Codex_Remote_Translation.json'
    path.write_text(json.dumps(flow, ensure_ascii=False, indent=2))
    print(json.dumps({'nodes': len(flow['data']['nodes']), 'edges': len(flow['data']['edges']),
        'file': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'executed': False}, ensure_ascii=False))
    return flow


if __name__ == '__main__':
    build()
