"""Native Windows process ownership. No process is resumed before joining its Job Object."""
from __future__ import annotations

import os
import subprocess
import time
import threading

from .common import JobError


_owner_job = None
_owner_lock = threading.Lock()


def ensure_owner_job():
    """Cover a controller crash between CreateProcess(SUSPENDED) and per-task assignment."""
    global _owner_job
    import win32api
    import win32job
    with _owner_lock:
        if _owner_job is not None:
            return
        handle = None
        try:
            handle = win32job.CreateJobObject(None, '')
            limits = win32job.QueryInformationJobObject(handle, win32job.JobObjectExtendedLimitInformation)
            limits['BasicLimitInformation']['LimitFlags'] |= win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            win32job.SetInformationJobObject(handle, win32job.JobObjectExtendedLimitInformation, limits)
            win32job.AssignProcessToJobObject(handle, win32api.GetCurrentProcess())
        except Exception as exc:
            if handle is not None:
                handle.Close()
            raise JobError('PROCESS_OWNERSHIP_FAILED', '无法建立服务进程所有权容器，拒绝启动任务。', 503) from exc
        # Keep this non-inheritable handle for the controller's entire lifetime.
        _owner_job = handle


class WindowsProcess:
    def __init__(self, command, cwd, env, name, on_created=lambda metadata: None):
        if os.name != 'nt':
            raise JobError('WINDOWS_REQUIRED', '正式执行器只允许在 Windows 运行。', 503)
        import msvcrt
        import win32api
        import win32con
        import win32event
        import win32job
        import win32process

        ensure_owner_job()

        self.api, self.event, self.jobs, self.process = win32api, win32event, win32job, win32process
        self.handle = self.job = None
        self.stdin = self.stdout = self.stderr = None
        descriptors = []
        thread = None
        try:
            self.job = win32job.CreateJobObject(None, name or '')
            if win32api.GetLastError() == 183:
                raise JobError('PROCESS_OWNERSHIP_FAILED', '任务进程容器已存在，拒绝复用。')
            limits = win32job.QueryInformationJobObject(self.job, win32job.JobObjectExtendedLimitInformation)
            limits['BasicLimitInformation']['LimitFlags'] |= win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            win32job.SetInformationJobObject(self.job, win32job.JobObjectExtendedLimitInformation, limits)
            stdin_r, stdin_w = os.pipe()
            descriptors.extend([stdin_r, stdin_w])
            stdout_r, stdout_w = os.pipe()
            descriptors.extend([stdout_r, stdout_w])
            stderr_r, stderr_w = os.pipe()
            descriptors.extend([stderr_r, stderr_w])
            child_fds = [stdin_r, stdout_w, stderr_w]
            for fd in child_fds:
                os.set_inheritable(fd, True)
            startup = win32process.STARTUPINFO()
            startup.dwFlags |= win32con.STARTF_USESTDHANDLES
            startup.hStdInput = msvcrt.get_osfhandle(stdin_r)
            startup.hStdOutput = msvcrt.get_osfhandle(stdout_w)
            startup.hStdError = msvcrt.get_osfhandle(stderr_w)
            flags = win32con.CREATE_SUSPENDED | win32con.CREATE_NO_WINDOW | win32con.CREATE_UNICODE_ENVIRONMENT
            self.handle, thread, self.pid, _ = win32process.CreateProcess(
                command[0], subprocess.list2cmdline(command), None, None, True, flags, env, str(cwd), startup)
            win32job.AssignProcessToJobObject(self.job, self.handle)
            self.metadata = {'pid': self.pid, 'created_at': win32process.GetProcessTimes(self.handle)['CreationTime'].timestamp(),
                             'job_object': name}
            on_created(self.metadata)
            self.stdin = os.fdopen(stdin_w, 'wb', buffering=0)
            descriptors.remove(stdin_w)
            self.stdout = os.fdopen(stdout_r, 'rb', buffering=64 * 1024)
            descriptors.remove(stdout_r)
            self.stderr = os.fdopen(stderr_r, 'rb', buffering=64 * 1024)
            descriptors.remove(stderr_r)
            win32process.ResumeThread(thread)
        except BaseException:
            if self.handle is not None:
                win32process.TerminateProcess(self.handle, 1)
            self.close()
            raise
        finally:
            if thread is not None:
                thread.Close()
            for fd in descriptors:
                os.close(fd)

    def poll(self):
        if self.event.WaitForSingleObject(self.handle, 0) == 258:
            return None
        return self.process.GetExitCodeProcess(self.handle)

    def active_count(self):
        info = self.jobs.QueryInformationJobObject(self.job, self.jobs.JobObjectBasicAccountingInformation)
        return info['ActiveProcesses']

    def active_processes(self):
        """Identify owned processes by PID and creation time, including packaged Core."""
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        query = kernel.QueryInformationJobObject
        query.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
                          ctypes.POINTER(wintypes.DWORD)]
        query.restype = wintypes.BOOL
        class ProcessList(ctypes.Structure):
            _fields_ = [('assigned', wintypes.DWORD), ('count', wintypes.DWORD),
                        ('pids', ctypes.c_size_t * 1024)]
        value = ProcessList()
        if not query(int(self.job), 3, ctypes.byref(value), ctypes.sizeof(value), None):
            raise JobError('PROCESS_INSPECTION_FAILED', '无法核验 App Server 进程成员。')
        if value.count != value.assigned or value.count > 1024:
            raise JobError('PROCESS_INSPECTION_FAILED', 'App Server 进程列表不完整。')
        members = []
        for pid in value.pids[:value.count]:
            handle = self.api.OpenProcess(0x1000, False, pid)
            try:
                members.append((pid, self.process.GetProcessTimes(handle)['CreationTime'].timestamp()))
            finally:
                handle.Close()
        return tuple(sorted(members))

    def process_details(self, members):
        """Resolve owned PID identities, images and parents without invoking a shell."""
        import ctypes
        from ctypes import wintypes
        kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        class Entry(ctypes.Structure):
            _fields_=[('size',wintypes.DWORD),('usage',wintypes.DWORD),('pid',wintypes.DWORD),
                ('heap',ctypes.c_size_t),('module',wintypes.DWORD),('threads',wintypes.DWORD),
                ('parent',wintypes.DWORD),('priority',wintypes.LONG),('flags',wintypes.DWORD),('exe',wintypes.WCHAR*260)]
        kernel.CreateToolhelp32Snapshot.restype=wintypes.HANDLE
        snapshot=kernel.CreateToolhelp32Snapshot(2,0)
        if snapshot==wintypes.HANDLE(-1).value:raise OSError('Process snapshot failed')
        parents={}
        try:
            entry=Entry();entry.size=ctypes.sizeof(entry)
            ok=kernel.Process32FirstW(wintypes.HANDLE(snapshot),ctypes.byref(entry))
            while ok:
                parents[entry.pid]=entry.parent
                ok=kernel.Process32NextW(wintypes.HANDLE(snapshot),ctypes.byref(entry))
        finally:kernel.CloseHandle(wintypes.HANDLE(snapshot))
        result={}
        for pid,created in members:
            handle=self.api.OpenProcess(0x1000,False,pid)
            try:
                if self.process.GetProcessTimes(handle)['CreationTime'].timestamp()!=created:
                    raise OSError('Process identity changed')
                size=wintypes.DWORD(4096);buffer=ctypes.create_unicode_buffer(4096)
                if not kernel.QueryFullProcessImageNameW(wintypes.HANDLE(int(handle)),0,buffer,ctypes.byref(size)):
                    raise OSError('Cannot inspect process image')
                result[pid]={'image':buffer.value,'parent':parents[pid]}
            finally:handle.Close()
        return result

    def terminate_tree(self, timeout=10):
        self.jobs.TerminateJobObject(self.job, 1)
        until = time.monotonic() + timeout
        while self.active_count() and time.monotonic() < until:
            time.sleep(0.05)
        if self.active_count():
            raise JobError('PROCESS_CLEANUP_FAILED', '无法确认任务进程树已停止，已暂停派发。', 503)

    def close(self):
        for name in ['job', 'handle']:
            handle = getattr(self, name, None)
            if handle is not None:
                handle.Close()
                setattr(self, name, None)
        for name in ['stdin', 'stdout', 'stderr']:
            stream = getattr(self, name, None)
            if stream is not None:
                stream.close()
                setattr(self, name, None)


