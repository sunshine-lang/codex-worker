import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
from types import SimpleNamespace

import pytest


PATH = Path(__file__).resolve().parents[1] / "packaging/portable/portable.py"
spec = importlib.util.spec_from_file_location("portable", PATH)
portable = importlib.util.module_from_spec(spec)
spec.loader.exec_module(portable)


def options(tmp_path, **values):
    (tmp_path / "worker-settings.json").write_text(json.dumps(values), encoding="utf-8")
    return portable.read_options(tmp_path)


def test_defaults_require_no_model_or_credentials(tmp_path):
    value = options(tmp_path)
    assert value == portable.DEFAULTS
    assert value["port"] == 8787 and value["listen_address"] == "0.0.0.0"
    assert not any("model" in key or "key" in key or "token" in key for key in value)


@pytest.mark.parametrize("value", [{"port": True}, {"port": 0}, {"port": 65536}, {"port": "8787"},
    {"listen_address": "invalid"}, {"model_name": "unwanted"}, {"public_api_url": "https://user:secret@example.test"},
    {"public_api_url": "https://example.test/?token=secret"}, {"public_api_url": "file:///private"},
    {"codex_executable": "private\npath"}])
def test_unsafe_options_rejected(tmp_path, value):
    with pytest.raises(portable.PortableError):
        options(tmp_path, **value)


def test_explicit_codex_path_never_silently_falls_back(tmp_path, monkeypatch):
    monkeypatch.setattr(portable.shutil, "which", lambda name: pytest.fail("No fallback for explicit path"))
    value = options(tmp_path, codex_executable=str(tmp_path / "missing.exe"))
    assert list(portable.codex_candidates(value)) == [value["codex_executable"]]
    with pytest.raises(portable.PortableError, match="未找到"):
        portable.find_codex(value)


def test_codex_discovery_executes_version_only(tmp_path, monkeypatch):
    executable = tmp_path / "codex.exe"
    executable.touch()
    calls = []
    def fake_run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout=b"codex-cli 0.167.0-alpha.3.2", stderr=b"")
    monkeypatch.setattr(portable.subprocess, "run", fake_run)
    path, version = portable.find_codex(options(tmp_path, codex_executable=str(executable)))
    assert path == executable and version == "0.167.0-alpha.3.2"
    assert calls == [[str(executable), "--version"]]


def test_gui_executable_not_mistaken_for_cli(tmp_path, monkeypatch):
    executable = tmp_path / "Codex.exe"
    executable.touch()
    monkeypatch.setattr(portable.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0, stdout=b"Electron GUI", stderr=b""))
    with pytest.raises(portable.PortableError):
        portable.find_codex(options(tmp_path, codex_executable=str(executable)))


def test_token_generated_once_and_bad_file_not_overwritten(tmp_path, monkeypatch):
    monkeypatch.setattr(portable, "protect_directory", lambda path: path.mkdir(parents=True, exist_ok=True))
    first = portable.ensure_token(tmp_path)
    assert len(first) >= 48 and portable.ensure_token(tmp_path) == first
    path = tmp_path / "Data/private/credentials.local.json"
    original = b'{"CODEX_WORKER_SERVICE_TOKEN":"short"}'
    path.write_bytes(original)
    with pytest.raises(portable.PortableError):
        portable.ensure_token(tmp_path)
    assert path.read_bytes() == original


def test_child_environment_does_not_inherit_model_api_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_ACCESS_TOKEN", "private")
    monkeypatch.setenv("OPENAI_API_KEY", "private")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://private.example")
    monkeypatch.setenv("PYTHONPATH", "private")
    config = {"codex_home": str(tmp_path / "home"), "codex_executable": str(tmp_path / "codex.exe")}
    env = portable.runtime_environment(tmp_path, config)
    assert "CODEX_ACCESS_TOKEN" not in env and "OPENAI_API_KEY" not in env and "OPENAI_BASE_URL" not in env
    assert "PYTHONPATH" not in env and env["CODEX_HOME"] == config["codex_home"]


def test_port_in_use_is_not_stopped():
    import socket
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen()
        with pytest.raises(portable.PortableError) as error:
            portable.check_port({"bind_host": "127.0.0.1", "port": sock.getsockname()[1]})
        assert error.value.code == "PORT_IN_USE"


