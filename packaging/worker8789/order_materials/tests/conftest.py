"""Test the actual 8789 staging artifact, never a live service or the base package."""

import importlib.util
from pathlib import Path
import sys
from tempfile import TemporaryDirectory

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
from stage_runtime import BASE, stage

_tmp = TemporaryDirectory(prefix="worker8789-pytest-")
_build = Path(_tmp.name)
stage(_build / "codex_remote_worker")
sys.path.insert(0, str(_build))
BASE_TESTS = BASE.parents[1] / "tests"
sys.path.insert(1, str(BASE_TESTS))
spec = importlib.util.spec_from_file_location("baseline_worker_fixtures", BASE_TESTS / "conftest.py")
baseline = importlib.util.module_from_spec(spec)
spec.loader.exec_module(baseline)
settings = baseline.settings
