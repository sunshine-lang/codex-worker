"""Explicit Windows checks. These execute fixed probes, never a model prompt."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import re
import shutil
import socket
import time
import uuid

from .common import JobError, now, write_json
from .runner import config_arguments, runtime_environment, environment_fingerprint
from .windows import WindowsProcess, same_process_alive
from .store import InstanceLock
from .sandbox_state import capture_sandbox_state, same_sandbox_state
from .task_runtime import layout


async def capture(command, settings, root, *, timeout=25):
    process = WindowsProcess(command, root / 'work', runtime_environment(settings, root),
                             'Local\\CodexWorkerProbe-' + uuid.uuid4().hex)
    process.stdin.close()
    readers = [asyncio.create_task(asyncio.to_thread(stream.read, 1024 * 1024)) for stream in [process.stdout, process.stderr]]
    try:
        async with asyncio.timeout(timeout):
            while process.poll() is None:
                await asyncio.sleep(.05)
            code = process.poll()
            await asyncio.to_thread(process.terminate_tree)
            data = await asyncio.gather(*readers)
            if any(len(part) >= 1024 * 1024 for part in data):
                raise JobError('PREFLIGHT_FAILED', '环境探测输出超过限制。')
            if code:
                raise JobError('PREFLIGHT_FAILED', '固定环境探测命令失败；检查工具版本和沙箱设置。')
            return b''.join(data).decode('utf-8', errors='replace')
    except TimeoutError:
        raise JobError('SANDBOX_PROBE_TIMEOUT', '环境探针启动超时；请在服务启动器中点击“修复沙箱”，再重新验证。', 503) from None
    finally:
        await asyncio.to_thread(process.terminate_tree)
        await asyncio.gather(*readers, return_exceptions=True)
        process.close()


async def verify(settings, *, live=False):
    if os.name != 'nt':
        raise JobError('WINDOWS_REQUIRED', '沙箱和进程树自检必须在目标 Windows 服务账户下执行。')
    import win32api
    import win32file
    from win32com.shell import shell
    if shell.IsUserAnAdmin():
        raise JobError('EXCESSIVE_PRIVILEGE', '请使用实际的非管理员服务账户做运行自检；沙箱预置由管理员另行完成。')
    if not settings.codex_home.is_dir():
        raise JobError('CODEX_HOME_MISSING', '请将 codex_home 指向当前服务账户已有的 Codex 配置目录。')
    from .config import read_codex_config
    read_codex_config(settings.codex_home)
    initial_sandbox_state = capture_sandbox_state(settings)
    if live and settings.windows_sandbox == 'elevated':
        try:
            receipt = json.loads((settings.data_root / 'preflight.json').read_text(encoding='utf-8'))
        except (OSError, ValueError):
            receipt = {}
        if not same_sandbox_state(receipt.get('sandbox_state'), initial_sandbox_state):
            raise JobError('SANDBOX_STATE_CHANGED', '沙箱状态已变化，需要自动修复后再启动。', 503)
    if win32file.GetDriveType(str(settings.data_root.anchor)) == 4:
        raise JobError('REMOTE_DISK_UNSUPPORTED', '任务数据库和工作目录必须放在 Windows 本地磁盘。')
    settings.data_root.mkdir(parents=True, exist_ok=True)
    # A live probe has its own fixed synthetic workspace and process container.
    # It does not dispatch jobs or rewrite the service's verified receipt.
    lock = None if live else InstanceLock(settings.data_root / '.instance.lock')
    root = settings.data_root / 'preflight' / uuid.uuid4().hex
    home_probe = settings.codex_home / ('.worker-probe-' + uuid.uuid4().hex)
    personal_probe = (settings.protected_read_roots[0] / ('.worker-private-probe-' + uuid.uuid4().hex)
                      if settings.protected_read_roots else None)
    for name in ['work', 'control']:
        (root / name).mkdir(parents=True)
    try:
        executable = settings.codex_executable
        for path in [executable, settings.tool_python, settings.doc_converter]:
            if not path.is_file() or path.suffix.lower() != '.exe':
                raise JobError('PREFLIGHT_FAILED', '请配置原生 codex.exe、python.exe 和 soffice.exe 的绝对路径。')
        version_text = await capture([str(executable), '--version'], settings, root)
        match = re.search(r'codex-cli ([^\s]+)', version_text)
        if not match or match[1] != settings.expected_codex_version:
            raise JobError('VERSION_MISMATCH', 'CLI 版本与部署配置不符，请核对固定版本。')
        tool_script = ('import json,sys,openpyxl,docx,defusedxml;print(json.dumps({'
            '"python":sys.version.split()[0],"openpyxl":openpyxl.__version__,"python_docx":docx.__version__}))')
        tools = json.loads((await capture([str(settings.tool_python), '-c', tool_script], settings, root)).strip())
        # LibreOffice's GUI launcher can hang when asked for console output; its sibling .com launcher owns the console.
        version_executable = settings.doc_converter.with_suffix('.com') if settings.doc_converter.name.lower() == 'soffice.exe' and settings.doc_converter.with_suffix('.com').is_file() else settings.doc_converter
        tools['doc_converter'] = (await capture([str(version_executable), '--headless', '--version'], settings, root)).strip()[:160]
        secret = root / 'control' / 'unreadable-canary.txt'
        secret.write_text('synthetic private canary')
        neighbor = root / 'neighbor.txt'
        neighbor.write_text('synthetic neighbor task')
        home_probe.write_text('synthetic Codex configuration boundary probe')
        if personal_probe is not None:
            personal_probe.write_text('synthetic personal profile boundary probe')
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            listener.listen()
            port = listener.getsockname()[1]
            script = '\n'.join([
                'from pathlib import Path', 'import socket, openpyxl, docx',
                'Path("allowed.txt").write_text("ok")',
                'assert Path("allowed.txt").read_text()=="ok"',
                f'for item in {list(map(str, [secret, neighbor, home_probe, *([personal_probe] if personal_probe is not None else [])]))!r}:',
                '    try: Path(item).read_bytes()',
                '    except (PermissionError, FileNotFoundError): pass',
                '    else: raise AssertionError("outside read allowed")',
                '    try: Path(item).write_text("bad")',
                '    except (PermissionError, FileNotFoundError): pass',
                '    else: raise AssertionError("outside write allowed")',
                f'try: socket.create_connection(("127.0.0.1", {port}), timeout=2).close()',
                'except OSError: pass',
                'else: raise AssertionError("command networking allowed")',
                'print("SANDBOX_PROBE_OK")',
            ])
            probe = root / 'work' / 'probe.py'
            probe.write_text(script)
            base = [str(executable), *config_arguments(settings, root), 'sandbox',
                    '--permission-profile', 'translation', '--include-managed-config', '--sandbox-state-disable-network',
                    '--cd', str(root / 'work'), '--']
            output = await capture(base + [str(settings.tool_python), str(probe)], settings, root)
            if 'SANDBOX_PROBE_OK' not in output:
                raise JobError('PREFLIGHT_FAILED', '沙箱未通过文件和网络隔离探测。')
        # Exercise the exact absolute-path, Unicode and CSV tools recommended to the Agent.
        # This deliberately invokes Windows PowerShell in its constrained sandbox.
        source = root / 'work' / 'probe-source.csv'
        source.write_bytes(b'\xef\xbb\xbfen,zh-CN,reference\r\nHello,,"quoted, value"\r\n')
        probe_inputs = {'source_file': {'filename': source.name}}
        for role in ('project_style', 'general_style'):
            guide = root / 'work' / (role + '.md')
            guide.write_text('中文风格指南\nPreserve terms and numbers.\n', encoding='utf-8')
            probe_inputs[role] = {'filename': guide.name, 'sha256': 'synthetic-' + role}
        from .qa import read_source
        layout(settings, {'inputs': probe_inputs, 'output_filename': 'probe-source_ZH-CN.csv'},
               root, read_source(source), cache=False)
        helper = str(root / 'work' / '.worker' / 'task_tools.py').replace("'", "''")
        python = str(settings.tool_python).replace("'", "''")
        powershell = Path(os.environ['SystemRoot']) / 'System32' / 'WindowsPowerShell' / 'v1.0' / 'powershell.exe'
        output = await capture(base + [str(powershell), '-NoProfile', '-NonInteractive', '-Command',
                                      f"& '{python}' -X utf8 '{helper}' smoke"], settings, root)
        if 'RUNTIME_HELPER_OK' not in output or '中文测试' not in output:
            raise JobError('PREFLIGHT_FAILED', '受限 PowerShell 的绝对路径、UTF-8 或 CSV 工具探测未通过。')
        # Prove a sandbox-launched child cannot outlive cancellation of the enclosing job.
        marker = root / 'work' / 'child.txt'
        child = root / 'work' / 'child_probe.py'
        child.write_text('import os,time\nfrom pathlib import Path\nPath("child.txt").write_text(str(os.getpid()))\ntime.sleep(120)\n')
        parent = root / 'work' / 'parent_probe.py'
        parent.write_text('import subprocess,sys,time\nsubprocess.Popen([sys.executable,"child_probe.py"])\ntime.sleep(120)\n')
        process = WindowsProcess(base + [str(settings.tool_python), str(parent)], root / 'work', runtime_environment(settings, root),
                                 'Local\\CodexWorkerProbe-' + uuid.uuid4().hex)
        process.stdin.close()
        try:
            until = time.monotonic() + 25
            while not marker.exists() and process.poll() is None and time.monotonic() < until:
                await asyncio.sleep(.05)
            if not marker.exists():
                raise JobError('PREFLIGHT_FAILED', '无法启动沙箱内子进程探测。')
            import win32process
            child_handle = win32api.OpenProcess(0x1000, False, int(marker.read_text()))
            try:
                child_meta = {'pid': int(marker.read_text()), 'created_at': win32process.GetProcessTimes(child_handle)['CreationTime'].timestamp()}
            finally:
                child_handle.Close()
            await asyncio.to_thread(process.terminate_tree)
            if same_process_alive(child_meta):
                raise JobError('PROCESS_CLEANUP_FAILED', '取消后沙箱内子进程仍存活。')
        finally:
            await asyncio.to_thread(process.terminate_tree)
            process.close()
        final_sandbox_state = capture_sandbox_state(settings)
        if settings.windows_sandbox == 'elevated' and not same_sandbox_state(initial_sandbox_state, final_sandbox_state):
            raise JobError('SANDBOX_STATE_CHANGED', '自检期间沙箱账户发生变化；请暂停其他 Codex 的沙箱初始化后再修复。', 503)
        receipt = {'passed': True, 'verified_at': now(), 'codex_version': match[1], 'tools': tools,
                   'sandbox_state': final_sandbox_state,
                   'fingerprint': environment_fingerprint(settings), 'model_called': False,
                   'checks': ['workspace_write', 'outside_read_denied', 'outside_write_denied', 'codex_home_denied',
                              'command_network_denied', 'sandbox_child_cleanup', 'absolute_path_helper',
                              'utf8_console', 'csv_preservation']}
        if personal_probe is not None:
            receipt['checks'].append('personal_profile_denied')
        if live:
            receipt['probe_only'] = True
        else:
            write_json(settings.data_root / 'preflight.json', receipt)
        return receipt
    finally:
        home_probe.unlink(missing_ok=True)
        if personal_probe is not None:
            personal_probe.unlink(missing_ok=True)
        shutil.rmtree(root, ignore_errors=True)
        if lock is not None:
            lock.close()


