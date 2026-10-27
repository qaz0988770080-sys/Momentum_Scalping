"""Loopback-only read-only Taishin acceptance UI. No external hosting or credentials on disk."""
from __future__ import annotations
import argparse
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import queue
import secrets
import subprocess
import sys
import threading

PREFIX = "@@MOMENTUM_READONLY@@"

class WorkerBridge:
    def __init__(self, root):
        self.root = Path(root)
        self.lock = threading.RLock()
        self.process = None
        self.messages = queue.Queue()
        self.connected = False
        self.accounts = []

    def _start(self):
        if self.process is not None and self.process.poll() is None:
            return
        self.messages = queue.Queue()
        process = subprocess.Popen(
            [sys.executable, "-u", str(self.root / "taishin_readonly_worker.py"),
             "--sdk-root", str(self.root / "vendor"), "--parent-pid", str(os.getpid())],
            cwd=self.root, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, encoding="utf-8",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.process = process
        messages = self.messages
        def read():
            try:
                for line in process.stdout:
                    if line.startswith(PREFIX):
                        messages.put(json.loads(line[len(PREFIX):]))
            except Exception:
                pass
            messages.put(dict(kind="exited"))
        threading.Thread(target=read, daemon=True).start()
        try:
            result = messages.get(timeout=5)
            if result.get("kind") != "ready":
                raise RuntimeError("READONLY_WORKER_START_FAILED")
        except Exception:
            self._stop()
            raise RuntimeError("READONLY_WORKER_START_FAILED") from None

    def state(self):
        with self.lock:
            self._start()
            return dict(ok=True, worker_ready=True, connected=self.connected,
                        accounts=list(self.accounts), readonly=True, live_enabled=False)

    def request(self, command):
        with self.lock:
            try:
                self._start()
                self.process.stdin.write(json.dumps(command, ensure_ascii=False) + "\n")
                self.process.stdin.flush()
                result = self.messages.get(timeout=48 if command["cmd"] == "login" else 22)
                if result.get("kind") == "exited":
                    raise RuntimeError("READONLY_WORKER_EXITED")
                if result.get("kind") not in {command["cmd"], "error"}:
                    raise RuntimeError("READONLY_PROTOCOL_MISMATCH")
                if command["cmd"] == "login":
                    self.connected = bool(result.get("connected", False))
                    self.accounts = result.get("accounts", []) if self.connected else []
                elif "connected" in result:
                    self.connected = bool(result["connected"])
                if command["cmd"] == "logout":
                    self._stop()
                return result
            except queue.Empty:
                self._stop()
                return dict(kind="error", ok=False, error="READONLY_OPERATION_TIMEOUT",
                            connected=False, readonly=True, live_enabled=False)
            except Exception:
                self._stop()
                return dict(kind="error", ok=False, error="READONLY_WORKER_UNAVAILABLE",
                            connected=False, readonly=True, live_enabled=False)

    def _stop(self):
        process, self.process = self.process, None
        self.connected = False
        self.accounts = []
        if process is not None:
            try:
                if process.poll() is None:
                    try:
                        process.stdin.write('{"cmd":"logout"}\n')
                        process.stdin.flush()
                        process.wait(timeout=1)
                    except Exception:
                        process.kill()
                        process.wait(timeout=2)
            finally:
                for stream in (process.stdin, process.stdout):
                    if stream is not None:
                        stream.close()

    def close(self):
        with self.lock:
            self._stop()

def make_handler(root, bridge):
    root = Path(root)
    session = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(24)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            # Do not record credential requests, account data or browser metadata.
            pass

        def host_ok(self):
            return self.headers.get("Host") == "127.0.0.1:" + str(self.server.server_port)

        def authorized(self):
            try:
                cookies = SimpleCookie(self.headers.get("Cookie", ""))
                cookie = cookies.get("momentum_readonly")
                return cookie is not None and secrets.compare_digest(cookie.value, session)
            except Exception:
                return False

        def respond(self, status, data, *, html=False, set_cookie=False):
            payload = data.encode("utf-8") if html else json.dumps(data, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8" if html else "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'nonce-" +
                nonce + "'; style-src 'nonce-" + nonce +
                "'; connect-src 'self'; img-src 'self' data:; base-uri 'none'; form-action 'none'")
            if set_cookie:
                self.send_header("Set-Cookie", "momentum_readonly=" + session +
                                 "; HttpOnly; SameSite=Strict; Path=/")
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):
            if not self.host_ok():
                return self.respond(403, dict(error="HOST_NOT_ALLOWED"))
            if self.path == "/":
                page = (root / "taishin_readonly.html").read_text(encoding="utf-8")
                page = page.replace("__NONCE__", nonce).replace("__CSRF__", csrf)
                return self.respond(200, page, html=True, set_cookie=True)
            if not self.authorized():
                return self.respond(403, dict(error="LOCAL_SESSION_REQUIRED"))
            if self.path == "/api/state":
                try:
                    return self.respond(200, bridge.state())
                except Exception:
                    return self.respond(503, dict(ok=False, error="READONLY_WORKER_START_FAILED"))
            return self.respond(404, dict(error="NOT_FOUND"))

        def do_POST(self):
            # Drain a bounded body before rejecting a request. Closing a Windows
            # socket with unread bytes can reset it before the 4xx response arrives.
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 8192 or self.headers.get("Transfer-Encoding"):
                    return self.respond(400, dict(error="REQUEST_SIZE_INVALID"))
                self.connection.settimeout(5)
                request_bytes = self.rfile.read(length)
                if len(request_bytes) != length:
                    return self.respond(400, dict(error="REQUEST_BODY_INCOMPLETE"))
            except Exception:
                return self.respond(400, dict(error="REQUEST_BODY_INVALID"))
            origin = "http://127.0.0.1:" + str(self.server.server_port)
            if (not self.host_ok() or not self.authorized()
                    or self.headers.get("Origin") != origin
                    or not secrets.compare_digest(self.headers.get("X-Momentum-CSRF", ""), csrf)):
                return self.respond(403, dict(error="LOCAL_REQUEST_NOT_AUTHORIZED"))
            command = {}
            try:
                if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                    return self.respond(415, dict(error="JSON_REQUIRED"))
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 8192:
                    return self.respond(400, dict(error="REQUEST_SIZE_INVALID"))
                body = json.loads(request_bytes)
                request_bytes = b""
                if not isinstance(body, dict):
                    raise ValueError()
                actions = {"/api/login": "login", "/api/inventory": "inventory",
                           "/api/orders": "orders", "/api/matches": "matches",
                           "/api/logout": "logout"}
                action = actions.get(self.path)
                if action is None:
                    return self.respond(404, dict(error="READ_ONLY_ACTION_NOT_AVAILABLE"))
                command = dict(cmd=action)
                if action == "login":
                    username, password = body.pop("username", ""), body.pop("password", "")
                    environment = body.pop("environment", "formal")
                    if (not isinstance(username, str) or not 0 < len(username) <= 128
                            or not isinstance(password, str) or not 0 < len(password) <= 4096
                            or environment not in {"formal", "validation"}):
                        raise ValueError()
                    command.update(username=username.strip(), password=password, environment=environment)
                    username = password = ""
                elif action in {"inventory", "orders", "matches"}:
                    index = body.pop("account_index", 0)
                    if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index <= 100:
                        raise ValueError()
                    command["account_index"] = index
                body.clear()
                result = bridge.request(command)
                return self.respond(200, result)
            except (ValueError, TypeError, json.JSONDecodeError):
                return self.respond(400, dict(ok=False, error="INVALID_REQUEST"))
            except Exception:
                return self.respond(500, dict(ok=False, error="READONLY_REQUEST_FAILED"))
            finally:
                command.clear()

    return Handler

