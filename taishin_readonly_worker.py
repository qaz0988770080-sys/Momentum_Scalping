"""Line-delimited JSON worker for the independent read-only acceptance UI."""
import argparse
import json
import os
from pathlib import Path
import sys
import threading
import time

PREFIX = "@@MOMENTUM_READONLY@@"
BUILD = "readonly-20261027-r3"

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--sdk-root", required=True)
    parser.add_argument("--check-sdk", action="store_true")
    parser.add_argument("--parent-pid", type=int)
    args = parser.parse_args()
    args.sdk_root = str(Path(args.sdk_root).resolve())
    root = Path(__file__).resolve().parent
    private = root / "private" / "readonly"
    private.mkdir(parents=True, exist_ok=True)
    # Native SDK output is not a protocol and may contain private broker text.
    # Preserve a private pipe fd for our own sanitized messages, silence fd 1/2.
    protocol_fd = os.dup(sys.stdout.fileno())
    null_fd = os.open(os.devnull, os.O_WRONLY)
    os.dup2(null_fd, 1)
    os.dup2(null_fd, 2)
    os.close(null_fd)
    os.chdir(private)
    from taishin_readonly import SDKDriver, MappingError, observed_now
    driver = SDKDriver(args.sdk_root)

    def emit(kind, **data):
        message = PREFIX + json.dumps(dict(kind=kind, **data), ensure_ascii=False) + "\n"
        os.write(protocol_fd, message.encode("utf-8"))

    def evidence(stage, ok, **values):
        safe = dict(stage=stage, ok=bool(ok), checked_at=observed_now(),
                    readonly=True, live_enabled=False, worker_build=BUILD, **values)
        dest = private / "readiness.json"
        temp = dest.with_suffix(".tmp")
        temp.write_text(json.dumps(safe, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temp, dest)

    if args.parent_pid and os.name == "nt":
        def watch_parent():
            import ctypes
            from ctypes import wintypes
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel.OpenProcess.restype = wintypes.HANDLE
            kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            handle = kernel.OpenProcess(0x00100000, False, args.parent_pid)
            if not handle:
                os._exit(0)
            while kernel.WaitForSingleObject(handle, 1000) == 258:
                pass
            kernel.CloseHandle(handle)
            os._exit(0)
        threading.Thread(target=watch_parent, daemon=True).start()

    if args.check_sdk:
        try:
            driver.load()
            emit("sdk_check", ok=True, sdk_source_sha256=driver.sdk_source_sha256,
                 readonly=True, live_enabled=False)
            return 0
        except Exception as exc:
            emit("sdk_check", ok=False, error=type(exc).__name__,
                 readonly=True, live_enabled=False)
            return 1

    emit("ready", readonly=True, live_enabled=False, worker_build=BUILD)
    evidence("worker_ready", True, connected=False, order_recovery_complete=False)
    try:
        for raw in sys.stdin:
            command = {}
            action = ""
            try:
                if len(raw) > 65536:
                    raise MappingError("COMMAND_TOO_LARGE")
                command = json.loads(raw)
                raw = ""
                if not isinstance(command, dict):
                    raise MappingError("COMMAND_MUST_BE_OBJECT")
                action = command.get("cmd")
                if action not in ("login", "inventory", "orders", "matches", "status", "logout"):
                    action = "invalid"
                    raise MappingError("READ_ONLY_COMMAND_NOT_ALLOWED")
                if action == "login":
                    username = str(command.pop("username", "")).strip()
                    password = str(command.pop("password", ""))
                    try:
                        if not username or not password:
                            raise MappingError("MISSING_CREDENTIALS")
                        result = driver.login(username, password,
                                              str(command.get("environment", "formal")))
                    finally:
                        username = password = ""
                    emit("login", **result)
                    evidence("login", result["ok"], connected=result["connected"],
                             order_recovery_complete=False,
                             sdk_source_sha256=result["sdk_source_sha256"])
                elif action == "inventory":
                    result = driver.inventory(command.get("account_index", 0), timeout=15)
                    # Keep symbols and quantities in the local worker pipe/UI only.
                    emit("inventory", **result)
                    evidence("inventory", True, connected=driver.ready(),
                             inventory_complete=result["complete"],
                             completion_evidence=result["completion_evidence"],
                             order_recovery_complete=False,
                             sdk_source_sha256=driver.sdk_source_sha256)
                elif action in {"orders", "matches"}:
                    result = driver.reports(command.get("account_index", 0),
                                            matches_only=action == "matches")
                    # The UI only needs counts. No account/report details are persisted.
                    result.pop("observed_rows", None)
                    emit(action, **result)
                elif action == "status":
                    emit("status", **driver.status())
                elif action == "logout":
                    emit("logout", **driver.logout())
                    break
                else:
                    raise MappingError("READ_ONLY_COMMAND_NOT_ALLOWED")
            except Exception as exc:
                if action == "login":
                    driver.logout()
                code = str(exc) if isinstance(exc, MappingError) else type(exc).__name__
                # Exception messages from SDKs are never forwarded.
                diagnostics = driver.last_login_diagnostics if action == "login" else {}
                emit("error", action=action, ok=False, error=code, diagnostics=diagnostics,
                     readonly=True, live_enabled=False, connected=driver.ready())
                evidence(action or "command", False, error=code, diagnostics=diagnostics, connected=driver.ready(),
                         order_recovery_complete=False)
            finally:
                if isinstance(command, dict):
                    command.clear()
                raw = ""
    finally:
        driver.logout()
        os.close(protocol_fd)
    return 0

if __name__ == "__main__":
    os._exit(main())
