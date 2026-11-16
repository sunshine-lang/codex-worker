import json
import os
from pathlib import Path
import uuid

import pytest

from codex_remote_worker.common import JobError, Redactor, safe_name
from codex_remote_worker.config import Options
from codex_remote_worker.files import new_stage, prepare_record
from codex_remote_worker.qa import check_row, read_source
from codex_remote_worker.runner import build_command, config_arguments, runtime_environment, CodexRunner, send_prompt
from codex_remote_worker.store import InstanceLock


def record(text):
    return {'id': 'row-2', 'excel_row': 2, 'source': text, 'terms': []}


@pytest.mark.parametrize('source', ['3 AC 380V...500 V', '380 V...500 V', '380V...500'])
def test_voltage_range_is_not_identifier(source):
    target = '3 AC 380 - 500 V' if source.startswith('3') else '380 - 500 V'
    assert check_row(record(source), target) == []
    assert any(i['rule'] == 'number' for i in check_row(record(source), target.replace('500', '600')))
    assert any(i['rule'] == 'unit' for i in check_row(record(source), target.replace('V', '')))


def test_true_identifiers_and_tags_are_still_checked():
    assert not check_row(record('Pt1000, Pt100'), 'Pt1000、Pt100')
    assert any(i['rule'] == 'identifier' for i in check_row(record('V20, Pt1000'), 'V21、Pt100'))
    assert any(i['rule'] == 'placeholder' for i in check_row(record('Value {1}'), '值 {2}'))


def test_trace_drops_private_items_and_all_configured_credentials():
    redactor = Redactor(['custom-service-secret', 'provider-secret'])
    value = {'items': [{'type': 'reasoning', 'text': 'PRIVATE'}, {'role': 'system', 'content': 'PRIVATE'},
                      {'type': 'command_execution', 'text': 'custom-service-secret provider-secret sk-abcdefghijklmnop'}],
             'encrypted_content': 'PRIVATE', 'authorization': 'PRIVATE'}
    text = json.dumps(redactor.event(value))
    assert 'PRIVATE' not in text
    assert 'custom-service-secret' not in text
    assert 'provider-secret' not in text
    assert 'abcdefghijklmnop' not in text
    assert redactor.event({'usage': None, 'values': [None]}) == {'usage': None, 'values': [None]}


def test_permission_profile_not_overridden_by_legacy_flags(settings, monkeypatch):
    monkeypatch.setenv('PATH', '/fixture/codex-auth-helper')
    settings.data_root.mkdir()
    job_id, root = new_stage(settings)
    job = {'options': Options().model_dump()}
    args = build_command(settings, job, root)
    assert '--sandbox' not in args
    assert '--dangerously-bypass-approvals-and-sandbox' not in args
    assert '--ignore-user-config' not in args and '--model' not in args
    assert args[-1] == '-'
    assert 'default_permissions="translation"' in args
    assert not any(a.startswith(('model_provider=', 'model_providers.', 'model_reasoning_effort=', 'web_search=')) for a in args)
    assert any('":root"="read"' in a for a in args)
    assert any(json.dumps(str(settings.data_root)) + '="deny"' in a for a in args)
    assert 'fixture-provider-key' not in ' '.join(args)
    env = runtime_environment(settings, root)
    assert settings.service_token.get_secret_value() not in env.values()
    assert env['FIXTURE_PROVIDER_KEY'] == 'fixture-provider-key'
    assert env['CODEX_HOME'] == str(settings.codex_home)
    assert env['PATH'] == '/fixture/codex-auth-helper'
    tool_env = next(a for a in config_arguments(settings, root) if a.startswith('shell_environment_policy.set='))
    assert 'fixture-provider-key' not in tool_env and 'FIXTURE_PROVIDER_KEY' not in tool_env
    assert '/fixture/codex-auth-helper' not in tool_env
    assert any(json.dumps(str(settings.codex_home)) + '="deny"' in a for a in args)


def test_single_scheduler_lock(tmp_path):
    first = InstanceLock(tmp_path / 'lock')
    try:
        with pytest.raises(RuntimeError):
            InstanceLock(tmp_path / 'lock')
    finally:
        first.close()
    InstanceLock(tmp_path / 'lock').close()


def test_xlsx_limits_and_macros(tmp_path):
    from openpyxl import Workbook
    from zipfile import ZipFile
    path = tmp_path / 'source.xlsx'
    book = Workbook()
    book.active.append(['en', 'zh-CN'])
    book.active.cell(10002, 1, 'overflow')
    book.save(path)
    with pytest.raises(JobError, match='行列'):
        read_source(path)
    with ZipFile(path, 'a') as archive:
        archive.writestr('xl/vbaProject.bin', b'fixture')
    with pytest.raises(JobError, match='宏'):
        read_source(path)


def test_non_windows_is_not_production_ready(settings):
    if os.name == 'nt':
        pytest.skip('non-Windows-only assertion')
    assert CodexRunner(settings).ready()['code'] == 'WINDOWS_REQUIRED'


def test_prompt_delivery_handles_short_writes_and_broken_pipe():
    class ShortWriter:
        def __init__(self):
            self.data = bytearray()
            self.closed = False
        def write(self, data):
            self.data.extend(data[:3])
            return min(len(data), 3)
        def close(self):
            self.closed = True
    writer = ShortWriter()
    payload = b'\xef\xbb\xbf  original\r\n '
    send_prompt(writer, payload)
    assert bytes(writer.data) == payload and writer.closed
    writer = ShortWriter()
    writer.write = lambda _: 0
    with pytest.raises(JobError, match='未完整发送'):
        send_prompt(writer, payload)
    assert writer.closed
