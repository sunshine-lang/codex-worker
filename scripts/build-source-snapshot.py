"""Archive the selected project sources, excluding local state and generated exports."""
import argparse
import hashlib
import json
from pathlib import Path
import zipfile


ROOT = Path(__file__).resolve().parents[1]


def source_files():
    for name in (".gitignore", "README.md", "NOTICE.md", "SPEC.md", "pyproject.toml", "config.example.json"):
        path = ROOT / name
        if path.is_file() and not path.is_symlink():
            yield path
    for folder in ("src", "tests", "packaging", "docs", "scripts", "integrations"):
        for path in sorted((ROOT / folder).rglob("*")):
            if not path.is_file() or path.is_symlink():
                continue
            relative = path.relative_to(ROOT)
            if any(part.casefold() in {"__pycache__", ".pytest_cache", ".git", ".runtime", "private", "data"} for part in relative.parts):
                continue
            if ".local." in path.name or path.suffix.lower() not in {".py", ".ps1", ".cs", ".manifest", ".md", ".txt", ".json"}:
                continue
            if folder == "integrations" and path.suffix != ".py":
                continue
            yield path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    records = []
    prefix = "CodexWorker-Source"
    with zipfile.ZipFile(args.output, "x", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for path in sorted(source_files()):
            relative = path.relative_to(ROOT).as_posix()
            data = path.read_bytes()
            records.append({"path": relative, "sha256": hashlib.sha256(data).hexdigest()})
            archive.writestr(prefix + "/" + relative, data)
        archive.writestr(prefix + "/SOURCE-SNAPSHOT.txt",
            "Project source snapshot. Use the companion portable runtime to run the Worker.\n"
            "Local configurations, job data, personal login, Git history and generated integration JSON are excluded.\n"
            "Packaging variants have different prerequisites; follow packaging/portable/README.zh-CN.txt for this delivery.\n")
        archive.writestr(prefix + "/source-manifest.json", json.dumps({"files": records}, indent=2))
    with zipfile.ZipFile(args.output) as archive:
        assert archive.testzip() is None
    with args.output.open("rb") as stream:
        checksum = hashlib.file_digest(stream, "sha256").hexdigest()
    args.output.with_suffix(".zip.sha256.txt").write_text(checksum + "  " + args.output.name + "\n", encoding="ascii")
    print(json.dumps({"source_files": len(records), "output": str(args.output), "zip_crc_passed": True}))


if __name__ == "__main__":
    main()