class LoopbackServer(ThreadingHTTPServer):
    # On Windows, SO_REUSEADDR can allow multiple listeners for one address.
    # Do not let an old process and a new process share this application's port.
    allow_reuse_address = False
    daemon_threads = True

    def server_bind(self):
        import socket
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()

def main():
    from taishin_web_diagnose import appcontainer, http_check
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8788)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    bridge = WorkerBridge(root)
    server = LoopbackServer(("127.0.0.1", args.port), make_handler(root, bridge))
    url = "http://127.0.0.1:" + str(server.server_port)
    state = root / "private" / "readonly-web-runtime.json"
    state.parent.mkdir(parents=True, exist_ok=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        check = http_check(url + "/")
        result = dict(pid=os.getpid(), url=url, readonly=True, live_enabled=False,
                      appcontainer=appcontainer(), http_self_check=check)
        temporary = state.with_suffix(".tmp")
        temporary.write_text(json.dumps(result), encoding="utf-8")
        os.replace(temporary, state)
        print(json.dumps(result), flush=True)
        if not check.get("ok"):
            raise RuntimeError("LOCAL_HTTP_SELF_CHECK_FAILED")
        print("SELF_CHECK_OK - keep this window open", flush=True)
        if not args.self_test:
            thread.join()
    finally:
        server.shutdown()
        bridge.close()
        server.server_close()
        thread.join(timeout=2)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
