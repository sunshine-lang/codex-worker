import asyncio
import json
from pathlib import Path
import sys
import uuid

from codex_remote_worker import doctor
from codex_remote_worker.common import JobError, Redactor
from codex_remote_worker.config import load_settings
from codex_remote_worker.runner import runtime_environment
from codex_remote_worker.windows import WindowsProcess


async def capture(command, settings, root, *, timeout=25):
    phase = 'sandbox' if 'sandbox' in command else Path(command[0]).name
    print(json.dumps({'phase': phase, 'status': 'started'}), flush=True)
    process = WindowsProcess(command, root / 'work', runtime_environment(settings, root),
                             'Local\\CodexWorkerStartup-' + uuid.uuid4().hex)
    process.stdin.close()
    readers = [asyncio.create_task(asyncio.to_thread(stream.read, 1024 * 1024))
               for stream in [process.stdout, process.stderr]]
    reported = False
    try:
        async with asyncio.timeout(timeout):
            while process.poll() is None:
                await asyncio.sleep(.05)
            code = process.poll()
            await asyncio.to_thread(process.terminate_tree)
            data = await asyncio.gather(*readers)
            output = b''.join(data).decode('utf-8', errors='replace')
            print(json.dumps({'phase': phase, 'exit_code': code,
                              'output': Redactor(settings.secret_values()).text(output)[:16000]}), flush=True)
            reported = True
            if any(len(part) >= 1024 * 1024 for part in data):
                raise JobError('PREFLIGHT_FAILED', 'Probe output limit exceeded')
            if code:
                raise JobError('PREFLIGHT_FAILED', 'Fixed environment probe failed')
            return output
    finally:
        await asyncio.to_thread(process.terminate_tree)
        results = await asyncio.gather(*readers, return_exceptions=True)
        if not reported:
            output = b''.join(part for part in results if isinstance(part, bytes)).decode('utf-8', errors='replace')
            print(json.dumps({'phase': phase, 'status': 'interrupted',
                              'output': Redactor(settings.secret_values()).text(output)[:16000]}), flush=True)
        process.close()


doctor.capture = capture
settings = load_settings(Path(sys.argv[1]), Path(sys.argv[2]))
try:
    print(json.dumps(asyncio.run(doctor.verify(settings))), flush=True)
except JobError as exc:
    print(json.dumps({'error': exc.public()}), flush=True)
    raise SystemExit(1)
