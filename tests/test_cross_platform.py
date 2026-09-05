"""macOS/Linux support and the 2026-09-05 audit fixes.

The Python core was always portable; these are the seams that were not:
reveal-in-file-manager, the app window, tool discovery, the proxy encoder,
where a pip install keeps its config -- plus two defects the same audit
found (a half-open chat socket, and the dashboard poll re-reading every
transcript).
"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from vodpipe import chat, config, server, util
from vodpipe.app import find_app_browser


class RevealCommandTests(unittest.TestCase):
    def test_each_platform_gets_its_file_manager(self):
        tmp = Path(tempfile.mkdtemp(prefix="vodpipe-reveal-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        file = tmp / "a.mp4"
        file.write_bytes(b"x")

        with patch.object(server.sys, "platform", "win32"):
            self.assertEqual(server._reveal_command(tmp), ["explorer", str(tmp)])
            self.assertEqual(server._reveal_command(file),
                             ["explorer", "/select,", str(file)])
        with patch.object(server.sys, "platform", "darwin"):
            self.assertEqual(server._reveal_command(tmp), ["open", str(tmp)])
            self.assertEqual(server._reveal_command(file), ["open", "-R", str(file)])
        with patch.object(server.sys, "platform", "linux"):
            self.assertEqual(server._reveal_command(tmp), ["xdg-open", str(tmp)])
            # xdg-open cannot select a file, so it opens the folder it is in.
            self.assertEqual(server._reveal_command(file), ["xdg-open", str(tmp)])


class AppWindowTests(unittest.TestCase):
    def test_macos_uses_the_system_browser(self):
        # Chrome outlives its last window on macOS, so "window closed" cannot
        # be the shutdown signal there. See app.py's docstring.
        with patch("vodpipe.app.sys.platform", "darwin"), \
                patch("vodpipe.app.shutil.which", return_value="/usr/bin/chromium"):
            self.assertIsNone(find_app_browser())

    def test_linux_finds_distro_named_chromium_on_path(self):
        def which(name):
            return "/usr/bin/chromium-browser" if name == "chromium-browser" else None

        with patch("vodpipe.app.sys.platform", "linux"), \
                patch("vodpipe.app.Path") as path_cls, \
                patch("vodpipe.app.shutil.which", side_effect=which):
            path_cls.return_value.is_file.return_value = False
            self.assertEqual(find_app_browser(), "/usr/bin/chromium-browser")


class ToolHintTests(unittest.TestCase):
    def test_posix_hints_cover_homebrew_and_the_cli_installers(self):
        for name, hints in util._POSIX_HINTS.items():
            self.assertIn(f"/opt/homebrew/bin/{name}", hints)
            self.assertIn(f"/usr/local/bin/{name}", hints)
        self.assertEqual(util._POSIX_HINTS["grok"][0], "~/.grok/bin/grok")
        self.assertIn("~/.local/bin/claude", util._POSIX_HINTS["claude"])

    def test_find_tool_expands_home_in_hints(self):
        tmp = Path(tempfile.mkdtemp(prefix="vodpipe-hint-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        binary = tmp / ".grok" / "bin" / "grok"
        binary.parent.mkdir(parents=True)
        binary.write_bytes(b"")
        with patch.dict(util._HINTS, {"grok": ["~/.grok/bin/grok"]}), \
                patch.dict(os.environ, {"HOME": str(tmp), "USERPROFILE": str(tmp)}), \
                patch("vodpipe.util.shutil.which", return_value=None):
            self.assertEqual(util.find_tool("grok"), str(binary))


class DataRootTests(unittest.TestCase):
    def test_checkout_keeps_config_beside_the_code(self):
        # This test runs from a checkout, so pyproject.toml is there.
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("VODPIPE_HOME", None)
            self.assertEqual(config._data_root(), config.APP_ROOT)

    def test_override_wins(self):
        with patch.dict(os.environ, {"VODPIPE_HOME": "~/elsewhere"}):
            self.assertEqual(config._data_root(), Path("~/elsewhere").expanduser())

    def test_pip_install_uses_the_platform_user_directory(self):
        tmp = Path(tempfile.mkdtemp(prefix="vodpipe-site-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        home = tmp / "home"
        home.mkdir()
        env = {"HOME": str(home), "USERPROFILE": str(home),
               "LOCALAPPDATA": str(home / "AppData" / "Local")}
        with patch.object(config, "APP_ROOT", tmp / "site-packages"), \
                patch.dict(os.environ, env):
            os.environ.pop("VODPIPE_HOME", None)
            os.environ.pop("XDG_DATA_HOME", None)
            with patch.object(config.sys, "platform", "win32"):
                self.assertEqual(config._data_root(),
                                 home / "AppData" / "Local" / "vodpipe")
            with patch.object(config.sys, "platform", "darwin"):
                self.assertEqual(config._data_root(),
                                 home / "Library" / "Application Support" / "vodpipe")
            with patch.object(config.sys, "platform", "linux"):
                self.assertEqual(config._data_root(),
                                 home / ".local" / "share" / "vodpipe")


class _SilentSocket:
    """A TLS socket that never delivers anything: a half-open connection."""

    def __init__(self):
        self.sent: list[bytes] = []

    def settimeout(self, _seconds):
        pass

    def sendall(self, data):
        self.sent.append(data)

    def recv(self, _size):
        raise TimeoutError

    def close(self):
        pass


class ChatIdleTests(unittest.TestCase):
    def test_silent_socket_is_pinged_then_abandoned(self):
        tmp = Path(tempfile.mkdtemp(prefix="vodpipe-chat-idle-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        sock = _SilentSocket()
        clock = iter(range(0, 100_000, 50))  # 50 s per monotonic() call

        capture = chat.LiveChatCapture("examplechannel", tmp / "chat.jsonl",
                                       origin=0.0)
        with patch("vodpipe.chat.open_tcp", return_value=object()), \
                patch("vodpipe.chat.wrap_tls", return_value=sock), \
                patch("vodpipe.chat.time.monotonic", side_effect=clock):
            with self.assertRaisesRegex(chat.ChatError, "silent"):
                capture._session()

        pings = [line for line in sock.sent if line.startswith(b"PING")]
        self.assertEqual(pings, [b"PING :vodpipe\r\n"])


if __name__ == "__main__":
    unittest.main()
