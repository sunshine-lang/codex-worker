import asyncio
import sys

import pytest


@pytest.mark.parametrize('passed', [True, False])
def test_probe_command_is_fixed_live_check(settings, monkeypatch, capsys, passed):
    from codex_remote_worker import __main__ as entry
    calls = []
    async def verify(s, *, live=False):
        calls.append((s, live))
        return {'passed': passed, 'probe_only': True, 'model_called': False}
    monkeypatch.setattr(entry, 'load_settings', lambda *args: settings)
    monkeypatch.setattr(entry, 'verify', verify)
    monkeypatch.setattr(sys, 'argv', ['worker', 'probe', '--config', 'fixture.json'])
    assert entry.main() == (0 if passed else 1)
    assert calls == [(settings, True)]
    assert '"model_called": false' in capsys.readouterr().out


def test_probe_rejects_job_override(settings, monkeypatch):
    from codex_remote_worker import __main__ as entry
    monkeypatch.setattr(entry, 'load_settings', lambda *args: settings)
    monkeypatch.setattr(sys, 'argv', ['worker', 'probe', '--config', 'fixture.json', '--job-id', 'anything'])
    with pytest.raises(SystemExit) as error:
        entry.main()
    assert error.value.code == 2
