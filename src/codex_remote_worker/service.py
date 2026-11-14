"""Optional native Windows Service entry point; configuration paths live in SCM Parameters."""
import os
from pathlib import Path
import threading


if os.name == 'nt':
    import servicemanager
    import win32service
    import win32serviceutil

    class WorkerService(win32serviceutil.ServiceFramework):
        _svc_name_ = 'CodexRemoteWorker'
        _svc_display_name_ = 'Codex Remote Translation Worker'
        _svc_description_ = 'Runs bounded Codex translation jobs for an authenticated Langflow caller.'

        def __init__(self, args):
            super().__init__(args)
            self.stop_requested = threading.Event()
            self.server = None

        def SvcStop(self):
            self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
            self.stop_requested.set()
            if self.server:
                self.server.should_exit = True

        def SvcDoRun(self):
            try:
                from codex_remote_worker.config import load_settings
                from codex_remote_worker.app import create_app
                import uvicorn
                import winreg
                key_path = 'SYSTEM\\CurrentControlSet\\Services\\' + self._svc_name_ + '\\Parameters'
                with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key_path, 0, winreg.KEY_READ) as key:
                    config_path = winreg.QueryValueEx(key, 'ConfigFile')[0]
                    secret_path = winreg.QueryValueEx(key, 'CredentialsFile')[0]
                settings = load_settings(Path(config_path), Path(secret_path))
                self.server = uvicorn.Server(uvicorn.Config(create_app(settings), host=settings.bind_host,
                    port=settings.port, workers=1, access_log=False, timeout_graceful_shutdown=30))
                if not self.stop_requested.is_set():
                    self.server.run()
            except Exception:
                # Windows Event Log must never receive exception repr containing deployment credentials.
                servicemanager.LogErrorMsg('CodexRemoteWorker startup failed; check config, account and preflight.')
                raise RuntimeError('CodexRemoteWorker service failed') from None


def main():
    if os.name != 'nt':
        raise SystemExit('Native Windows is required for service installation.')
    win32serviceutil.HandleCommandLine(WorkerService, serviceClassString='codex_remote_worker.service.WorkerService')


if __name__ == '__main__':
    main()
