"""Frozen worker entry point. Credentials and deployment paths stay external."""
import ctypes
import importlib
import multiprocessing
import os
from pathlib import Path
import sys


def main():
    # Preload native extensions before restoring the DLL search path for children.
    if sys.platform == 'win32' and getattr(sys, 'frozen', False):
        for module in ('pywintypes', 'pythoncom', 'win32api', 'win32con', 'win32event',
                       'win32file', 'win32job', 'win32process', 'win32security',
                       'win32com.shell.shell'):
            importlib.import_module(module)
        ctypes.windll.kernel32.SetDllDirectoryW(None)
        bundle_root = Path(sys._MEIPASS).resolve()
        path_entries = []
        for entry in os.environ.get('PATH', '').split(os.pathsep):
            if entry and not Path(entry).resolve().is_relative_to(bundle_root):
                path_entries.append(entry)
        os.environ['PATH'] = os.pathsep.join(path_entries)
    from codex_remote_worker.__main__ import main as worker_main
    if '--startup-trace' in sys.argv:
        sys.argv.remove('--startup-trace')
        import traceback
        from codex_remote_worker.common import Redactor
        def trace_main(frame, event, argument):
            if frame.f_globals.get('__name__') != 'codex_remote_worker.__main__':
                return None
            if event == 'exception':
                error_type, error, tb = argument
                settings = frame.f_locals.get('settings')
                if settings is not None:
                    details = ''.join(traceback.format_exception(error_type, error, tb))
                    print(Redactor(settings.secret_values()).text(details), file=sys.stderr, flush=True)
                else:
                    print('Startup exception: ' + error_type.__name__, file=sys.stderr, flush=True)
            return trace_main
        sys.settrace(trace_main)
        try:
            return worker_main()
        finally:
            sys.settrace(None)
    return worker_main()


if __name__ == '__main__':
    multiprocessing.freeze_support()
    raise SystemExit(main())

