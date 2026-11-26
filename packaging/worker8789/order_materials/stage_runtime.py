"""Assemble only the 8789 runtime from the baseline shipped in this checkout."""

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory

HERE = Path(__file__).resolve().parent
BASE = HERE.parents[2] / "deployments/worker8788/source/src/codex_remote_worker"


def normalized(path):
    return path.read_text(encoding="utf-8-sig").encode("utf-8")


def verify(package, manifest, field):
    for name, expected in manifest.items():
        if hashlib.sha256(normalized(package / name)).hexdigest() != expected[field]:
            raise ValueError(f"8789 build: unexpected {field} contents of {name}.")


def stage(output):
    output = Path(output).resolve()
    if (
        output.exists()
        or output.is_relative_to(BASE.resolve())
        or any(
            x.casefold() in ("codexremoteworker-appserver-8788", "codexworkerdata-appserver-8788")
            for x in output.parts
        )
    ):
        raise ValueError("Expected a new, isolated 8789 staging directory.")
    manifest = json.loads((HERE / "runtime_base.json").read_text(encoding="utf-8"))
    verify(BASE, manifest["files"], "before")
    output.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="worker-8789-") as tmp:
        package = Path(tmp) / "codex_remote_worker"
        package.mkdir()
        for path in BASE.rglob("*.py"):
            target = package / path.relative_to(BASE)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(normalized(path))
        for name in ("materials.py", "worker_materials.py"):
            if (package / name).exists():
                raise ValueError("Unexpected module collision: " + name)
            shutil.copyfile(HERE / name, package / name)
        # The temporary directory is not a Git checkout. Only its copy is patched.
        subprocess.run(
            ["git", "apply", "--no-index", "--whitespace=nowarn", str(HERE / "runtime.patch")],
            cwd=package,
            check=True,
        )
        verify(package, manifest["files"], "after")
        (package / "build_8789.py").write_text(
            "BASELINE_COMMIT = " + repr(manifest["commit"]) + "\n", encoding="utf-8"
        )
        for file in package.rglob("*.py"):
            compile(file.read_bytes(), str(file), "exec")
        # TemporaryDirectory may be on another Windows drive.
        shutil.copytree(package, output)
    return manifest["commit"]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print("8789 staged from", stage(args.output))
