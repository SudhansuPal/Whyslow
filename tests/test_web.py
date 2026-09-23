import json
import os
import socket
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from dataclasses import replace
from pathlib import Path

from whyslow import storage
from whyslow.config import Config, DashboardConfig
from whyslow.sampler import ProcSample, Sample
from whyslow.web import auth
from whyslow.web.app import DashboardServer

from helpers import proc, system


class TestLoginCodes(unittest.TestCase):
    def setUp(self):
        self.token = auth.new_token()
        self.verifier = auth.CodeVerifier(self.token)

    def test_code_works_exactly_once(self):
        code = auth.mint_code(self.token)
        self.assertTrue(self.verifier.redeem(code))
        self.assertFalse(self.verifier.redeem(code))

    def test_expired_code_rejected(self):
        code = auth.mint_code(self.token, now=time.time() - auth.CODE_TTL_S - 1)
        self.assertFalse(self.verifier.redeem(code))

    def test_far_future_code_rejected(self):
        code = auth.mint_code(self.token, now=time.time() + 3600)
        self.assertFalse(self.verifier.redeem(code))

    def test_tampered_or_foreign_codes_rejected(self):
        code = auth.mint_code(self.token)
        expires, nonce, mac = code.split(".")
        self.assertFalse(self.verifier.redeem(f"{int(expires) + 60}.{nonce}.{mac}"))
        self.assertFalse(self.verifier.redeem(auth.mint_code(auth.new_token())))
        for junk in ("", "a.b", "x.y.z", "1.2.3.4", "9" * 300):
            self.assertFalse(self.verifier.redeem(junk))

    def test_token_file_is_private(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "dashboard.token"
            auth.write_token(path, self.token)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(auth.read_token(path), self.token)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class TestDashboardServer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        old = os.umask(0o077)
        db = Path(cls.tmp.name) / "w.sqlite3"
        cfg = replace(Config(), dashboard=DashboardConfig(port=free_port()))
        store = storage.Store(db, cfg)
        evil = proc(66, "<script>alert(1)</script>")
        evil.cmdline = '"><img src=x onerror=alert(1)>'
        now = time.time()
        for i in range(5):
            store.record(Sample(system(now - 5 + i), [ProcSample(evil, 1.0, 100.0, 1, 1)], [], 0.0, True))
        store.close()
        os.umask(old)
        cls.token = auth.new_token()
        cls.server = DashboardServer(cfg, cls.token, db)
        assert cls.server.start(), "dashboard did not start"
        cls.base = f"http://127.0.0.1:{cfg.dashboard.port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()
        cls.tmp.cleanup()

    def request(self, path, token=None, method="GET", body=None, host=None):
        req = urllib.request.Request(self.base + path, method=method, data=body)
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        if body is not None:
            req.add_header("Content-Type", "application/json")
        if host:
            req.add_header("Host", host)
        try:
            with urllib.request.urlopen(req, timeout=5) as res:
                return res.status, dict(res.headers), res.read()
        except urllib.error.HTTPError as err:
            return err.code, dict(err.headers), err.read()

    def test_bound_to_loopback_only(self):
        self.assertEqual(set(self.server.bound_addresses()), {"127.0.0.1"})

    def test_page_served_with_strict_headers(self):
        status, headers, body = self.request("/")
        self.assertEqual(status, 200)
        csp = headers["content-security-policy"]
        self.assertIn("default-src 'none'", csp)
        self.assertIn("script-src 'self'", csp)
        self.assertNotIn("unsafe-inline", csp)
        self.assertNotIn("unsafe-eval", csp)
        self.assertEqual(headers["x-frame-options"], "DENY")
        self.assertEqual(headers["x-content-type-options"], "nosniff")
        self.assertNotIn("server", {k.lower() for k in headers})
        self.assertNotIn(b"<script>", body.split(b"</head>")[1])  # no inline scripts in the body

    def test_api_requires_token(self):
        for path in ("/api/meta", "/api/series", "/api/spikes", "/api/now"):
            with self.subTest(path=path):
                self.assertEqual(self.request(path)[0], 401)
                self.assertEqual(self.request(path, token="wrong")[0], 401)
                self.assertEqual(self.request(path, token=self.token)[0], 200)

    def test_foreign_host_header_rejected(self):
        # DNS rebinding: attacker.example resolving to 127.0.0.1
        status, _, _ = self.request("/api/meta", token=self.token, host="attacker.example:8765")
        self.assertEqual(status, 400)
        self.assertEqual(self.request("/", host=f"localhost:{self.server.port}")[0], 200)

    def test_login_code_exchange(self):
        code = auth.mint_code(self.token)
        body = json.dumps({"code": code}).encode()
        status, _, payload = self.request("/api/session", method="POST", body=body)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(payload)["token"], self.token)
        self.assertEqual(self.request("/api/session", method="POST", body=body)[0], 401)  # single use

    def test_read_only_and_no_docs(self):
        self.assertEqual(self.request("/api/meta", token=self.token, method="DELETE")[0], 405)
        self.assertEqual(self.request("/api/now", token=self.token, method="POST", body=b"{}")[0], 405)
        for path in ("/docs", "/redoc", "/openapi.json"):
            self.assertEqual(self.request(path)[0], 404)

    def test_no_path_traversal(self):
        for path in ("/static/../app.py", "/static/%2e%2e/app.py", "/static/..%2fauth.py"):
            with self.subTest(path=path):
                self.assertEqual(self.request(path)[0], 404)

    def test_query_parameters_validated(self):
        self.assertEqual(self.request("/api/series?seconds=1", token=self.token)[0], 422)
        self.assertEqual(self.request("/api/series?seconds=abc", token=self.token)[0], 422)
        self.assertEqual(self.request("/api/spikes?limit=100000", token=self.token)[0], 422)

    def test_hostile_process_names_come_back_as_inert_json(self):
        status, headers, body = self.request("/api/now", token=self.token)
        self.assertEqual(status, 200)
        self.assertTrue(headers["content-type"].startswith("application/json"))
        proc_row = json.loads(body)["latest"]["processes"][0]
        self.assertEqual(proc_row["app"], "<script>alert(1)</script>")  # data, rendered via textContent

    def test_leaderboard_and_battery_endpoints(self):
        status, _, body = self.request("/api/leaderboard?seconds=86400", token=self.token)
        self.assertEqual(status, 200)
        apps = json.loads(body)["apps"]
        self.assertEqual(apps[0]["app"], "<script>alert(1)</script>")  # inert JSON, rendered via textContent
        self.assertIn("cpu_seconds_on_battery", apps[0])
        self.assertIn("spikes_caused", apps[0])

        status, _, body = self.request("/api/battery?seconds=86400", token=self.token)
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertTrue(data["available"])
        self.assertFalse(data["model"]["usable"])          # only seconds of synthetic data
        self.assertTrue(all(a["percent"] is None for a in data["apps"]))

    def test_new_endpoints_need_the_token(self):
        for path in ("/api/leaderboard", "/api/battery"):
            with self.subTest(path=path):
                self.assertEqual(self.request(path)[0], 401)
                self.assertEqual(self.request(path + "?seconds=1", token=self.token)[0], 422)

    def test_series_shape(self):
        status, _, body = self.request("/api/series?seconds=300", token=self.token)
        data = json.loads(body)
        self.assertEqual(status, 200)
        self.assertFalse(data["aggregated"])
        self.assertEqual(len(data["ts"]), 5)
        self.assertEqual(len(data["cpu"]), 5)
        status, _, body = self.request("/api/series?seconds=86400", token=self.token)
        self.assertTrue(json.loads(body)["aggregated"])


if __name__ == "__main__":
    unittest.main()
