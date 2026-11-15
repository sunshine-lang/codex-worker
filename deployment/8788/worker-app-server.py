from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parent / "src"))
from codex_remote_worker.app_server_service import main
raise SystemExit(main())