def same_process_alive(metadata):
    if not metadata or not metadata.get('pid'):
        return False
    import win32api
    import win32process
    try:
        handle = win32api.OpenProcess(0x1000, False, metadata['pid'])
    except Exception as exc:
        if getattr(exc, 'winerror', None) in (87, 1168):
            return False
        # Access denied is not evidence that an owned process has stopped.
        return True
    try:
        return (win32process.GetProcessTimes(handle)['CreationTime'].timestamp() == metadata['created_at']
                and win32process.GetExitCodeProcess(handle) == 259)
    finally:
        handle.Close()


def reconcile_process(metadata):
    if not metadata:
        return True
    if os.name != 'nt':
        return False
    import win32job
    try:
        handle = win32job.OpenJobObject(0x001F003F, False, metadata['job_object'])
    except Exception as exc:
        if getattr(exc, 'winerror', None) != 2:
            return False
        return not same_process_alive(metadata)
    try:
        win32job.TerminateJobObject(handle, 1)
        until = time.monotonic() + 10
        while time.monotonic() < until:
            info = win32job.QueryInformationJobObject(handle, win32job.JobObjectBasicAccountingInformation)
            if info['ActiveProcesses'] == 0:
                return True
            time.sleep(.05)
        return False
    finally:
        handle.Close()


