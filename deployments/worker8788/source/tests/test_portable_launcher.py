"""Exercise the real EXE with a fixture child, without Codex, credentials or services."""
import os
from pathlib import Path
import subprocess

import pytest


PROJECT = Path(__file__).resolve().parents[1]
COMPILER = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "Microsoft.NET/Framework64/v4.0.30319/csc.exe"
pytestmark = pytest.mark.skipif(os.name != "nt" or not COMPILER.is_file(), reason="Windows C# compiler required")


def compile_exe(source, output, manifest=None):
    command = [str(COMPILER), "/nologo", "/target:exe", "/platform:x64", "/out:" + str(output)]
    if manifest:
        command.append("/win32manifest:" + str(manifest))
    subprocess.run(command + [str(source)], capture_output=True, check=True, timeout=30)


@pytest.fixture(scope="module")
def binaries(tmp_path_factory):
    build = tmp_path_factory.mktemp("portable-launcher-build")
    launcher = build / "CodexWorker.exe"
    compile_exe(PROJECT / "packaging/portable/PortableLauncher.cs", launcher, PROJECT / "packaging/desktop.manifest")
    source = build / "fixture.cs"
    source.write_text('''using System; using System.IO; using System.Text;
class Fixture { static int Main(string[] args) {
 Console.OutputEncoding = new UTF8Encoding(false);
 Console.WriteLine(Directory.GetCurrentDirectory());
 foreach (string arg in args) Console.WriteLine(arg);
 string code = Environment.GetEnvironmentVariable("PORTABLE_FIXTURE_EXIT_CODE");
 if (code != null) return Int32.Parse(code);
 return args.Length > 0 && args[args.Length - 1] == "stop" ? 19 : 0;
}}''', encoding="utf-8")
    child = build / "python.exe"
    compile_exe(source, child)
    return launcher, child


@pytest.fixture
def package(tmp_path, binaries):
    root = tmp_path / "迁移 & portable"
    root.mkdir()
    launcher, child = binaries
    (root / "CodexWorker.exe").write_bytes(launcher.read_bytes())
    runtime = root / ".runtime/python313/python.exe"
    runtime.parent.mkdir(parents=True)
    runtime.write_bytes(child.read_bytes())
    for name in ("portable.py", "worker-settings.json", "bin/CodexRemoteWorker/CodexRemoteWorker.exe", "internal/verify-json-runtime.py"):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("fixture only; never executed", encoding="ascii")
    return root


def run(root, *args):
    return subprocess.run([str(root / "CodexWorker.exe"), *args], cwd=PROJECT, stdin=subprocess.DEVNULL,
        capture_output=True, encoding="utf-8-sig", timeout=15)


@pytest.mark.parametrize("command", ["start", "status", "stop", "check"])
def test_launcher_preserves_unicode_paths_arguments_and_child_exit(package, command):
    result = run(package, command, "--no-pause")
    assert result.returncode == (19 if command == "stop" else 0)
    assert result.stdout.splitlines() == [str(package), "-X", "utf8", str(package / "portable.py"), command]


def test_double_click_defaults_to_start(package):
    result = run(package)
    assert result.returncode == 0
    assert result.stdout.splitlines()[4] == "start"
    assert "本次启动已完成" in result.stdout


def test_failed_double_click_never_claims_background_service_is_ready(package, monkeypatch):
    monkeypatch.setenv("PORTABLE_FIXTURE_EXIT_CODE", "1")
    result = run(package)
    assert result.returncode == 1
    assert "本次启动未完成" in result.stdout
    assert "会继续运行" not in result.stdout


def test_incomplete_package_fails_before_child_launch(package):
    (package / "worker-settings.json").unlink()
    result = run(package, "start", "--no-pause")
    assert result.returncode == 3 and "PACKAGE_INCOMPLETE" in result.stderr
    assert not result.stdout


def test_runtime_file_check_does_not_launch_child(package):
    result = run(package, "--check-runtime")
    assert result.returncode == 0 and "PACKAGE_FILES_PRESENT" in result.stdout
    assert str(package / "portable.py") not in result.stdout


@pytest.mark.parametrize("argument", ["--help", "--unexpected"])
def test_help_and_invalid_arguments_need_no_runtime(package, argument):
    (package / ".runtime/python313/python.exe").unlink()
    result = run(package, argument)
    assert result.returncode == (0 if argument == "--help" else 2)
    assert "PACKAGE_INCOMPLETE" not in result.stderr
