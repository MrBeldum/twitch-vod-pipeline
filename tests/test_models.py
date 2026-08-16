"""Absolute timeout contracts for shared model transports."""

from __future__ import annotations

import json
import subprocess
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import vodpipe.models as models
from vodpipe.models import AnthropicApiModel, ClaudeCliModel, ModelError


class FakeResponse:
    def __init__(self, *chunks: bytes, sock=None) -> None:
        self.chunks = list(chunks)
        self.fp = SimpleNamespace(raw=SimpleNamespace(_sock=sock))

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read1(self, size: int) -> bytes:
        if not self.chunks:
            return b""
        return self.chunks.pop(0)


class AnthropicDeadlineTests(unittest.TestCase):
    def test_slow_drip_read_uses_remaining_absolute_deadline(self):
        class Clock:
            now = 0.0

            def monotonic(self):
                return self.now

        class Socket:
            def __init__(self) -> None:
                self.timeouts = []

            def settimeout(self, timeout):
                self.timeouts.append(timeout)

        class SlowResponse(FakeResponse):
            def __init__(self, clock, sock) -> None:
                super().__init__(sock=sock)
                self.clock = clock

            def read1(self, size: int) -> bytes:
                self.clock.now += 0.6
                return b"{"

        clock = Clock()
        sock = Socket()
        response = SlowResponse(clock, sock)
        with patch.object(models.time, "monotonic",
                          side_effect=clock.monotonic), \
                patch.object(models.urllib.request, "urlopen",
                             return_value=response) as urlopen:
            model = AnthropicApiModel("key", timeout=1.0, max_retries=4)
            with self.assertRaisesRegex(ModelError, "deadline"):
                model.ask("system", "user")

        self.assertEqual(urlopen.call_count, 1)
        self.assertAlmostEqual(urlopen.call_args.kwargs["timeout"], 1.0)
        self.assertEqual(len(sock.timeouts), 2)
        self.assertAlmostEqual(sock.timeouts[0], 1.0)
        self.assertAlmostEqual(sock.timeouts[1], 0.4)

    def test_truncated_json_is_retried_cleanly(self):
        payload = json.dumps({
            "stop_reason": "end_turn",
            "content": [{"type": "text", "text": "complete"}],
        }).encode("utf-8")
        responses = [FakeResponse(b'{"content":'), FakeResponse(payload)]

        with patch.object(models.urllib.request, "urlopen",
                          side_effect=responses) as urlopen, \
                patch.object(models.time, "sleep") as sleep:
            model = AnthropicApiModel("key", timeout=10.0, max_retries=2)
            self.assertEqual(model.ask("system", "user"), "complete")

        self.assertEqual(urlopen.call_count, 2)
        sleep.assert_called_once_with(2.0)


class ClaudeDeadlineTests(unittest.TestCase):
    def test_cli_runs_with_no_tools_or_project_customizations(self):
        class Process:
            returncode = 0

            def communicate(self, data=None, *, timeout=None):
                return b"answer", b""

        with patch.object(models, "popen", return_value=Process()) as spawn:
            result = ClaudeCliModel("claude", timeout=10).ask(
                "system boundary", "untrusted transcript")
        self.assertEqual(result, "answer")
        command = spawn.call_args.args[0]
        self.assertIn("--safe-mode", command)
        self.assertEqual(command[command.index("--tools") + 1], "")
        self.assertEqual(command[command.index("--system-prompt") + 1],
                         "system boundary")
        self.assertIn("--no-session-persistence", command)

    def test_deadline_is_not_extended_for_process_cleanup(self):
        class Clock:
            now = 0.0

            def monotonic(self):
                return self.now

        class Process:
            def __init__(self, clock) -> None:
                self.clock = clock
                self.timeouts = []
                self.killed = False

            def communicate(self, data=None, *, timeout=None):
                self.timeouts.append(timeout)
                self.clock.now = 5.0
                raise subprocess.TimeoutExpired("claude", timeout)

            def kill(self):
                self.killed = True

        clock = Clock()
        process = Process(clock)

        def spawn(*args, **kwargs):
            clock.now = 2.0
            return process

        with patch.object(models.time, "monotonic",
                          side_effect=clock.monotonic), \
                patch.object(models, "popen", side_effect=spawn), \
                patch.object(models, "_reap_killed_process") as reap:
            model = ClaudeCliModel("claude", timeout=600.0)
            with self.assertRaisesRegex(ModelError, "timed out"):
                model.ask("system", "user", deadline=5.0)

        self.assertEqual(process.timeouts, [3.0])
        self.assertTrue(process.killed)
        reap.assert_called_once_with(process)


if __name__ == "__main__":
    unittest.main()
