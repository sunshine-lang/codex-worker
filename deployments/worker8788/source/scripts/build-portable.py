"""Assemble an allowlisted portable tree, without installing or running it."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from datetime import datetime, timezone
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from codex_remote_worker import __version__


def sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--zip", dest="zip_path", type=Path)
    parser.add_argument("--scan-config", type=Path)
    parser.add_argument("--scan-credentials", type=Path)
    parser.add_argument("--test-results", type=Path)
    parser.add_argument("--source-archive", type=Path, help="Include a separately built source snapshot")
    args = parser.parse_args()
    dest = args.output.resolve()
    if dest.exists() or args.zip_path and args.zip_path.exists():
        raise SystemExit("Output must be new; refusing to overwrite.")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.mkdir()
    stage = ROOT / "build/desktop-subscription-stage/app"
    mapping = {".runtime/python313": ".runtime/python313", "bin/CodexRemoteWorker": "bin/CodexRemoteWorker",
               "tools/LibreOffice": "tools/LibreOffice", "licenses": "licenses"}
    for source, target in mapping.items():
        print("Copying " + source, flush=True)
        shutil.copytree(stage / source, dest / target, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store"))
    (dest / "prerequisites").mkdir()
    shutil.copy2(stage.parent / "dependencies/vc_redist.x64.exe", dest / "prerequisites/vc_redist.x64.exe")
    shutil.copy2(ROOT / "packaging/portable/portable.py", dest / "portable.py")
    shutil.copy2(ROOT / "packaging/portable/README.zh-CN.txt", dest / "使用说明.txt")
    shutil.copy2(ROOT / "packaging/portable/QUICKSTART.zh-CN.txt", dest / "先看这里.txt")
    compiler = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "Microsoft.NET/Framework64/v4.0.30319/csc.exe"
    subprocess.run([str(compiler), "/nologo", "/target:exe", "/platform:x64", "/optimize+",
        "/win32manifest:" + str(ROOT / "packaging/desktop.manifest"),
        "/out:" + str(dest / "CodexWorker.exe"), str(ROOT / "packaging/portable/PortableLauncher.cs")], check=True)
    if args.source_archive:
        (dest / "source").mkdir()
        shutil.copy2(args.source_archive, dest / "source/CodexWorker-Source.zip")
    (dest / "internal").mkdir()
    shutil.copy2(ROOT / "packaging/desktop-user/verify-json-runtime.py", dest / "internal/verify-json-runtime.py")
    examples = dest / "examples"
    examples.mkdir()
    shutil.copy2(ROOT / "packaging/portable/test-translation.py", examples / "test-translation.py")
    example = {"contract": "siemens-json/1", "phase": "translate", "rows": [{"row_id": "demo-001", "record": {
        "source_language": "en", "target_language": "zh-CN", "source_text": "Press the Start button.", "target_text": "",
        "context": "Operator panel", "tb_list": [], "tm_list": []}, "source_units": [{"id": 1, "source": "Press the Start button."}],
        "memory": {}, "term_decisions": []}]}
    (examples / "input.json").write_text(json.dumps(example, ensure_ascii=False, indent=2), encoding="utf-8")
    (examples / "prompt.md").write_text('Translate every source row from English to Simplified Chinese. Follow the two style guides. '
        'Return only a JSON object of this exact form: {"rows":[{"row_id":"demo-001","target_text":"the Chinese translation",'
        '"term_decisions":[]}]}. Preserve row_id exactly. No Markdown fences or explanation. Read input.json as needed.\n', encoding="utf-8")
    (examples / "project.md").write_text("Use concise industrial user-interface Chinese. Preserve the meaning.\n", encoding="utf-8")
    (examples / "general.md").write_text("Use Simplified Chinese, preserve numbers, and do not add unsupported information.\n", encoding="utf-8")
    demo_cmd = ('@echo off\r\nchcp 65001 >nul\r\n"%~dp0.runtime\\python313\\python.exe" -X utf8 '
                '"%~dp0examples\\test-translation.py"\r\nset "result=%errorlevel%"\r\necho.\r\npause\r\nexit /b %result%\r\n')
    for name in ("test-translation.cmd", "运行一条测试翻译.cmd"):
        (dest / name).write_bytes(demo_cmd.encode("ascii"))
    from importlib.util import module_from_spec, spec_from_file_location
    spec = spec_from_file_location("portable_defaults", ROOT / "packaging/portable/portable.py")
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    (dest / "worker-settings.json").write_text(json.dumps(module.DEFAULTS, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for command, chinese in (("start", "启动服务"), ("stop", "停止服务"), ("status", "查看状态"), ("check", "环境检查")):
        body = ('@echo off\r\nchcp 65001 >nul\r\nsetlocal\r\n'
                '"%~dp0.runtime\\python313\\python.exe" -X utf8 "%~dp0portable.py" ' + command + '\r\n'
                'set "result=%errorlevel%"\r\necho.\r\npause\r\nexit /b %result%\r\n')
        for name in (command, chinese):
            (dest / (name + ".cmd")).write_bytes(body.encode("ascii"))
    files = sorted(p for p in dest.rglob("*") if p.is_file())
    forbidden = {"auth.json", "credentials.local.json", "service-account.local.json", "config.local.json", "jobs.sqlite3", "worker.process.json"}
    secret_values = []
    if bool(args.scan_config) != bool(args.scan_credentials):
        raise SystemExit("Both scan paths are required.")
    if args.scan_config:
        from codex_remote_worker.config import load_settings
        secret_values = load_settings(args.scan_config, args.scan_credentials).secret_values()
    needles = [value.encode("utf-8") for value in secret_values if len(value) >= 8]
    needles += [value.encode("utf-16-le") for value in secret_values if len(value) >= 8]
    # Include the old Windows service-account password in the exclusion audit, never output it.
    if args.scan_credentials:
        account = args.scan_credentials.parent / "service-account.local.json"
        if account.is_file():
            value = json.loads(account.read_text(encoding="utf-8-sig")).get("password")
            if value:
                needles += [value.encode("utf-8"), value.encode("utf-16-le")]
    max_needle = max(map(len, needles), default=1)
    records = []
    print("Auditing file hashes and private-state exclusion", flush=True)
    for path in files:
        rel = path.relative_to(dest)
        if path.name.casefold() in forbidden or any(p in ("Data", ".codex") for p in rel.parts):
            raise SystemExit("Forbidden private-state filename in package.")
        if path.is_symlink():
            raise SystemExit("Symlinks are not allowed in package.")
        if needles:
            with path.open("rb") as stream:
                tail = b""
                while block := stream.read(1024 * 1024):
                    chunk = tail + block
                    if any(value in chunk for value in needles):
                        raise SystemExit("Credential scan failed; no secret was printed.")
                    tail = chunk[-max_needle:]
        records.append({"path": rel.as_posix(), "bytes": path.stat().st_size, "sha256": sha(path)})
    manifest = {"variant": "portable-existing-codex-official-login", "version": __version__,
        "built_at_utc": datetime.now(timezone.utc).isoformat(), "bundles_codex": False, "files": records}
    (dest / "package-manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    report = {"variant": manifest["variant"], "worker_version": __version__, "bundles_codex": False,
        "uses_existing_user_login": True, "copies_login_credentials": False, "creates_windows_users": False,
        "changes_firewall": False, "changes_personal_codex_config": False, "default_bind": "0.0.0.0:8787",
        "optional_signed_microsoft_runtime_included": True,
        "double_click_exe": "CodexWorker.exe", "source_included": bool(args.source_archive),
        "known_credential_matches": 0 if needles else None, "known_credentials_scanned": bool(needles), "file_count": len(files),
        "uncompressed_bytes": sum(v["bytes"] for v in records), "worker_sha256": sha(dest / "bin/CodexRemoteWorker/CodexRemoteWorker.exe"),
        "native_target_login_tested": False, "native_target_sandbox_tested": False, "target_real_translation_tested": False,
        "input_contracts": ["siemens-json/2", "siemens-json/1", "table-csv/1"],
        "notes": ["Hardened runtime plus TM/project-archive upgrade and existing-login portable startup.",
                  "Does not deploy/restart production. Target authentication, sandbox, entitlement and quota require real test."]}
    if args.test_results:
        report["local_test_results"] = json.loads(args.test_results.read_text(encoding="utf-8"))
    (dest / "verification.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    if args.zip_path:
        print("Creating transfer ZIP", flush=True)
        args.zip_path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(args.zip_path, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            for path in sorted(p for p in dest.rglob("*") if p.is_file()):
                archive.write(path, Path(dest.name) / path.relative_to(dest))
        archive_hash = sha(args.zip_path)
        args.zip_path.with_suffix(".zip.sha256.txt").write_text(archive_hash + "  " + args.zip_path.name + "\n", encoding="utf-8")
        report.update(zip_bytes=args.zip_path.stat().st_size, zip_sha256=archive_hash)
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
