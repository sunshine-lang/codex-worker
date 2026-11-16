import os
from pathlib import Path
import sys
import time
import uuid
import json
import subprocess

import pytest

from codex_remote_worker.windows import WindowsProcess


pytestmark = [pytest.mark.windows, pytest.mark.skipif(os.name != 'nt', reason='native Windows required')]


def test_process_is_registered_before_it_runs_and_children_stop(tmp_path):
    marker = tmp_path / 'registered'
    started = tmp_path / 'started'
    script = ('from pathlib import Path;import subprocess,sys,time;'
              'assert Path("registered").exists();'
              'subprocess.Popen([sys.executable,"-c","import time;time.sleep(120)"]);'
              'Path("started").write_text("ok");time.sleep(120)')
    process = WindowsProcess([sys.executable, '-c', script], tmp_path, dict(os.environ),
        'Local\\CodexWorkerTest-' + uuid.uuid4().hex, lambda metadata: marker.write_text(str(metadata['pid'])))
    try:
        until = time.monotonic() + 10
        while not started.exists() and time.monotonic() < until:
            time.sleep(.05)
        assert started.exists()
        assert process.active_count() >= 2
        process.terminate_tree()
        assert process.active_count() == 0
    finally:
        process.terminate_tree()
        process.close()


def test_controller_crash_before_task_assignment_does_not_leave_suspended_child(tmp_path):
    from codex_remote_worker.windows import same_process_alive
    marker = tmp_path / 'created.json'
    source_root = str(Path(__file__).parents[1] / 'src')
    script = f'''
import json,os,sys,win32process
from pathlib import Path
sys.path.insert(0,{source_root!r})
from codex_remote_worker.windows import WindowsProcess
original=win32process.CreateProcess
def crash_after_create(*args,**kwargs):
    handle,thread,pid,tid=original(*args,**kwargs)
    Path({str(marker)!r}).write_text(json.dumps({{"pid":pid,"created_at":win32process.GetProcessTimes(handle)["CreationTime"].timestamp()}}))
    os._exit(0)
win32process.CreateProcess=crash_after_create
WindowsProcess([sys.executable,"-c","import time;time.sleep(120)"],{str(tmp_path)!r},dict(os.environ),"Local\\\\CrashProbe-"+str(os.getpid()))
'''
    subprocess.run([sys.executable, '-c', script], check=True, timeout=15)
    metadata = json.loads(marker.read_text())
    until = time.monotonic() + 10
    while same_process_alive(metadata) and time.monotonic() < until:
        time.sleep(.05)
    assert not same_process_alive(metadata)
