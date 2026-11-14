"""Persistent WFP isolation for the dedicated offline sandbox account only."""
import argparse
import ctypes as C
from ctypes import wintypes as W
import json
import uuid
import win32security

class GUID(C.Structure):
    _fields_ = [('data', C.c_ubyte * 16)]
    @classmethod
    def parse(cls, value):
        return cls.from_buffer_copy(uuid.UUID(str(value)).bytes_le)

class DISPLAY(C.Structure):
    _fields_ = [('name', W.LPWSTR), ('description', W.LPWSTR)]

class BLOB(C.Structure):
    _fields_ = [('size', W.DWORD), ('data', C.c_void_p)]

class VALUE_UNION(C.Union):
    _fields_ = [('uint8', C.c_uint8), ('uint16', C.c_uint16), ('uint32', C.c_uint32), ('pointer', C.c_void_p)]

class VALUE(C.Structure):
    _fields_ = [('type', C.c_uint32), ('value', VALUE_UNION)]

class CONDITION(C.Structure):
    _fields_ = [('fieldKey', GUID), ('matchType', C.c_uint32), ('conditionValue', VALUE)]

class ACTION(C.Structure):
    _fields_ = [('type', C.c_uint32), ('filterType', GUID)]

class CONTEXT(C.Union):
    _fields_ = [('rawContext', C.c_uint64), ('providerContextKey', GUID)]

class FILTER(C.Structure):
    _fields_ = [('filterKey', GUID), ('displayData', DISPLAY), ('flags', C.c_uint32),
                ('providerKey', C.POINTER(GUID)), ('providerData', BLOB),
                ('layerKey', GUID), ('subLayerKey', GUID), ('weight', VALUE),
                ('numFilterConditions', C.c_uint32), ('filterCondition', C.POINTER(CONDITION)),
                ('action', ACTION), ('context', CONTEXT), ('reserved', C.POINTER(GUID)),
                ('filterId', C.c_uint64), ('effectiveWeight', VALUE)]

class SUBLAYER(C.Structure):
    _fields_ = [('subLayerKey', GUID), ('displayData', DISPLAY), ('flags', C.c_uint32),
                ('providerKey', C.POINTER(GUID)), ('providerData', BLOB), ('weight', C.c_uint16)]

assert C.sizeof(FILTER) == 200 and C.sizeof(CONDITION) == 40 and C.sizeof(SUBLAYER) == 72
api = C.WinDLL('fwpuclnt.dll', use_last_error=True)
advapi = C.WinDLL('advapi32.dll', use_last_error=True)
kernel = C.WinDLL('kernel32.dll', use_last_error=True)
api.FwpmEngineOpen0.argtypes = [W.LPCWSTR, C.c_uint32, C.c_void_p, C.c_void_p, C.POINTER(W.HANDLE)]
api.FwpmEngineClose0.argtypes = [W.HANDLE]
api.FwpmTransactionBegin0.argtypes = [W.HANDLE, C.c_uint32]
api.FwpmTransactionCommit0.argtypes = [W.HANDLE]
api.FwpmTransactionAbort0.argtypes = [W.HANDLE]
api.FwpmSubLayerAdd0.argtypes = [W.HANDLE, C.POINTER(SUBLAYER), C.c_void_p]
api.FwpmSubLayerDeleteByKey0.argtypes = [W.HANDLE, C.POINTER(GUID)]
api.FwpmFilterAdd0.argtypes = [W.HANDLE, C.POINTER(FILTER), C.c_void_p, C.POINTER(C.c_uint64)]
api.FwpmFilterDeleteByKey0.argtypes = [W.HANDLE, C.POINTER(GUID)]
for function_name in ['FwpmEngineOpen0', 'FwpmEngineClose0', 'FwpmTransactionBegin0', 'FwpmTransactionCommit0', 'FwpmTransactionAbort0', 'FwpmSubLayerAdd0', 'FwpmSubLayerDeleteByKey0', 'FwpmFilterAdd0', 'FwpmFilterDeleteByKey0']:
    getattr(api, function_name).restype = W.DWORD
advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [W.LPCWSTR, W.DWORD, C.POINTER(C.c_void_p), C.POINTER(W.DWORD)]
advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = W.BOOL
kernel.LocalFree.argtypes = [C.c_void_p]
kernel.LocalFree.restype = C.c_void_p

