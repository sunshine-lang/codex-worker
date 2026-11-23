"""Windows registered-package entry point, invoked as the existing Worker account."""
import argparse
import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import sys
import traceback

root = Path(__file__).resolve().parent
data = Path(r"C:\CodexWorkerData-AppServer-8790")
args = argparse.ArgumentParser()
args.add_argument("mode", choices=["serve"])
args.add_argument("--operation-id", required=True)
options = args.parse_args()
sys.stdout = (data / "serve.stdout.log").open("a", encoding="utf-8", buffering=1)
sys.stderr = (data / "serve.stderr.log").open("a", encoding="utf-8", buffering=1)
sys.path[:0] = [str(root / "src"), str(root / "vendor")]
from codex_remote_worker.common import write_json, now

def start():
    length = wintypes.UINT(0)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    if kernel.GetCurrentPackageFamilyName(ctypes.byref(length), None) != 122:
        raise RuntimeError("Missing registered Codex desktop package identity")
    family = ctypes.create_unicode_buffer(length.value)
    if kernel.GetCurrentPackageFamilyName(ctypes.byref(length), family) or family.value != "OpenAI.Codex_2p2nqsd0c76g0":
        raise RuntimeError("Unexpected registered desktop package")
    os.environ["CODEX_WINDOWS_REGISTERED_CORE"] = "1"
    os.environ["CODEX_WINDOWS_SANDBOX_PACKAGE_FAMILY"] = family.value
    os.environ["MATERIALS_SOFFICE"] = (root / "office.path").read_text(encoding="utf-8-sig").strip()
    write_json(data / "serve.process.json", {"operation_id": options.operation_id, "ProcessId": os.getpid(), "created_at": now()})
    from codex_remote_worker.material_service import main
    sys.argv = [sys.argv[0], "--config", str(root / "config.trial.json")]
    main()

if __name__ == "__main__":
    try:
        start()
    except Exception:
        traceback.print_exc()
        sys.exit(1)
