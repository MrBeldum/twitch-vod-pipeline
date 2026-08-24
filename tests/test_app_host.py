"""Chromium-only window host and Windows app identity."""

from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest.mock import patch

from vodpipe.app import browser_candidates, find_app_browser
from vodpipe.cli import SUBCOMMANDS, build_parser
from vodpipe.winapp import AUMID, APP_NAME, HOST_NAME, find_csc, host_path


class BrowserSelectionTests(unittest.TestCase):
    def test_candidates_never_include_edge(self):
        joined = "\n".join(browser_candidates()).lower()
        self.assertNotIn("msedge", joined)
        self.assertNotIn("\\edge\\", joined)

    def test_chromium_is_tried_before_chrome(self):
        names = [os.path.normcase(path) for path in browser_candidates()]
        chromium = next(i for i, name in enumerate(names) if "chromium" in name)
        chrome = next(i for i, name in enumerate(names)
                      if "google" in name and "chrome" in name)
        self.assertLess(chromium, chrome)

    def test_find_app_browser_ignores_edge_on_path(self):
        def which(name):
            if name in ("chromium", "chrome", "chrome.exe", "chromium.exe"):
                return None
            if name == "msedge":
                return r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"
            return None

        with patch("vodpipe.app.Path") as path_cls, \
                patch("vodpipe.app.shutil.which", side_effect=which):
            path_cls.return_value.is_file.return_value = False
            self.assertIsNone(find_app_browser())


class WindowsAppIdentityTests(unittest.TestCase):
    def test_aumid_is_stable(self):
        self.assertEqual(AUMID, "MrBeldum.VODPipeline")
        self.assertEqual(APP_NAME, "VOD Pipeline")
        self.assertEqual(HOST_NAME, "VODPipeline.exe")
        self.assertTrue(str(host_path()).endswith(HOST_NAME))

    def test_install_and_uninstall_are_cli_commands(self):
        self.assertIn("install", SUBCOMMANDS)
        self.assertIn("uninstall", SUBCOMMANDS)
        parser = build_parser()
        actions = [action for action in parser._subparsers._group_actions
                   if hasattr(action, "choices")]
        self.assertIn("install", actions[0].choices)
        self.assertIn("uninstall", actions[0].choices)

    def test_csc_hint_points_at_framework(self):
        csc = find_csc()
        if csc is None:
            self.skipTest("csc.exe is not installed on this machine")
        self.assertTrue(Path(csc).is_file())
        self.assertTrue(os.path.normcase(csc).endswith("csc.exe"))
