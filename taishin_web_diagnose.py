"""Loopback diagnostics only. Does not import the broker SDK or accept credentials."""
import ctypes
from ctypes import wintypes
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import socket
import threading
import urllib.error
import urllib.request

def appcontainer():
    if os.name != "nt":
        return None
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    advapi.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    advapi.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                         wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    token = wintypes.HANDLE()
    if not advapi.OpenProcessToken(kernel.GetCurrentProcess(), 8, ctypes.byref(token)):
        return None
    try:
        value, size = wintypes.DWORD(), wintypes.DWORD()
        if advapi.GetTokenInformation(token, 29, ctypes.byref(value), ctypes.sizeof(value),
                                     ctypes.byref(size)):
            return bool(value.value)
        return None
    finally:
        kernel.CloseHandle(token)

def process_running(pid):
    if os.name != "nt":
        return None
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        return dict(running=None, winerror=ctypes.get_last_error())
    try:
        code = wintypes.DWORD()
        ok = kernel.GetExitCodeProcess(handle, ctypes.byref(code))
        return dict(running=code.value == 259 if ok else None)
    finally:
        kernel.CloseHandle(handle)

def http_check(url):
    # Proxy settings must not route a request to this loopback service elsewhere.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(url, timeout=3) as response:
            return dict(ok=response.status == 200, http_status=response.status)
    except Exception as exc:
        cause = getattr(exc, "reason", exc)
        return dict(ok=False, error=type(cause).__name__, errno=getattr(cause, "errno", None),
                    winerror=getattr(cause, "winerror", None))

def main():
    base = Path(__file__).resolve().parent
    result = dict(checked_at=datetime.now(timezone.utc).isoformat(),
                  appcontainer=appcontainer(), service_http=http_check("http://127.0.0.1:8786/"))
    saved = base / "private" / "readonly-web-runtime.json"
    if saved.is_file():
        try:
            state = json.loads(saved.read_text(encoding="utf-8"))
            result["saved_process"] = process_running(int(state["pid"]))
        except Exception:
            result["saved_process"] = dict(error="STATE_NOT_READABLE")
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"OK")
    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        result["same_process_loopback"] = http_check("http://127.0.0.1:" + str(server.server_port) + "/")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    folder = base / "private"
    folder.mkdir(exist_ok=True)
    (folder / "web-diagnostics.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2), flush=True)
    return 0 if result["service_http"]["ok"] else 1

if __name__ == "__main__":
    raise SystemExit(main())