def test_duplicate_start_never_rewrites_config_or_reruns_sandbox(tmp_path, monkeypatch, capsys):
    options(tmp_path)
    private = tmp_path / "Data/private"
    private.mkdir(parents=True)
    config = {"port": 8787, "bind_host": "0.0.0.0"}
    portable.save_json(tmp_path / "Data/config.local.json", config)
    monkeypatch.setattr(portable, "load_running", lambda root: {"pid": 123})
    monkeypatch.setattr(portable, "ensure_token", lambda root: "synthetic-private-token")
    monkeypatch.setattr(portable, "query_ready", lambda *a, **k: {"ready": True, "input_contracts": list(portable.REQUIRED_CONTRACTS)})
    monkeypatch.setattr(portable, "make_config", lambda *a: pytest.fail("Do not rewrite live config"))
    monkeypatch.setattr(portable, "run_probe", lambda *a: pytest.fail("Do not probe a live worker"))
    portable.start(tmp_path)
    assert "synthetic-private-token" not in capsys.readouterr().out
    assert portable.read_json(tmp_path / "Data/config.local.json") == config


@pytest.mark.skipif(os.name != "nt", reason="Windows process APIs")
def test_stop_refuses_active_jobs_and_releases_maintenance(tmp_path, monkeypatch):
    import win32api
    data = tmp_path / "Data"
    data.mkdir()
    db = sqlite3.connect(data / "jobs.sqlite3")
    db.execute("CREATE TABLE jobs(status TEXT)")
    db.execute("INSERT INTO jobs VALUES('queued')")
    db.commit()
    db.close()
    monkeypatch.setattr(portable, "load_running", lambda root: {"pid": 123})
    monkeypatch.setattr(win32api, "TerminateProcess", lambda *a: pytest.fail("Never terminate active jobs"))
    with pytest.raises(portable.PortableError) as error:
        portable.stop(tmp_path)
    assert error.value.code == "JOBS_ACTIVE"
    assert not (data / ".maintenance.json").exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows process APIs")
def test_stop_uses_verified_handle_only(tmp_path, monkeypatch):
    import win32api
    import win32event
    data = tmp_path / "Data"
    data.mkdir()
    db = sqlite3.connect(data / "jobs.sqlite3")
    db.execute("CREATE TABLE jobs(status TEXT)")
    db.commit()
    db.close()
    record = {"pid": 123}
    closed, terminated = [], []
    handle = SimpleNamespace(Close=lambda: closed.append(True))
    monkeypatch.setattr(portable, "load_running", lambda root: record)
    def verified(root, value, **kwargs):
        assert value is record and kwargs == {"terminate": True}
        assert (data / ".maintenance.json").exists()
        return handle
    monkeypatch.setattr(portable, "owned_process", verified)
    monkeypatch.setattr(win32api, "TerminateProcess", lambda value, code: terminated.append(value))
    monkeypatch.setattr(win32event, "WaitForSingleObject", lambda *a: 0)
    portable.stop(tmp_path)
    assert terminated == [handle] and closed == [True]
    assert not (data / ".maintenance.json").exists()


def test_probe_logs_redact_known_credentials(tmp_path, monkeypatch):
    from codex_remote_worker import config as settings_module
    (tmp_path / "Data/logs").mkdir(parents=True)
    secret = "synthetic-private-test-token"
    monkeypatch.setattr(settings_module, "load_settings", lambda *a: SimpleNamespace(secret_values=lambda: [secret]))
    monkeypatch.setattr(portable, "runtime_environment", lambda *a: {})
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=1, stdout=json.dumps({
        "passed": False, "code": "CHECK_FAILED", "message": secret}).encode(), stderr=secret.encode()))
    with pytest.raises(portable.PortableError) as error:
        portable.run_probe(["synthetic"], tmp_path, {}, "probe")
    assert secret not in error.value.message
    assert all(secret not in path.read_text(encoding="utf-8") for path in (tmp_path / "Data/logs").iterdir())


@pytest.mark.skipif(os.name != "nt", reason="Windows process APIs")
def test_admin_session_rejected_without_initializing_sandbox(monkeypatch):
    from win32com.shell import shell
    monkeypatch.setattr(shell, "IsUserAnAdmin", lambda: True)
    with pytest.raises(portable.PortableError) as error:
        portable.require_user_session()
    assert error.value.code == "USER_CONTEXT_REQUIRED"


