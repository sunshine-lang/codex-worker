import asyncio
import csv
from pathlib import Path
import sys
import threading

import pytest

from codex_remote_worker.common import JobError, now
from codex_remote_worker.config import Settings


@pytest.fixture
def settings(tmp_path):
    home = tmp_path / 'codex-home'
    home.mkdir()
    (home / 'config.toml').write_text('model = "server/default-model"\nmodel_provider = "fixture"\nmodel_reasoning_effort = "high"\nweb_search = "live"\n[model_providers.fixture]\nname = "Fixture"\nenv_key = "FIXTURE_PROVIDER_KEY"\n')
    return Settings(data_root=tmp_path / 'data', codex_executable=tmp_path / 'codex.exe',
        codex_home=home,
        tool_python=Path(sys.executable), doc_converter=tmp_path / 'soffice.exe',
        service_token='fixture-service-token-with-32-characters', provider_env={'FIXTURE_PROVIDER_KEY': 'fixture-provider-key'},
        min_free_bytes=0)


class FakeRunner:
    def __init__(self, mode='success', blocked=False):
        self.mode, self.calls, self.prompts, self.stops = mode, 0, [], 0
        self.release = threading.Event()
        if not blocked:
            self.release.set()
        self.recoverable = True

    def ready(self):
        return {'ready': True, 'codex_version': 'fixture', 'tools': {}, 'upstream_model_verified': False}

    def recover(self, job):
        return self.recoverable

    async def run(self, job, root, touch, stopped, redactor):
        self.calls += 1
        self.prompts.append((root / 'input' / job['inputs']['prompt_file']['filename']).read_bytes())
        touch(stage='agent_running', progress={'event_count': 1, 'last_activity_at': now()})
        try:
            while not self.release.is_set():
                if stopped():
                    raise JobError('CANCEL_REQUESTED', 'fixture cancelled')
                await asyncio.sleep(.01)
            if self.mode == 'error':
                raise JobError('EXECUTION_FAILED', 'fixture failed')
            source = root / 'work' / job['inputs']['source_file']['filename']
            with source.open(encoding='utf-8-sig', newline='') as f:
                rows = list(csv.reader(f))
            index = rows[0].index('zh-CN')
            for row in rows[1:]:
                row[index] = '' if self.mode == 'empty' else '你好'
            if self.mode == 'changed':
                rows[1][0] = 'Modified'
            if self.mode != 'missing':
                with (root / 'work' / 'outputs' / job['output_filename']).open('w', encoding='utf-8-sig', newline='') as f:
                    csv.writer(f).writerows(rows)
            (root / 'control' / 'final_response.raw.md').write_text('done fixture-provider-key')
            (root / 'logs' / 'trace.jsonl').write_text('{"type":"turn.completed"}\n')
            return {'exit_code': 0, 'codex_version': 'fixture', 'provider': 'fixture', 'trace_complete': True}
        finally:
            self.stops += 1


@pytest.fixture
def runner():
    return FakeRunner()
