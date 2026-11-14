"""Per-user portable controller. Never installs Codex or changes its credentials/config."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import ctypes
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, ProxyHandler, build_opener
import uuid


DEFAULTS = {"port": 8787, "listen_address": "0.0.0.0", "codex_executable": "",
            "codex_home": "", "public_api_url": ""}
ACTIVE = ("queued", "running", "checking", "cancelling")
REQUIRED_CONTRACTS = frozenset(("siemens-json/2", "siemens-json/1", "table-csv/1"))


def require_service(ready):
    if not ready.get("ready") or not REQUIRED_CONTRACTS.issubset(ready.get("input_contracts", [])):
        raise PortableError("WORKER_UPGRADE_REQUIRED", "本实例未就绪或缺少 /2 项目归档能力；请空闲停止旧服务后用完整新包启动。")


class PortableError(Exception):
    def __init__(self, code, message):
        self.code, self.message = code, message
        super().__init__(message)


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def save_json(path, value):
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def user_sid():
    import win32api
    import win32con
    import win32security
    token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
    try:
        return win32security.ConvertSidToStringSid(win32security.GetTokenInformation(token, win32security.TokenUser)[0])
    finally:
        token.Close()


def require_user_session():
    if os.name != "nt" or not ctypes.sizeof(ctypes.c_void_p) == 8:
        raise PortableError("WINDOWS_X64_REQUIRED", "请在 Windows x64 本地磁盘上运行。")
    from win32com.shell import shell
    if shell.IsUserAnAdmin():
        raise PortableError("USER_CONTEXT_REQUIRED", "当前 Windows 进程带有管理员权限，尚未检查 Codex 登录；这不表示官方订阅无效。请使用已登录 Codex 的同一 Windows 用户的非提权会话。若当前是内置 Administrator，请切换到标准 Windows 用户，在该用户下登录你现有的官方账号，再完整解压本包并启动。")


def protect_directory(path):
    import win32security
    path.mkdir(parents=True, exist_ok=True)
    dacl = win32security.ACL()
    for sid in (user_sid(), "S-1-5-18", "S-1-5-32-544"):
        dacl.AddAccessAllowedAceEx(win32security.ACL_REVISION_DS, 3, 0x1F01FF,
                                  win32security.ConvertStringSidToSid(sid))
    win32security.SetNamedSecurityInfo(str(path), win32security.SE_FILE_OBJECT,
        win32security.DACL_SECURITY_INFORMATION | win32security.PROTECTED_DACL_SECURITY_INFORMATION,
        None, None, dacl, None)


def read_options(root):
    values = read_json(root / "worker-settings.json")
    if not isinstance(values, dict) or values.keys() - DEFAULTS.keys():
        raise PortableError("INVALID_SETTINGS", "worker-settings.json 包含不支持的字段。")
    options = {**DEFAULTS, **values}
    if type(options["port"]) is not int or not 1 <= options["port"] <= 65535:
        raise PortableError("INVALID_PORT", "端口必须是 1 至 65535 的整数。")
    if options["listen_address"] not in ("127.0.0.1", "0.0.0.0"):
        raise PortableError("INVALID_BIND", "listen_address 只支持 127.0.0.1 或 0.0.0.0。")
    for name in ("codex_home", "codex_executable", "public_api_url"):
        if not isinstance(options[name], str) or any(c in options[name] for c in "\x00\r\n"):
            raise PortableError("INVALID_SETTINGS", "配置路径和地址必须是单行字符串。")
    if options["public_api_url"]:
        try:
            url = urlsplit(options["public_api_url"])
            if url.scheme not in ("http", "https") or not url.hostname or url.username or url.password or url.query or url.fragment:
                raise ValueError()
            _ = url.port
        except ValueError:
            raise PortableError("INVALID_PUBLIC_URL", "public_api_url 应为不包含密码、查询或片段的 HTTP/HTTPS 地址。") from None
    return options


def executable_path(value):
    path = Path(os.path.expandvars(value)).expanduser()
    return path.resolve() if path.is_absolute() and path.is_file() and path.suffix.lower() == ".exe" else None


def desktop_package_candidates():
    # Query only this user's package registration; do not recurse through personal files.
    powershell = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    try:
        result = subprocess.run([str(powershell), "-NoProfile", "-NonInteractive", "-Command",
            "[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false); Get-AppxPackage -Name OpenAI.Codex | Select-Object -ExpandProperty InstallLocation"],
            capture_output=True, timeout=15, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if result.returncode == 0:
            for line in result.stdout.decode("utf-8", errors="replace").splitlines():
                location = Path(line.strip())
                if location.is_absolute():
                    # Never select app/Codex.exe: that is the Electron GUI, not the CLI.
                    yield location / "app/resources/codex.exe"
    except (OSError, subprocess.TimeoutExpired):
        return


def codex_candidates(options):
    if options["codex_executable"]:
        yield options["codex_executable"]
        return
    for name in ("codex.exe", "codex"):
        value = shutil.which(name)
        if value and Path(value).suffix.lower() == ".exe":
            yield value
    local = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local")))
    base = local / "OpenAI/Codex/bin"
    if base.is_dir():
        for child in sorted(base.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
            yield child / "codex.exe"
    yield from desktop_package_candidates()
    npm = Path(os.environ.get("APPDATA", str(Path.home() / "AppData/Roaming"))) / "npm/node_modules/@openai"
    for base in (npm / "codex", npm / "codex/node_modules/@openai/codex-win32-x64", npm / "codex-win32-x64"):
        yield base / "vendor/x86_64-pc-windows-msvc/codex/codex.exe"


def find_codex(options):
    seen = set()
    for value in codex_candidates(options):
        path = executable_path(str(value))
        if path is None or str(path).casefold() in seen:
            continue
        seen.add(str(path).casefold())
        try:
            result = subprocess.run([str(path), "--version"], capture_output=True, timeout=15,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            match = re.search(rb"\bcodex-cli ([0-9][^\s]*)", result.stdout + result.stderr)
            if result.returncode == 0 and match:
                return path, match[1].decode("ascii")
        except (OSError, UnicodeError, subprocess.TimeoutExpired):
            pass
    raise PortableError("CODEX_CLI_NOT_FOUND", "未找到可调用的原生 codex.exe。请先用当前用户打开 Codex；如已安装，在 worker-settings.json 的 codex_executable 填写 CLI 的绝对路径（不是桌面 GUI 的 Codex.exe）。不会下载或替换 Codex。")


def personal_roots():
    from win32com.shell import shell, shellcon
    roots = [Path.home().resolve()]
    for key in (shellcon.CSIDL_DESKTOPDIRECTORY, shellcon.CSIDL_PERSONAL,
                shellcon.CSIDL_MYPICTURES, shellcon.CSIDL_MYMUSIC, shellcon.CSIDL_MYVIDEO):
        path = Path(shell.SHGetFolderPath(0, key, None, 0)).resolve()
        if path not in roots:
            roots.append(path)
    return roots


def runtime_environment(root, config):
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("CODEX_")
           and k.upper() not in ("OPENAI_API_KEY", "OPENROUTER_API_KEY", "OPENAI_BASE_URL", "PYTHONPATH", "PYTHONHOME")}
    env.update(CODEX_HOME=config["codex_home"], PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    system = Path(os.environ.get("SystemRoot", r"C:\Windows"))
    env["PATH"] = os.pathsep.join(map(str, [root / ".runtime/python313", Path(config["codex_executable"]).parent,
        root / "tools/LibreOffice/program", system / "System32", system, system / "System32/WindowsPowerShell/v1.0"]))
    return env


def make_config(root, options):
    import win32file
    if win32file.GetDriveType(str(root.anchor)) == 4:
        raise PortableError("LOCAL_DISK_REQUIRED", "请解压到 Windows 本地磁盘，不要从网络共享运行。")
    codex, version = find_codex(options)
    value = options["codex_home"] or os.environ.get("CODEX_HOME") or str(Path.home() / ".codex")
    home = Path(os.path.expandvars(value)).expanduser()
    if not home.is_absolute() or not home.is_dir():
        raise PortableError("CODEX_HOME_MISSING", "找不到现有 Codex 配置目录。请先在当前用户登录 Codex；自定义目录可在 worker-settings.json 的 codex_home 指定。")
    config = {"data_root": str(root / "Data"), "codex_executable": str(codex), "codex_home": str(home.resolve()),
        "expected_codex_version": version, "tool_python": str(root / ".runtime/python313/python.exe"),
        "doc_converter": str(root / "tools/LibreOffice/program/soffice.exe"),
        "tool_read_roots": [str(root / ".runtime/python313"), str(codex.parent), str(root / "tools/LibreOffice/program")],
        "authentication_mode": "chatgpt", "protected_read_roots": list(map(str, personal_roots())),
        "windows_sandbox": "mxc", "bind_host": options["listen_address"], "port": options["port"],
        "queue_capacity": 10, "min_free_bytes": 536870912}
    from types import SimpleNamespace
    from codex_remote_worker.subscription import effective_config
    current = effective_config(SimpleNamespace(codex_home=home.resolve()))
    mode = current.get("windows", {}).get("sandbox")
    if mode is None:
        mode = "elevated" if (home / ".sandbox/setup_marker.json").is_file() else "mxc"
    if mode not in ("mxc", "elevated"):
        raise PortableError("SANDBOX_UNSUPPORTED", "当前 Codex 沙箱模式不兼容；未改动原配置。")
    config["windows_sandbox"] = mode
    return config


def ensure_token(root):
    private = root / "Data/private"
    protect_directory(private)
    path = private / "credentials.local.json"
    if path.exists():
        value = read_json(path)
        token = value.get("CODEX_WORKER_SERVICE_TOKEN")
        if set(value) != {"CODEX_WORKER_SERVICE_TOKEN"} or not isinstance(token, str) or len(token) < 32:
            raise PortableError("INVALID_WORKER_TOKEN", "已有 Worker Token 文件不合法；不会覆盖它。")
    else:
        token = secrets.token_urlsafe(48)
        save_json(path, {"CODEX_WORKER_SERVICE_TOKEN": token})
    return token


def check_port(config):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            if os.name == "nt":
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            sock.bind((config["bind_host"], config["port"]))
        except OSError:
            raise PortableError("PORT_IN_USE", "端口已被占用或被系统保留；请修改 worker-settings.json 的 port，或确认占用程序。不会停止其他进程。") from None


def run_probe(command, root, config, name, timeout=180):
    logs = root / "Data/logs"
    logs.mkdir(exist_ok=True)
    try:
        result = subprocess.run(list(map(str, command)), cwd=root, env=runtime_environment(root, config),
            stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired:
        raise PortableError("CHECK_TIMEOUT", "检查超时：" + name + "。请在 Codex 桌面端确认沙箱可正常执行，再重试。") from None
    # These are fixed core probes with synthetic inputs, NOT raw `codex login status`.
    from codex_remote_worker.common import Redactor
    from codex_remote_worker.config import load_settings
    settings = load_settings(root / "Data/config.local.json", root / "Data/private/credentials.local.json")
    redactor = Redactor(settings.secret_values())
    for suffix, payload in (("stdout", result.stdout), ("stderr", result.stderr)):
        (logs / (name + "." + suffix + ".log")).write_text(
            redactor.text(payload.decode("utf-8", errors="replace")), encoding="utf-8")
    try:
        value = json.loads(result.stdout.decode("utf-8-sig"))
    except (ValueError, UnicodeError):
        value = {}
    if result.returncode != 0 or not value.get("passed", value.get("ready", False)):
        code = value.get("code", value.get("error", {}).get("code", "CHECK_FAILED"))
        message = value.get("message", value.get("error", {}).get("message", "请查看 Data/logs 中对应检查日志。"))
        raise PortableError(str(code), name + " 未通过：" + redactor.text(message))
    return value


def query_ready(config, token, timeout=15):
    request = Request("http://127.0.0.1:" + str(config["port"]) + "/v1/ready",
                      headers={"Authorization": "Bearer " + token})
    opener = build_opener(ProxyHandler({}))
    try:
        with opener.open(request, timeout=timeout) as response:
            value = json.load(response)
            return value if isinstance(value, dict) else {"ready": False, "code": "INVALID_READY"}
    except HTTPError as exc:
        try:
            value = json.loads(exc.read(65536))
            return value if isinstance(value, dict) else {"ready": False, "code": "HTTP_" + str(exc.code)}
        except ValueError:
            return {"ready": False, "code": "HTTP_" + str(exc.code)}
    except (OSError, URLError, ValueError):
        return {"ready": False, "code": "API_UNREACHABLE"}


def owned_process(root, record, *, terminate=False):
    import win32api
    import win32process
    expected = root / "bin/CodexRemoteWorker/CodexRemoteWorker.exe"
    if not isinstance(record, dict) or record.get("root") != str(root) or record.get("user_sid") != user_sid() or record.get("executable") != str(expected):
        raise PortableError("PROCESS_IDENTITY_MISMATCH", "运行记录不属于本用户/目录，拒绝操作其他服务。")
    try:
        handle = win32api.OpenProcess(0x1000 | 0x100000 | (1 if terminate else 0), False, int(record["pid"]))
    except Exception as exc:
        if getattr(exc, "winerror", None) in (87, 1168):
            return None
        raise PortableError("PROCESS_QUERY_FAILED", "无法安全确认原服务身份；不会尝试强制停止其他程序。") from None
    try:
        if win32process.GetExitCodeProcess(handle) != 259:
            handle.Close()
            return None
        from ctypes import wintypes
        query = ctypes.windll.kernel32.QueryFullProcessImageNameW
        query.argtypes = (wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD))
        query.restype = wintypes.BOOL
        size = wintypes.DWORD(32768)
        buffer = ctypes.create_unicode_buffer(size.value)
        if not query(int(handle), 0, buffer, ctypes.byref(size)):
            raise PortableError("PROCESS_QUERY_FAILED", "无法核实进程路径，拒绝继续。")
        current = win32process.GetProcessTimes(handle)["CreationTime"].timestamp()
        if current != record["created_at"] or Path(buffer.value).resolve() != expected:
            handle.Close()
            return None  # stale PID; never kill the process that reused it
        return handle
    except BaseException:
        handle.Close()
        raise


def load_running(root):
    path = root / "Data/worker.process.json"
    if not path.is_file():
        return None
    record = read_json(path)
    handle = owned_process(root, record)
    if handle is None:
        return None
    handle.Close()
    return record


def connection_file(root, options, config, token):
    url = options["public_api_url"].rstrip("/") or "http://127.0.0.1:" + str(config["port"])
    text = "token: " + token + "\napi: " + url + "\n\n"
    text += "127.0.0.1 只供本机检查；外部调用请使用目标机器的 IP/域名或内网穿透地址。\n"
    text += "公网入口请使用 HTTPS、保留 Bearer Token，并限制可信调用来源。请勿公开分享本文件。\n"
    (root / "Data/private/connection.txt").write_text(text, encoding="utf-8-sig")


def print_ready(root, config, *, reused=False):
    print("服务已就绪" + ("（复用已有实例）" if reused else "") + "。")
    print("本机检查地址：http://127.0.0.1:" + str(config["port"]))
    print("监听地址：" + config["bind_host"] + ":" + str(config["port"]))
    print("调用连接信息：" + str(root / "Data/private/connection.txt"))
    print("可以关闭本窗口；服务继续后台运行。停止请双击 stop.cmd。")


def start(root, *, check_only=False):
    options = read_options(root)
    existing = load_running(root)
    if existing:
        config = read_json(root / "Data/config.local.json")
        token = ensure_token(root)
        ready = query_ready(config, token)
        if not ready.get("ready"):
            raise PortableError(str(ready.get("code", "NOT_READY")), "已有 Worker 未就绪。请查看状态和日志；空闲时停止后重试。不会在有任务时自动重启或重置沙箱。")
        require_service(ready)
        connection_file(root, options, config, token)
        print_ready(root, config, reused=True)
        print("如修改了配置，请先停止服务再启动。")
        return
    print("[1/5] 识别本机现有 Codex 和配置……", flush=True)
    config = make_config(root, options)
    check_port(config)
    token = ensure_token(root)
    config_path = root / "Data/config.local.json"
    save_json(config_path, config)
    credentials = root / "Data/private/credentials.local.json"
    from codex_remote_worker.config import load_settings
    from codex_remote_worker.subscription import require_chatgpt_login
    print("Codex CLI：" + config["codex_executable"] + "（" + config["expected_codex_version"] + "）", flush=True)
    print("[2/5] 只读验证当前用户已有的官方登录……", flush=True)
    require_chatgpt_login(load_settings(config_path, credentials), refresh=True)
    worker = root / "bin/CodexRemoteWorker/CodexRemoteWorker.exe"
    print("[3/5] 验证现有沙箱、文件和网络隔离（不调用模型）……", flush=True)
    doctor = run_probe([worker, "doctor", "--config", config_path, "--credentials-file", credentials, "--verify-sandbox"], root, config, "sandbox")
    print("[4/5] 验证 JSON、中文及原始字段保留……", flush=True)
    json_check = run_probe([config["tool_python"], "-X", "utf8", root / "internal/verify-json-runtime.py",
        "--config", config_path, "--credentials-file", credentials], root, config, "json")
    save_json(root / "Data/startup-checks.json", {"passed": True, "model_called": False, "sandbox": doctor, "json": json_check})
    connection_file(root, options, config, token)
    if check_only:
        print("环境检查通过，尚未启动服务。真实订阅权限和额度仍需一条翻译验证。")
        return
    print("[5/5] 启动后台 Worker，等待带鉴权的就绪检查……", flush=True)
    logs = root / "Data/logs"
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    with (logs / "worker.stdout.log").open("ab", buffering=0) as out, (logs / "worker.stderr.log").open("ab", buffering=0) as err:
        process = subprocess.Popen([str(worker), "serve", "--config", str(config_path), "--credentials-file", str(credentials)],
            cwd=root, env=runtime_environment(root, config), stdin=subprocess.DEVNULL, stdout=out, stderr=err,
            creationflags=flags, close_fds=True)
    import win32api
    import win32process
    handle = win32api.OpenProcess(0x1000, False, process.pid)
    try:
        created = win32process.GetProcessTimes(handle)["CreationTime"].timestamp()
    finally:
        handle.Close()
    save_json(root / "Data/worker.process.json", {"pid": process.pid, "created_at": created, "executable": str(worker),
        "root": str(root), "user_sid": user_sid()})
    deadline = time.monotonic() + 90
    ready = {"ready": False}
    while time.monotonic() < deadline and process.poll() is None:
        ready = query_ready(config, token, timeout=10)
        if ready.get("ready"):
            require_service(ready)
            print_ready(root, config)
            return
        time.sleep(.5)
    raise PortableError("SERVICE_NOT_READY", "服务未就绪（" + str(ready.get("code", "START_FAILED")) + "）。请双击 status.cmd 并查看 Data/logs；不会自动重置沙箱。")


def status(root):
    existing = load_running(root)
    if not existing:
        print("本目录的 Worker 没有运行。")
        return
    config = read_json(root / "Data/config.local.json")
    token = read_json(root / "Data/private/credentials.local.json")["CODEX_WORKER_SERVICE_TOKEN"]
    value = query_ready(config, token)
    print(json.dumps({"running": True, "pid": existing["pid"], "ready": value.get("ready", False),
        "code": value.get("code"), "message": value.get("message"), "port": config["port"],
        "service_version": value.get("service_version")}, ensure_ascii=False, indent=2))


def stop(root):
    import win32api
    import win32event
    record = load_running(root)
    if not record:
        print("本目录的 Worker 没有运行，无需停止。")
        return
    maintenance = root / "Data/.maintenance.json"
    operation = uuid.uuid4().hex
    try:
        with maintenance.open("x", encoding="utf-8") as stream:
            json.dump({"operation": "portable-stop", "operation_id": operation}, stream)
    except FileExistsError:
        raise PortableError("MAINTENANCE_ACTIVE", "已有维护操作，拒绝覆盖。请检查 Data/.maintenance.json 和日志。") from None
    try:
        database = root / "Data/jobs.sqlite3"
        if not database.exists():
            raise PortableError("STARTUP_IN_PROGRESS", "任务数据库尚未建立，请稍后重试停止。")
        db = sqlite3.connect(str(database), timeout=10, isolation_level=None)
        try:
            db.execute("BEGIN IMMEDIATE")
            count = db.execute("SELECT COUNT(*) FROM jobs WHERE status IN ('queued','running','checking','cancelling')").fetchone()[0]
            if count:
                raise PortableError("JOBS_ACTIVE", "还有 " + str(count) + " 个未完成任务，拒绝停止。请等待完成或通过原接口取消。")
            db.execute("COMMIT")
        finally:
            db.close()
        handle = owned_process(root, record, terminate=True)
        if handle is not None:
            try:
                win32api.TerminateProcess(handle, 0)
                if win32event.WaitForSingleObject(handle, 10000) != 0:
                    raise PortableError("STOP_TIMEOUT", "尚未确认服务停止，请查看状态。")
            finally:
                handle.Close()
        (root / "Data/worker.process.json").unlink(missing_ok=True)
        print("本目录的 Worker 已停止；未修改 Codex 或其他服务。")
    finally:
        if maintenance.exists() and read_json(maintenance).get("operation_id") == operation:
            maintenance.unlink()


@contextmanager
def controller_lock(root):
    from codex_remote_worker.store import InstanceLock
    try:
        lock = InstanceLock(root / "Data/.controller.lock")
    except (OSError, RuntimeError):
        raise PortableError("CONTROLLER_BUSY", "已有启动/停止/检查正在进行，请等待当前窗口完成。") from None
    try:
        yield
    finally:
        lock.close()


def main():
    parser = argparse.ArgumentParser(description="Codex Worker portable controller")
    parser.add_argument("command", choices=("start", "stop", "status", "check"))
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    try:
        require_user_session()
        protect_directory(root / "Data")
        (root / "Data/logs").mkdir(exist_ok=True)
        with controller_lock(root):
            if args.command in ("start", "check"):
                start(root, check_only=args.command == "check")
            elif args.command == "stop":
                stop(root)
            else:
                status(root)
        return 0
    except Exception as error:
        from codex_remote_worker.common import JobError
        if isinstance(error, (PortableError, JobError)):
            code, message = error.code, error.message
        else:
            code, message = "PORTABLE_START_FAILED", "配置、路径或本地工具检查失败，请核对 worker-settings.json 及 Data/logs。"
        print("\n未完成 [" + code + "]：" + message, flush=True)
        logs = root / "Data/logs"
        if logs.is_dir():
            # Never log repr(error), raw authentication outputs, or environment values.
            with (logs / "controller.log").open("a", encoding="utf-8") as stream:
                stream.write(time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()) + " " + code + "\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
