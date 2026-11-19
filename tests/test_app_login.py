"""Tests for the Spotify login that ends on the app's own /callback route.

No network and no spotipy login: the callback route and the waiting helper are driven
directly with a stand-in for the run thread.
"""

import os
import sys
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient
import app as webapp


def start_waiting():
    webapp._login.update(done=threading.Event(), waiting=True, code=None, error=None)


class CallbackRouteTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(webapp.app)

    def test_code_is_handed_to_the_waiting_run(self):
        start_waiting()
        response = self.client.get("/callback", params={"code": "abc123"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("Logged in to Spotify", response.text)
        self.assertTrue(webapp._login["done"].is_set())
        self.assertEqual(webapp._wait_for_login_code(), "abc123")

    def test_denied_login_is_reported_and_raised(self):
        start_waiting()
        response = self.client.get("/callback", params={"error": "access_denied"})
        self.assertIn("Spotify login failed", response.text)
        with self.assertRaises(RuntimeError) as caught:
            webapp._wait_for_login_code()
        self.assertIn("access_denied", str(caught.exception))

    def test_visit_with_no_login_waiting_changes_nothing(self):
        webapp._login.update(done=threading.Event(), waiting=False, code=None, error=None)
        response = self.client.get("/callback", params={"code": "stray"})
        self.assertIn("Nothing was waiting", response.text)
        self.assertFalse(webapp._login["done"].is_set())

    def test_waiting_gives_up_after_the_timeout(self):
        start_waiting()
        with mock.patch.object(webapp, "LOGIN_TIMEOUT_SECONDS", 0):
            with self.assertRaises(TimeoutError):
                webapp._wait_for_login_code()
        self.assertFalse(webapp._login["waiting"])


class RedirectPortTests(unittest.TestCase):
    def port(self, uri):
        with mock.patch.dict(os.environ, {"SPOTIPY_REDIRECT_URI": uri}):
            return webapp._redirect_port()

    def test_loopback_callback_gives_its_port(self):
        self.assertEqual(self.port("http://127.0.0.1:8888/callback"), 8888)
        self.assertEqual(self.port("http://localhost:9000/callback"), 9000)

    def test_other_uris_give_none(self):
        self.assertIsNone(self.port(""))
        self.assertIsNone(self.port("https://example.com/callback"))
        self.assertIsNone(self.port("http://127.0.0.1:8888/other"))
        self.assertIsNone(self.port("http://127.0.0.1/callback"))


class ListenerTests(unittest.TestCase):
    def test_no_listener_when_redirect_is_not_loopback(self):
        with mock.patch.dict(os.environ, {"SPOTIPY_REDIRECT_URI": "https://example.com/callback"}):
            self.assertFalse(webapp._start_login_listener())

    def test_app_port_needs_no_second_listener(self):
        env = {"SPOTIPY_REDIRECT_URI": "http://127.0.0.1:8000/callback", "APP_PORT": "8000"}
        with mock.patch.dict(os.environ, env):
            self.assertTrue(webapp._start_login_listener())


if __name__ == "__main__":
    unittest.main()