def check(code, label, allowed=()):
    if code and code not in allowed:
        raise RuntimeError(f'{label}: 0x{code:08x}')

parser = argparse.ArgumentParser()
parser.add_argument('--remove', action='store_true')
args = parser.parse_args()
account = 'CodexSandboxOffline'
sid, domain, sid_type = win32security.LookupAccountName(None, account)
sid_text = win32security.ConvertSidToStringSid(sid)
if sid_type != win32security.SidTypeUser or not sid_text.startswith('S-1-5-21-') or sid_text.endswith(('-500', '-501')):
    raise RuntimeError('Unexpected sandbox account; refusing to create filters')
sublayer_key = GUID.parse(uuid.uuid5(uuid.NAMESPACE_URL, 'codex-remote-worker:offline-lockdown:sublayer'))
layers = {
    'connect_v4': 'c38d57d1-05a7-4c33-904f-7fbceee60e82',
    'connect_v6': '4a72393b-319f-44bc-84c3-ba54dcb3b6b4',
    'accept_v4': 'e1cd9fe7-f4b5-4273-96c0-592e487b8650',
    'accept_v6': 'a3b42c97-9f04-4672-b87e-cee9c483257f',
}
engine = W.HANDLE()
check(api.FwpmEngineOpen0(None, 10, None, None, C.byref(engine)), 'open WFP engine')
security_descriptor = C.c_void_p()
security_size = W.DWORD()
transaction_started = False
try:
    check(api.FwpmTransactionBegin0(engine, 0), 'begin transaction')
    transaction_started = True
    if not args.remove:
        sublayer = SUBLAYER()
        sublayer.subLayerKey = sublayer_key
        sublayer.displayData.name = 'Codex Remote Worker offline isolation'
        sublayer.displayData.description = 'Blocks network connections for CodexSandboxOffline only'
        sublayer.flags = 1
        sublayer.weight = 65535
        check(api.FwpmSubLayerAdd0(engine, C.byref(sublayer), None), 'add sublayer', (0x80320009,))
        if not advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW('D:(A;;CC;;;' + sid_text + ')', 1, C.byref(security_descriptor), C.byref(security_size)):
            raise C.WinError(C.get_last_error())
        descriptor_blob = BLOB(security_size.value, security_descriptor)
    added = []
    for label, layer_guid in layers.items():
        filter_key = GUID.parse(uuid.uuid5(uuid.NAMESPACE_URL, 'codex-remote-worker:offline-lockdown:' + label))
        check(api.FwpmFilterDeleteByKey0(engine, C.byref(filter_key)), 'delete previous filter', (0x80320003,))
        if args.remove:
            continue
        condition = CONDITION()
        condition.fieldKey = GUID.parse('af043a0a-b34d-4f86-979c-c90371af6e66')
        condition.matchType = 0
        condition.conditionValue.type = 14
        condition.conditionValue.value.pointer = C.addressof(descriptor_blob)
        network_filter = FILTER()
        network_filter.filterKey = filter_key
        network_filter.displayData.name = 'Codex Remote Worker offline block ' + label
        network_filter.displayData.description = 'Applies only to ' + domain + '\\' + account + ' (' + sid_text + ')'
        network_filter.flags = 1
        network_filter.layerKey = GUID.parse(layer_guid)
        network_filter.subLayerKey = sublayer_key
        network_filter.weight.type = 1
        network_filter.weight.value.uint8 = 15
        network_filter.numFilterConditions = 1
        network_filter.filterCondition = C.pointer(condition)
        network_filter.action.type = 0x1001
        filter_id = C.c_uint64()
        check(api.FwpmFilterAdd0(engine, C.byref(network_filter), None, C.byref(filter_id)), 'add ' + label)
        added.append({'layer': label, 'filter_id': filter_id.value})
    if args.remove:
        check(api.FwpmSubLayerDeleteByKey0(engine, C.byref(sublayer_key)), 'delete sublayer', (0x80320007,))
    check(api.FwpmTransactionCommit0(engine), 'commit filters')
    transaction_started = False
    print(json.dumps({'account': domain + '\\' + account, 'sid': sid_text, 'removed': args.remove, 'filters': added}))
finally:
    if transaction_started:
        api.FwpmTransactionAbort0(engine)
    if security_descriptor:
        kernel.LocalFree(security_descriptor)
    api.FwpmEngineClose0(engine)
