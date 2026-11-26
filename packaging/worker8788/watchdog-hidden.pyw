"""Run the 8788 recovery task without opening a console on the operator's desktop."""
import json
from pathlib import Path
import subprocess


if __name__ == "__main__":
    desktop = Path(__file__).resolve().parent
    runtime = json.loads((desktop / "runtime.json").read_text(encoding="utf-8-sig"))
    raise SystemExit(subprocess.call(
        [runtime["pwsh"], "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden",
         "-File", str(desktop / "watchdog.ps1")],
        cwd=desktop, creationflags=subprocess.CREATE_NO_WINDOW,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    ))