@pytest.mark.skipif(os.name != "nt", reason="Windows filesystem ACLs")
def test_data_acl_only_current_user_system_and_admins(tmp_path):
    import win32security
    path = tmp_path / "private"
    portable.protect_directory(path)
    descriptor = win32security.GetNamedSecurityInfo(str(path), win32security.SE_FILE_OBJECT, win32security.DACL_SECURITY_INFORMATION)
    acl = descriptor.GetSecurityDescriptorDacl()
    sids = {win32security.ConvertSidToStringSid(acl.GetAce(i)[2]) for i in range(acl.GetAceCount())}
    assert sids == {portable.user_sid(), "S-1-5-18", "S-1-5-32-544"}


@pytest.mark.skipif(os.name != "nt", reason="Windows process APIs")
@pytest.mark.parametrize("check_only", [False, True])
def test_full_start_uses_existing_cli_and_checks_before_serve(tmp_path, monkeypatch, check_only):
    import win32api
    import win32process
    from codex_remote_worker import subscription, config as settings_module
    options(tmp_path)
    (tmp_path / "Data/private").mkdir(parents=True)
    (tmp_path / "Data/logs").mkdir()
    config = {"port": 8787, "bind_host": "0.0.0.0", "codex_executable": str(tmp_path / "existing/codex.exe"),
              "expected_codex_version": "target-version", "tool_python": str(tmp_path / ".runtime/python313/python.exe"),
              "codex_home": str(tmp_path / "existing-home")}
    events = []
    monkeypatch.setattr(portable, "load_running", lambda root: None)
    monkeypatch.setattr(portable, "make_config", lambda *a: config)
    monkeypatch.setattr(portable, "check_port", lambda *a: None)
    monkeypatch.setattr(portable, "ensure_token", lambda *a: "synthetic-private-token")
    monkeypatch.setattr(portable, "runtime_environment", lambda *a: {})
    monkeypatch.setattr(settings_module, "load_settings", lambda *a: object())
    monkeypatch.setattr(subscription, "require_chatgpt_login", lambda *a, **k: events.append("login"))
    def probe(command, *args):
        events.append("json" if "verify-json-runtime.py" in str(command) else "sandbox")
        return {"passed": True}
    monkeypatch.setattr(portable, "run_probe", probe)
    process = SimpleNamespace(pid=123, poll=lambda: None)
    def spawn(command, **kwargs):
        assert events == ["login", "sandbox", "json"]
        assert command[1] == "serve" and command[0].endswith("CodexRemoteWorker.exe")
        assert kwargs["creationflags"] & subprocess.CREATE_NO_WINDOW
        events.append("serve")
        return process
    monkeypatch.setattr(subprocess, "Popen", spawn)
    monkeypatch.setattr(win32api, "OpenProcess", lambda *a: SimpleNamespace(Close=lambda: None))
    monkeypatch.setattr(win32process, "GetProcessTimes", lambda *a: {"CreationTime": SimpleNamespace(timestamp=lambda: 123.5)})
    monkeypatch.setattr(portable, "user_sid", lambda: "synthetic-user")
    monkeypatch.setattr(portable, "query_ready", lambda *a, **k: {"ready": True, "service_version": "0.4.0", "input_contracts": list(portable.REQUIRED_CONTRACTS)})
    portable.start(tmp_path, check_only=check_only)
    assert events == ["login", "sandbox", "json"] + ([] if check_only else ["serve"])
    assert portable.read_json(tmp_path / "Data/config.local.json") == config
    assert (tmp_path / "Data/worker.process.json").exists() is not check_only


def test_portable_has_no_system_install_or_auth_copy_steps():
    source = PATH.read_text(encoding="utf-8")
    for forbidden in ("New-LocalUser", "Register-ScheduledTask", "New-NetFirewallRule", "sandbox setup", "login logout", "copy2(home"):
        assert forbidden not in source
    builder = (PATH.parents[2] / "scripts/build-portable.py").read_text(encoding="utf-8")
    assert '"tools/codex"' not in builder
    assert '"service-account.local.json"' in builder and '"auth.json"' in builder


@pytest.mark.skipif(os.name != "nt", reason="Windows process APIs")
def test_wrong_executable_pid_cannot_be_owned(tmp_path):
    # A real handle to this pytest process, never terminated. Identity must reject it.
    import win32api
    import win32process
    handle = win32api.OpenProcess(0x1000, False, os.getpid())
    try:
        created = win32process.GetProcessTimes(handle)["CreationTime"].timestamp()
    finally:
        handle.Close()
    record = {"root": str(tmp_path), "user_sid": portable.user_sid(), "pid": os.getpid(),
              "created_at": created, "executable": str(tmp_path / "bin/CodexRemoteWorker/CodexRemoteWorker.exe")}
    assert portable.owned_process(tmp_path, record) is None
