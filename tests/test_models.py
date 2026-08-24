"""Absolute timeout contracts for the `claude -p` transport."""

from __future__ import annotations

import subprocess
import unittest
from unittest.mock import patch

import vodpipe.models as models
from vodpipe.models import ClaudeCliModel, GrokCliModel, ModelError


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


class GrokTransportTests(unittest.TestCase):
    def test_grok_sends_the_prompt_as_a_file_not_argv_or_stdin(self):
        captured = {}

        class Process:
            returncode = 0

            def communicate(self, data=None, *, timeout=None):
                captured["stdin"] = data
                return b"answer", b""

        def spawn(argv, **kwargs):
            captured["argv"] = argv
            return Process()

        with patch.object(models, "popen", side_effect=spawn):
            result = GrokCliModel("grok", timeout=10).ask(
                "system boundary", "untrusted transcript " * 50)
        self.assertEqual(result, "answer")
        argv = captured["argv"]
        self.assertIn("--prompt-file", argv)
        self.assertIn("--cwd", argv)
        self.assertNotIn("-m", argv)
        self.assertEqual(argv[argv.index("--tools") + 1], "")
        self.assertEqual(captured["stdin"], None)
        # A two-hour transcript must not travel on argv.
        self.assertTrue(all("untrusted transcript" not in str(part)
                            for part in argv))

    def test_a_named_grok_model_is_passed_through(self):
        captured = {}

        class Process:
            returncode = 0

            def communicate(self, data=None, *, timeout=None):
                return b"ok", b""

        def spawn(argv, **kwargs):
            captured["argv"] = argv
            return Process()

        with patch.object(models, "popen", side_effect=spawn):
            GrokCliModel("grok", model="grok-4").ask("sys", "user")
        self.assertEqual(captured["argv"][captured["argv"].index("-m") + 1],
                         "grok-4")


if __name__ == "__main__":
    unittest.main()
