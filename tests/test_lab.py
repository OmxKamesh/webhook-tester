import contextlib
import io
import json
from pathlib import Path
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
import unittest
import urllib.error
from unittest.mock import patch
from webhook_chaos_lab.cli import main
from webhook_chaos_lab.demo import demo_server
from webhook_chaos_lab.engine import read_url, request, run_suite, validate_url


class SafetyTests(unittest.TestCase):
    def test_local_urls(self):
        for host in ("127.0.0.1", "localhost", "[::1]"):
            self.assertTrue(validate_url(f"http://{host}:1234/events"))

    def test_remote_requires_explicit_opt_in(self):
        with self.assertRaises(ValueError):
            validate_url("https://staging.example/events")
        self.assertTrue(validate_url("https://staging.example/events", True))

    def test_remote_requires_tls(self):
        with self.assertRaises(ValueError):
            validate_url("http://staging.example/events", True)

    def test_bad_urls(self):
        for url in ("file:///tmp/a", "http://a:b@localhost/", "https://localhost/#fragment", "events"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                validate_url(url)

    def test_readback_preserves_query(self):
        self.assertEqual(read_url("http://localhost/state?mode=fixed", "test one"),
                         "http://localhost/state?mode=fixed&entity_id=test+one")
        with self.assertRaises(ValueError):
            read_url("http://localhost/state?entity_id=old", "new")

    def test_actual_abrupt_connection_close(self):
        class CloseHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.connection.close()
            def log_message(self, *args):
                pass
        server = HTTPServer(("127.0.0.1", 0), CloseHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            result = request(f"http://127.0.0.1:{server.server_port}/")
            self.assertIsNone(result["status"])
            self.assertEqual(result["error"], "HTTP or transport failure")
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_transport_errors_do_not_expose_secrets(self):
        with patch("urllib.request.OpenerDirector.open", side_effect=urllib.error.URLError("SECRET in URL")):
            response = request("http://localhost/events")
        self.assertNotIn("SECRET", json.dumps(response))
        self.assertFalse(response["timed_out"])

    def test_invalid_observation_window(self):
        for value in (0, -1, 61, float("nan")):
            with self.assertRaises(ValueError):
                run_suite("http://localhost/", "http://localhost/state", settle_timeout=value)


class IntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.context = demo_server()
        cls.base = cls.context.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.context.__exit__(None, None, None)

    def suite(self, mode):
        return run_suite(self.base + "/" + mode, self.base + "/state?mode=" + mode, settle_timeout=.05)

    def test_fixed_all_scenarios(self):
        report = self.suite("fixed")
        self.assertTrue(report["passed"], report)
        self.assertEqual(len(report["scenarios"]), 4)
        timeout = report["scenarios"][-1]
        self.assertTrue(timeout["checks"]["write_observed_before_retry"])
        self.assertTrue(timeout["checks"]["timeout_observed"])
        self.assertEqual(report["scenarios"][2]["observed"]["effect_count"], 1)

    def test_broken_all_scenarios(self):
        report = self.suite("broken")
        self.assertFalse(report["passed"])
        self.assertTrue(all(not r["passed"] for r in report["scenarios"]))
        self.assertEqual(report["scenarios"][1]["observed"]["state"], "created")
        self.assertEqual(report["scenarios"][2]["observed"]["effect_count"], 16)
        self.assertEqual(report["scenarios"][-1]["observed"]["effect_count"], 2)

    def test_isolation_between_runs(self):
        one, two = self.suite("fixed"), self.suite("fixed")
        self.assertNotEqual(one["run_id"], two["run_id"])
        self.assertTrue(one["passed"] and two["passed"])

    def test_bad_requests(self):
        self.assertEqual(request(self.base + "/fixed", {"test": False})["status"], 400)
        self.assertEqual(request(self.base + "/missing", {"test": True})["status"], 404)

    def test_http_ack_alone_cannot_pass(self):
        with patch("webhook_chaos_lab.engine.request", return_value={"status": 200, "body": {"accepted": True}, "timed_out": False}):
            report = run_suite(self.base + "/fixed", self.base + "/state?mode=fixed", settle_timeout=.001)
        self.assertFalse(report["passed"])
        self.assertTrue(all(not r["checks"]["readback_matches"] for r in report["scenarios"]))

    def test_response_secrets_are_not_in_report(self):
        malicious = {"status": 200, "timed_out": False, "body": {
            "token": "SECRET-TOKEN", "state": "SECRET-STATE",
            "applied_event_ids": ["SECRET-ID"], "effect_count": "SECRET-COUNT"}}
        with patch("webhook_chaos_lab.engine.request", return_value=malicious):
            report = run_suite(self.base + "/fixed", self.base + "/state?mode=fixed", settle_timeout=.001)
        self.assertNotIn("SECRET", json.dumps(report))
        self.assertFalse(report["passed"])

    def test_network_failure_reported_without_crash(self):
        with patch("webhook_chaos_lab.engine.request", return_value={"status": None, "error": "offline", "timed_out": False}):
            report = run_suite(self.base + "/fixed", self.base + "/state?mode=fixed", settle_timeout=.001)
        self.assertFalse(report["passed"])

    def test_cli_report_and_exit_codes(self):
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(io.StringIO()):
            output = Path(directory) / "report.json"
            self.assertEqual(main(["run", "--target", self.base + "/fixed", "--readback", self.base + "/state?mode=fixed", "--settle-timeout", ".05", "--json", str(output)]), 0)
            self.assertTrue(json.loads(output.read_text())["passed"])
            self.assertEqual(main(["run", "--target", self.base + "/broken", "--readback", self.base + "/state?mode=broken", "--settle-timeout", ".05"]), 1)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(["run", "--target", "https://example.org/events", "--readback", "https://example.org/state"]), 2)


if __name__ == "__main__":
    unittest.main()
