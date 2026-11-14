"""Detect changes to the legacy machine-wide Windows sandbox accounts.

Only public account metadata is read. Passwords and sandbox credential stores
are never read or copied. Password age has one-second resolution, so allow a
small sampling tolerance when comparing the last-set times it implies.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import hashlib
import json
import math
import time

from .common import JobError


def _account_state(name):
    import win32security

    class UserInfo1(ctypes.Structure):
        _fields_ = [('name', wintypes.LPWSTR), ('password', ctypes.c_void_p),
                    ('password_age', wintypes.DWORD), ('privilege', wintypes.DWORD),
                    ('home_dir', wintypes.LPWSTR), ('comment', wintypes.LPWSTR),
                    ('flags', wintypes.DWORD), ('script_path', wintypes.LPWSTR)]

    netapi = ctypes.WinDLL('netapi32.dll')
    netapi.NetUserGetInfo.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR,
                                    wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p)]
    netapi.NetUserGetInfo.restype = wintypes.DWORD
    netapi.NetApiBufferFree.argtypes = [ctypes.c_void_p]
    netapi.NetApiBufferFree.restype = wintypes.DWORD
    buffer = ctypes.c_void_p()
    result = netapi.NetUserGetInfo(None, name, 1, ctypes.byref(buffer))
    if result:
        raise OSError(result, 'Sandbox account metadata unavailable')
    try:
        info = ctypes.cast(buffer, ctypes.POINTER(UserInfo1)).contents
        # UF_ACCOUNTDISABLE and UF_LOCKOUT.
        if info.flags & (0x0002 | 0x0010):
            raise OSError('Sandbox account disabled or locked')
        password_set_at = int(time.time()) - info.password_age
    finally:
        netapi.NetApiBufferFree(buffer)
    sid, _, _ = win32security.LookupAccountName(None, name)
    return {'name': name, 'sid': win32security.ConvertSidToStringSid(sid),
            'password_set_at': password_set_at}


def capture_sandbox_state(settings):
    if settings.windows_sandbox != 'elevated':
        return None
    try:
        raw = (settings.codex_home / '.sandbox' / 'setup_marker.json').read_bytes()
        marker = json.loads(raw.decode('utf-8-sig'))
        names = [marker['offline_username'], marker['online_username']]
        if (not isinstance(marker.get('version'), int) or marker['version'] < 1
                or any(not isinstance(name, str) or not name or len(name) > 256
                       or '\\' in name or '/' in name for name in names)
                or len(set(names)) != 2):
            raise ValueError('Invalid sandbox marker')
        return {'marker_sha256': hashlib.sha256(raw).hexdigest(),
                'accounts': [_account_state(name) for name in names]}
    except Exception:
        # Account APIs can contain machine details; expose only a fixed message.
        raise JobError('SANDBOX_STATE_CHANGED',
                       'Windows 沙箱账户或预置状态不可用；请在服务启动器中点击“修复沙箱”。', 503) from None


def same_sandbox_state(expected, current):
    if not isinstance(expected, dict) or not isinstance(current, dict):
        return False
    if expected.get('marker_sha256') != current.get('marker_sha256'):
        return False
    try:
        before, after = expected['accounts'], current['accounts']
        if len(before) != 2 or len(after) != 2:
            return False
        before = {item['name']: item for item in before}
        after = {item['name']: item for item in after}
        if len(before) != 2 or before.keys() != after.keys():
            return False
        for name, item in before.items():
            other = after[name]
            stamp, next_stamp = item['password_set_at'], other['password_set_at']
            if (item['sid'] != other['sid'] or not math.isfinite(stamp)
                    or not math.isfinite(next_stamp) or abs(stamp - next_stamp) > 3):
                return False
    except (KeyError, TypeError, ValueError, OverflowError):
        return False
    return True
