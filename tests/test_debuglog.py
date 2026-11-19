"""Tests for the server-side debug record that feeds the browser console.

No network: the HTTP wrapper is exercised against a stand-in session method.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient
import app as webapp
import debuglog


class RedactTests(unittest.TestCase):
    def test_secret_looking_query_values_are_masked(self):
        url = "https://api.getsong.co/search/?api_key=SECRET&type=song&lookup=abc&access_token=T0K&code=C"
        shown = debuglog.redact_url(url)
        for secret in ("SECRET", "T0K", "code=C"):
            self.assertNotIn(secret, shown)
        self.assertIn("type=song", shown)
        self.assertIn("lookup=abc", shown)

    def test_user_password_in_the_url_is_dropped(self):
        self.assertEqual(debuglog.redact_url("https://user:pw@example.com/x?a=1"), "https://example.com/x?a=1")

    def test_unparseable_input_does_not_raise(self):
        self.assertIsInstance(debuglog.redact_url("http://[bad"), str)


class RecordTests(unittest.TestCase):
    def setUp(self):
        debuglog.begin_run()

    def test_cursor_returns_only_new_records(self):
        _, cursor, generation = debuglog.since(0)
        debuglog.record("app", "one")
        debuglog.record("app", "two")
        records, cursor2, _ = debuglog.since(cursor, generation)
        self.assertEqual([r["msg"] for r in records], ["one", "two"])
        self.assertEqual(debuglog.since(cursor2, generation)[0], [])

    def test_new_run_resets_the_record_and_old_cursors(self):
        debuglog.record("app", "old")
        _, cursor, old_generation = debuglog.since(0)
        debuglog.begin_run()
        debuglog.record("app", "fresh")
        records, _, generation = debuglog.since(cursor, old_generation)
        self.assertNotEqual(generation, old_generation)
        self.assertEqual([r["msg"] for r in records], ["fresh"])  # from the start, not the stale cursor


class FakeStore:
    def get(self, key):
        return {"lyrics": "x" * 5000}

    def put(self, record):
        raise RuntimeError("db down")


class LoggedStoreTests(unittest.TestCase):
    def setUp(self):
        debuglog.begin_run()

    def test_calls_are_recorded_without_dumping_big_values(self):
        store = debuglog.logged(FakeStore(), "lyrics_db")
        self.assertEqual(len(store.get("a - b")["lyrics"]), 5000)
        record = debuglog.since(0)[0][-1]
        self.assertEqual((record["cat"], record["call"], record["ok"]), ("db", "get", True))
        self.assertNotIn("xxxxx", record["msg"])

    def test_errors_are_recorded_and_still_raised(self):
        store = debuglog.logged(FakeStore(), "lyrics_db")
        with self.assertRaises(RuntimeError):
            store.put({})
        record = debuglog.since(0)[0][-1]
        self.assertFalse(record["ok"])
        self.assertEqual(record["error"], "RuntimeError")

    def test_no_store_stays_none(self):
        self.assertIsNone(debuglog.logged(None, "x"))


class HttpLoggingTests(unittest.TestCase):
    def setUp(self):
        debuglog.begin_run()

    def test_calls_are_recorded_with_secrets_masked_and_error_bodies_kept(self):
        import requests

        class Response:
            status_code, ok, headers, content = 403, False, {"content-type": "application/json"}, b"{}"
            text = '{"error": {"status": 403, "message": "Forbidden"}}'

            def json(self):
                return {}

        original = requests.Session.request
        try:
            requests.Session.request = lambda self, method, url, *a, **k: Response()
            debuglog._http_installed = False
            debuglog.install_http_logging()
            requests.Session().request("POST", "https://api.example.com/v1/x?api_key=SECRET")
        finally:
            requests.Session.request = original
            debuglog._http_installed = False
        record = debuglog.since(0)[0][-1]
        self.assertEqual((record["cat"], record["status"], record["ok"]), ("http", 403, False))
        self.assertNotIn("SECRET", record["url"])
        self.assertIn("Forbidden", record["body"])


class EventsEndpointTests(unittest.TestCase):
    def test_events_response_carries_the_debug_record(self):
        debuglog.begin_run()
        debuglog.record("app", "hello")
        data = TestClient(webapp.app).get("/api/events", params={"cursor": 0}).json()
        self.assertIn("hello", [r["msg"] for r in data["debug"]])
        self.assertIn("debug_cursor", data)
        self.assertIn("generation", data)

    def test_debug_endpoint_pages_by_cursor(self):
        debuglog.begin_run()
        debuglog.record("app", "a")
        client = TestClient(webapp.app)
        first = client.get("/api/debug", params={"cursor": 0}).json()
        debuglog.record("app", "b")
        second = client.get("/api/debug", params={"cursor": first["debug_cursor"],
                                                  "generation": first["generation"]}).json()
        self.assertEqual([r["msg"] for r in second["debug"]], ["b"])


if __name__ == "__main__":
    unittest.main()
