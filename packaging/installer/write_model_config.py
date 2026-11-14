import json
from pathlib import Path
import sys
values = json.loads(Path(sys.argv[1]).read_text(encoding='utf-8-sig'))
quote = lambda value: json.dumps(str(value), ensure_ascii=False)
text = '\n'.join([
    'model_provider = "custom"',
    'model = ' + quote(values['model']),
    'model_reasoning_effort = "high"',
    '', '[model_providers.custom]',
    'name = "Configured API"',
    'base_url = ' + quote(values['base_url']),
    'wire_api = "responses"',
    'env_key = "CODEX_WORKER_PROVIDER_API_KEY"',
    'requires_openai_auth = false',
    '', '[windows]', 'sandbox = "elevated"', '',
])
Path(sys.argv[2]).write_text(text, encoding='utf-8')
