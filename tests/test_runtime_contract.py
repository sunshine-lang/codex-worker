import json
import os

from codex_remote_worker.runner import config_arguments, environment_fingerprint, _binary_digest


def test_environment_notes_keep_existing_developer_settings_and_do_not_override_model(settings, tmp_path):
    path = settings.codex_home / 'config.toml'
    path.write_text('developer_instructions="Existing instructions"\n' + path.read_text(encoding='utf-8'), encoding='utf-8')
    root = tmp_path / 'job'
    (root / 'work').mkdir(parents=True)
    values = config_arguments(settings, root)
    notes = next(value for value in values if value.startswith('developer_instructions='))
    assert 'Existing instructions' in notes
    assert 'Set-Location' in notes and 'task_tools.py' in notes
    assert 'fixture-provider-key' not in notes
    assert not any(value.startswith(('model=', 'model_provider=', 'model_reasoning_effort=')) for value in values)


def test_large_executable_hashes_are_cached_but_file_changes_invalidate(settings, monkeypatch):
    from codex_remote_worker import runner
    settings.codex_executable.write_bytes(b'codex')
    settings.doc_converter.write_bytes(b'office')
    original = runner.file_digest
    calls = []
    def tracked(path):
        calls.append(str(path))
        return original(path)
    _binary_digest.cache_clear()
    monkeypatch.setattr(runner, 'file_digest', tracked)
    first = environment_fingerprint(settings)
    environment_fingerprint(settings)
    assert calls.count(str(settings.codex_executable)) == 1
    stat = settings.codex_executable.stat()
    settings.codex_executable.write_bytes(b'other')
    os.utime(settings.codex_executable, ns=(stat.st_atime_ns, stat.st_mtime_ns + 10000000))
    assert environment_fingerprint(settings) != first
    assert calls.count(str(settings.codex_executable)) == 2
    _binary_digest.cache_clear()
