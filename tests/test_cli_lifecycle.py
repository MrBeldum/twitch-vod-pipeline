"""CLI ownership barriers around startup and nested cleanup failures."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from vodpipe.cli import cmd_dashboard, cmd_record
from vodpipe.config import DEFAULTS, Config, deep_merge


def make_config(root: Path) -> Config:
    return Config(deep_merge(DEFAULTS, {
        "paths": {
            "masters_root": str(root / "masters"),
            "work_root": str(root / "work"),
            "censor_master_list": str(root / "none.txt"),
        },
        "recording": {"free_space_floor_gb": 0, "hard_reserve_gb": 0},
        "watcher": {"enabled": False},
    }))


class CliLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="vodpipe-cli-life-")
        self.addCleanup(self.temp.cleanup)
        self.config = make_config(Path(self.temp.name))
        self.dashboard_args = SimpleNamespace(port=None, no_browser=True)
        self.record_args = SimpleNamespace(
            channel="chan", now=True, minutes=None)

    def test_dashboard_failed_start_still_closes_owned_pipeline_once(self):
        pipeline = Mock()
        pipeline.start.side_effect = RuntimeError("start failed")
        with patch("vodpipe.cli.Pipeline", return_value=pipeline):
            with self.assertRaisesRegex(RuntimeError, "start failed"):
                cmd_dashboard(self.config, self.dashboard_args)
        pipeline.shutdown_until_stopped.assert_called_once_with()

    def test_dashboard_failed_bind_still_closes_owned_pipeline_once(self):
        pipeline = Mock()
        with patch("vodpipe.cli.Pipeline", return_value=pipeline), \
                patch("vodpipe.cli.serve", side_effect=OSError("bind failed")):
            with self.assertRaisesRegex(OSError, "bind failed"):
                cmd_dashboard(self.config, self.dashboard_args)
        pipeline.shutdown_until_stopped.assert_called_once_with()

    def test_server_close_failure_cannot_skip_pipeline_shutdown(self):
        pipeline = Mock()
        httpd = Mock()
        httpd.server_close.side_effect = OSError("close failed")
        with patch("vodpipe.cli.Pipeline", return_value=pipeline), \
                patch("vodpipe.cli.serve", return_value=httpd):
            with self.assertRaisesRegex(OSError, "close failed"):
                cmd_dashboard(self.config, self.dashboard_args)
        pipeline.shutdown_until_stopped.assert_called_once_with()

    def test_dashboard_log_failure_cannot_skip_pipeline_shutdown(self):
        pipeline = Mock()
        httpd = Mock()
        with patch("vodpipe.cli.Pipeline", return_value=pipeline), \
                patch("vodpipe.cli.serve", return_value=httpd), \
                patch("vodpipe.cli.LOG.info", side_effect=OSError("log failed")):
            with self.assertRaisesRegex(OSError, "log failed"):
                cmd_dashboard(self.config, self.dashboard_args)
        pipeline.shutdown_until_stopped.assert_called_once_with()

    def test_record_failed_start_still_closes_owned_pipeline_once(self):
        pipeline = Mock()
        pipeline.start.side_effect = RuntimeError("start failed")
        with patch("vodpipe.cli.Pipeline", return_value=pipeline):
            with self.assertRaisesRegex(RuntimeError, "start failed"):
                cmd_record(self.config, self.record_args)
        pipeline.shutdown_until_stopped.assert_called_once_with()

    def test_record_print_failure_cannot_skip_pipeline_shutdown(self):
        pipeline = Mock()
        session = SimpleNamespace(
            directory="somewhere", status="complete", chunks=[])
        pipeline.start_recording.return_value = session
        with patch("vodpipe.cli.Pipeline", return_value=pipeline), \
                patch("builtins.print", side_effect=OSError("print failed")):
            with self.assertRaisesRegex(OSError, "print failed"):
                cmd_record(self.config, self.record_args)
        pipeline.shutdown_until_stopped.assert_called_once_with()

    def test_record_returns_nonzero_when_the_session_failed(self):
        # P10: a FAILED terminal state is a nonzero exit even with no per-chunk
        # error, so a script driving `vodpipe record` can tell success from failure.
        pipeline = Mock()
        session = SimpleNamespace(directory="somewhere", status="failed",
                                  error="no video arrived", chunks=[])
        pipeline.start_recording.return_value = session
        with patch("vodpipe.cli.Pipeline", return_value=pipeline), \
                patch("builtins.print"):
            self.assertEqual(cmd_record(self.config, self.record_args), 1)

    def test_record_returns_zero_on_a_clean_session(self):
        pipeline = Mock()
        session = SimpleNamespace(directory="somewhere", status="complete",
                                  chunks=[])
        pipeline.start_recording.return_value = session
        with patch("vodpipe.cli.Pipeline", return_value=pipeline), \
                patch("builtins.print"):
            self.assertEqual(cmd_record(self.config, self.record_args), 0)

    def test_vod_returns_nonzero_when_the_download_failed(self):
        from vodpipe.cli import cmd_vod
        pipeline = Mock()
        session = SimpleNamespace(session_id="s", channel="c",
                                  directory="somewhere", status="failed",
                                  error="cannot open this VOD", chunks=[])
        pipeline.download_vod.return_value = session
        args = SimpleNamespace(
            url="https://www.twitch.tv/videos/1", start=None, duration=None)
        with patch("vodpipe.cli.Pipeline", return_value=pipeline), \
                patch("builtins.print"):
            self.assertEqual(cmd_vod(self.config, args), 1)

    def test_vod_rejects_a_non_vod_url_before_starting(self):
        from vodpipe.cli import cmd_vod
        args = SimpleNamespace(
            url="https://twitch.tv/somechannel", start=None, duration=None)
        with patch("vodpipe.cli.Pipeline") as pipeline_cls, \
                patch("builtins.print"):
            self.assertEqual(cmd_vod(self.config, args), 2)
        pipeline_cls.assert_not_called()

    def test_second_interrupt_from_final_drain_is_not_returned_as_success(self):
        pipeline = Mock()
        httpd = Mock()
        httpd.serve_forever.side_effect = KeyboardInterrupt
        pipeline.shutdown_until_stopped.side_effect = KeyboardInterrupt
        with patch("vodpipe.cli.Pipeline", return_value=pipeline), \
                patch("vodpipe.cli.serve", return_value=httpd), \
                patch("builtins.print"):
            with self.assertRaises(KeyboardInterrupt):
                cmd_dashboard(self.config, self.dashboard_args)
        pipeline.shutdown_until_stopped.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
