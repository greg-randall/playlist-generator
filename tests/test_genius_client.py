"""build_genius_client must work with lyricsgenius versions with and without `verbose`.

Stand-in module only: nothing is imported from the real library or sent over the network.
"""

import os
import sys
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import generate_playlist as gp


class WithVerbose:
    def __init__(self, access_token=None, remove_section_headers=False, verbose=True,
                 retries=0, timeout=5, sleep_time=0.2):
        self.options = dict(verbose=verbose, retries=retries, timeout=timeout, sleep_time=sleep_time)


class WithoutVerbose:
    def __init__(self, access_token=None, remove_section_headers=False,
                 retries=0, timeout=5, sleep_time=0.2):
        self.options = dict(retries=retries, timeout=timeout, sleep_time=sleep_time)


def fake_module(genius_class):
    module = types.ModuleType("lyricsgenius")
    module.Genius = genius_class
    return module


class BuildGeniusClientTests(unittest.TestCase):
    def build(self, genius_class):
        env = {"GENIUS_TOKEN": "token", "GENIUS_SLEEP": "1.5"}
        with mock.patch.dict(sys.modules, {"lyricsgenius": fake_module(genius_class)}), \
                mock.patch.dict(os.environ, env):
            return gp.build_genius_client()

    def test_older_library_is_told_to_be_quiet(self):
        client = self.build(WithVerbose)
        self.assertIs(client.options["verbose"], False)
        self.assertEqual((client.options["retries"], client.options["sleep_time"]), (1, 1.5))

    def test_newer_library_without_verbose_is_not_passed_it(self):
        client = self.build(WithoutVerbose)  # a TypeError here is the bug this guards against
        self.assertEqual((client.options["retries"], client.options["timeout"]), (1, 10))

    def test_missing_token_is_reported(self):
        with mock.patch.dict(sys.modules, {"lyricsgenius": fake_module(WithoutVerbose)}), \
                mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(SystemExit):
                gp.build_genius_client()


if __name__ == "__main__":
    unittest.main()
