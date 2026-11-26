"""Run the installed 8789 recovery check without allocating a desktop console."""
import json
from pathlib import Path
import subprocess


def main():
    desktop = Path(__file__).resolve().parent
    runtime = json.loads((desktop / "runtime.json").read_text(encoding="utf-8-sig"))
    return subprocess.call(
        [runtime["pwsh"], "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden",
         "-File", str(desktop / "watchdog.ps1")],
        cwd=desktop,
        creationflags=subprocess.CREATE_NO_WINDOW,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


if __name__ == "__main__":
    raise SystemExit(main())
