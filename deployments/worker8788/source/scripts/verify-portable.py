"""Read-only portable release checks. No login, sandbox, server, or model invocation."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from codex_remote_worker import __version__


def digest(stream):
    return hashlib.file_digest(stream, "sha256").hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--zip", dest="archive", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    root = args.root.resolve()
    manifest = json.loads((root / "package-manifest.json").read_text(encoding="utf-8"))
    records = {entry["path"]: entry for entry in manifest["files"]}
    print("Checking portable file hashes", flush=True)
    def check_file(item):
        relative, entry = item
        path = (root / relative).resolve()
        assert path.is_relative_to(root) and path.is_file()
        assert path.stat().st_size == entry["bytes"]
        with path.open("rb") as stream:
            assert digest(stream) == entry["sha256"], relative
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(check_file, records.items()))
    all_files = {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}
    assert all_files == set(records) | {"package-manifest.json", "verification.json"}
    assert not (root / "Data").exists() and not (root / "tools/codex").exists()
    assert not list(root.rglob("codex.exe"))
    print("Checking every ZIP entry and CRC", flush=True)
    with zipfile.ZipFile(args.archive) as archive:
        expected = {root.name + "/" + path for path in all_files}
        assert set(archive.namelist()) == expected
        assert len(archive.namelist()) == len(expected)
        for entry in archive.infolist():
            relative = entry.filename[len(root.name) + 1:]
            with archive.open(entry) as stream:
                checksum = digest(stream)  # consumes every byte and verifies ZIP CRC
            if relative in records:
                assert checksum == records[relative]["sha256"], relative
    print("Checking relocated Python and CLI help in the Unicode desktop path", flush=True)
    python = root / ".runtime/python313/python.exe"
    worker = root / "bin/CodexRemoteWorker/CodexRemoteWorker.exe"
    checks = [
        ([python, "-X", "utf8", root / "portable.py", "--help"], "portable_help"),
        ([worker, "--help"], "worker_help"),
        ([python, "-X", "utf8", "-c", "import codex_remote_worker,openpyxl,docx,defusedxml,win32job,win32security; print(codex_remote_worker.__version__)"], "python_imports"),
    ]
    for command, name in checks:
        result = subprocess.run(list(map(str, command)), cwd=root, capture_output=True, timeout=60,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUTF8": "1"},
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        assert result.returncode == 0, name
        if name == "python_imports":
            assert result.stdout.strip().decode('ascii') == __version__
    assert not (root / "Data").exists()
    from codex_remote_worker.json_delivery import read_json_source
    packet = read_json_source(root / "examples/input.json")
    assert packet["phase"] == "translate" and len(packet["rows"]) == 1
    report = json.loads((root / "verification.json").read_text(encoding="utf-8"))
    with args.archive.open("rb") as stream:
        archive_hash = digest(stream)
    report.update({"all_file_hashes_passed": True, "full_zip_crc_passed": True,
        "unicode_desktop_path_runtime_smoke_passed": True, "python_tool_imports_passed": True,
        "example_json_validated": True, "distributed_data_directory": False,
        "zip_sha256": archive_hash, "zip_bytes": args.archive.stat().st_size,
        "native_target_login_tested": False, "native_target_sandbox_tested": False,
        "target_real_translation_tested": False})
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
