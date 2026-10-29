import http.client
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import re
import threading
import unittest

from taishin_readonly_web import make_handler, LoopbackServer, WorkerBridge, EXPECTED_WORKER_BUILD
from unittest.mock import patch

class FakeBridge:
    def __init__(self):
        self.calls = []
    def state(self):
        return dict(ok=True, worker_ready=True, connected=False, readonly=True, live_enabled=False)
    def request(self, command):
        self.calls.append(dict(command))
        return dict(kind=command["cmd"], ok=True, connected=False, readonly=True, live_enabled=False)

class WebTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bridge = FakeBridge()
        cls.server = LoopbackServer(("127.0.0.1", 0),
            make_handler(Path(__file__).parent, cls.bridge))
        cls.server.daemon_threads = True
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.port = cls.server.server_port
        cls.origin = "http://127.0.0.1:" + str(cls.port)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def call(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        try:
            conn.request(method, path, body=body, headers=headers or {})
            result = conn.getresponse()
            return result.status, dict(result.getheaders()), result.read()
        finally:
            conn.close()

    def auth(self):
        status, headers, body = self.call("GET", "/")
        self.assertEqual(status, 200)
        self.cookie = headers["Set-Cookie"].split(";")[0]
        self.csrf = re.search(r'const csrf="([^"]+)"', body.decode()).group(1)
        return body

    def headers(self, **extra):
        result = {"Cookie": self.cookie, "Origin": self.origin,
                  "X-Momentum-CSRF": self.csrf, "Content-Type": "application/json"}
        result.update(extra)
        return result

    def test_health_requires_session_and_returns_safe_build(self):
        self.assertEqual(self.call("GET", "/api/health")[0], 403)
        self.auth()
        status, _, body = self.call("GET", "/api/health", headers={"Cookie": self.cookie})
        data = json.loads(body)
        self.assertEqual(status, 200)
        self.assertFalse(data["live_enabled"])
        self.assertIn("server_build", data)
        self.assertNotIn("accounts", data)

    def test_second_listener_cannot_share_this_port(self):
        with self.assertRaises(OSError):
            LoopbackServer(("127.0.0.1", self.port),
                           make_handler(Path(__file__).parent, FakeBridge()))

    def test_page_is_private_and_does_not_cache_credentials(self):
        status, headers, body = self.call("GET", "/")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertIn("HttpOnly", headers["Set-Cookie"])
        self.assertIn("SameSite=Strict", headers["Set-Cookie"])
        self.assertIn("default-src 'self'", headers["Content-Security-Policy"])
        self.assertNotIn(b"__CSRF__", body)
        self.assertNotIn(b"__NONCE__", body)

    def test_state_requires_local_session(self):
        self.assertEqual(self.call("GET", "/api/state")[0], 403)
        self.auth()
        status, _, body = self.call("GET", "/api/state", headers={"Cookie": self.cookie})
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["worker_ready"])

    def test_rebinding_hostname_rejected(self):
        self.assertEqual(self.call("GET", "/", headers={"Host": "example.com"})[0], 403)

    def test_wrong_origin_missing_token_and_wrong_cookie_are_rejected(self):
        self.auth()
        for changes in [{"Origin": "https://example.com"}, {"X-Momentum-CSRF": ""},
                        {"Cookie": "momentum_readonly=wrong"}, {"Host": "example.com"}]:
            with self.subTest(changes=changes):
                status, _, _ = self.call("POST", "/api/logout", "{}", self.headers(**changes))
                self.assertEqual(status, 403)

    def test_json_and_request_size_are_checked(self):
        self.auth()
        self.assertEqual(self.call("POST", "/api/logout", "x",
            self.headers(**{"Content-Type": "text/plain"}))[0], 415)
        self.assertEqual(self.call("POST", "/api/logout", "",
            self.headers())[0], 400)
        self.assertEqual(self.call("POST", "/api/logout", "not-json",
            self.headers())[0], 400)

    def test_no_order_entry_endpoint(self):
        self.auth()
        before = len(self.bridge.calls)
        self.assertEqual(self.call("POST", "/api/place_order", "{}", self.headers())[0], 404)
        self.assertEqual(len(self.bridge.calls), before)

    def test_invalid_login_or_account_index_is_rejected(self):
        self.auth()
        for body in [dict(username="", password="TEST"), dict(username="TEST", password=""),
                     dict(username="TEST", password="TEST", environment="unexpected")]:
            with self.subTest(body=body):
                self.assertEqual(self.call("POST", "/api/login", json.dumps(body), self.headers())[0], 400)
        self.assertEqual(self.call("POST", "/api/inventory",
            json.dumps(dict(account_index=True)), self.headers())[0], 400)

    def test_login_uses_only_requested_schema_and_clears_response_secrets(self):
        self.auth()
        status, _, body = self.call("POST", "/api/login", json.dumps(dict(
            username="TEST-USER", password="FAKE-SECRET", environment="formal",
            extra="IGNORED")), self.headers())
        self.assertEqual(status, 200)
        self.assertNotIn(b"FAKE-SECRET", body)
        self.assertEqual(set(self.bridge.calls[-1]), {"cmd", "username", "password", "environment"})

class BridgeRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.bridge = WorkerBridge(Path(__file__).parent)
    def tearDown(self):
        self.bridge.close()
    def test_restart_clears_dead_worker_session(self):
        first = self.bridge.state()
        self.assertEqual(first["worker_build"], EXPECTED_WORKER_BUILD)
        self.bridge.connected = True
        self.bridge.accounts = ["FAKE-ACCOUNT"]
        old = self.bridge.process
        old.kill()
        old.wait(timeout=4)
        state = self.bridge.state()
        self.assertFalse(state["connected"])
        self.assertEqual(state["accounts"], [])
        self.assertIsNot(self.bridge.process, old)
        old.stdin.close()
        old.stdout.close()
    def test_wrong_worker_version_fails_closed(self):
        with patch("taishin_readonly_web.EXPECTED_WORKER_BUILD", "wrong-build"):
            with self.assertRaisesRegex(RuntimeError, "READONLY_WORKER_START_FAILED"):
                self.bridge.state()
        self.assertIsNone(self.bridge.process)
    def test_close_then_start_creates_fresh_worker(self):
        self.bridge.state()
        old = self.bridge.process
        self.bridge.close()
        self.assertIsNotNone(old.poll())
        self.assertTrue(self.bridge.state()["worker_ready"])
        self.assertIsNot(old, self.bridge.process)

if __name__ == "__main__":
    unittest.main()
