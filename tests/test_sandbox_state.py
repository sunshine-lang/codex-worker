import copy
import json
import os

import pytest

from codex_remote_worker.common import JobError
from codex_remote_worker.sandbox_state import capture_sandbox_state, same_sandbox_state


def state():
    return {'marker_sha256': 'fixture', 'accounts': [
        {'name': 'Offline', 'sid': 'S-1-5-21-1001', 'password_set_at': 1000},
        {'name': 'Online', 'sid': 'S-1-5-21-1002', 'password_set_at': 1001}]}


def test_password_age_sampling_and_reordered_accounts():
    first = state()
    second = copy.deepcopy(first)
    second['accounts'].reverse()
    second['accounts'][0]['password_set_at'] += 2
    assert same_sandbox_state(first, second)


@pytest.mark.parametrize('change', ['password', 'sid', 'marker', 'duplicate', 'missing', 'nan'])
def test_reprovisioning_or_invalid_receipt_is_not_ready(change):
    first, second = state(), state()
    if change == 'password':
        second['accounts'][0]['password_set_at'] += 60
    elif change == 'sid':
        second['accounts'][0]['sid'] += '0'
    elif change == 'marker':
        second['marker_sha256'] = 'reprovisioned'
    elif change == 'duplicate':
        second['accounts'][1] = second['accounts'][0]
    elif change == 'missing':
        second = None
    elif change == 'nan':
        second['accounts'][0]['password_set_at'] = float('nan')
    assert not same_sandbox_state(first, second)


def test_capture_only_reads_public_account_metadata(settings, monkeypatch):
    from codex_remote_worker import sandbox_state
    directory = settings.codex_home / '.sandbox'
    directory.mkdir()
    (directory / 'setup_marker.json').write_text(json.dumps({
        'version': 5, 'offline_username': 'Offline', 'online_username': 'Online'}))
    monkeypatch.setattr(sandbox_state, '_account_state', lambda name: {'name': name})
    result = capture_sandbox_state(settings)
    assert [a['name'] for a in result['accounts']] == ['Offline', 'Online']
    assert len(result['marker_sha256']) == 64
    assert not (settings.codex_home / '.sandbox-secrets').exists()


def test_missing_or_locked_accounts_fail_with_safe_error(settings, monkeypatch):
    from codex_remote_worker import sandbox_state
    directory = settings.codex_home / '.sandbox'
    directory.mkdir()
    (directory / 'setup_marker.json').write_text(json.dumps({
        'version': 5, 'offline_username': 'Offline', 'online_username': 'Online'}))
    def fail(name):
        raise OSError('private diagnostic details')
    monkeypatch.setattr(sandbox_state, '_account_state', fail)
    with pytest.raises(JobError) as error:
        capture_sandbox_state(settings)
    assert error.value.code == 'SANDBOX_STATE_CHANGED'
    assert 'private' not in error.value.message


def test_mxc_does_not_depend_on_legacy_users(settings):
    settings.windows_sandbox = 'mxc'
    assert capture_sandbox_state(settings) is None


@pytest.mark.skipif(os.name != 'nt', reason='Native Windows readiness gate')
@pytest.mark.parametrize('changed', [False, True])
def test_readiness_rechecks_shared_accounts(settings, monkeypatch, changed):
    from codex_remote_worker import runner
    from win32com.shell import shell
    settings.data_root.mkdir()
    settings.codex_executable.touch()
    settings.doc_converter.touch()
    receipt = {'passed': True, 'fingerprint': 'fixture', 'codex_version': 'fixture',
               'tools': {}, 'verified_at': '2026-11-11T00:00:00Z', 'sandbox_state': state()}
    (settings.data_root / 'preflight.json').write_text(json.dumps(receipt))
    monkeypatch.setattr(shell, 'IsUserAnAdmin', lambda: False)
    monkeypatch.setattr(runner, 'environment_fingerprint', lambda settings: 'fixture')
    current = state()
    if changed:
        current['accounts'][0]['password_set_at'] += 600
    monkeypatch.setattr(runner, 'capture_sandbox_state', lambda settings: current)
    result = runner.CodexRunner(settings).ready()
    assert result['ready'] is not changed
    if changed:
        assert result['code'] == 'SANDBOX_STATE_CHANGED'


@pytest.mark.skipif(os.name != 'nt', reason='Native Windows readiness gate')
def test_maintenance_gate_does_not_dispatch(settings):
    from codex_remote_worker.runner import CodexRunner
    settings.data_root.mkdir()
    (settings.data_root / '.maintenance.json').write_text('{}')
    assert CodexRunner(settings).ready()['code'] == 'MAINTENANCE'
