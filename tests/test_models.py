"""Transport contracts for the `claude -p` and `grok -p` report engines."""

from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path
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
    """The argv and the delivery contract for `grok -p`.

    Grok is an agent runtime: it offloads a large `--prompt-file` to a file the
    model has to read, and it prints every assistant message rather than only
    the answer. The transport works with that instead of against it -- see
    `GrokCliModel` for the 2026-09-02 recording that established each point.
    """

    @staticmethod
    def _spawn(captured, *, returncode=0, report=None, stdout=b"", stderr=b""):
        """A fake `grok` that writes `report.md` into the `--cwd` it was given."""

        class Process:
            def __init__(self) -> None:
                self.returncode = returncode

            def communicate(self, data=None, *, timeout=None):
                captured["stdin"] = data
                if report is not None:
                    cwd = Path(captured["argv"][captured["argv"].index("--cwd") + 1])
                    (cwd / "report.md").write_text(report, encoding="utf-8")
                return stdout, stderr

        def spawn(argv, **kwargs):
            captured["argv"] = argv
            captured["env"] = kwargs.get("env")
            work = Path(argv[argv.index("--cwd") + 1])
            captured["prompt"] = (work / "prompt.txt").read_text(encoding="utf-8")
            captured["transcript"] = (
                work / "transcript.md").read_text(encoding="utf-8")
            return Process()

        return spawn

    def test_grok_sends_the_prompt_as_a_file_not_argv_or_stdin(self):
        captured = {}
        report = "## Overview\n\n" + "the report " * 60
        with patch.object(models, "popen",
                          side_effect=self._spawn(captured, report=report)):
            result = GrokCliModel("grok", timeout=10).ask(
                "system boundary", "untrusted transcript " * 50)

        self.assertEqual(result, report.strip())
        argv = captured["argv"]
        self.assertIn("--prompt-file", argv)
        self.assertIn("--cwd", argv)
        self.assertNotIn("-m", argv)
        self.assertIsNone(captured["stdin"])
        # A two-hour transcript must not travel on argv.
        self.assertTrue(all("untrusted transcript" not in str(part)
                            for part in argv))
        # It travels as a file the model is told the name of, and the
        # instruction stays small enough to arrive inline rather than being
        # offloaded to a stub the model then has to hunt down.
        self.assertIn("untrusted transcript", captured["transcript"])
        self.assertIn("system boundary", captured["prompt"])
        self.assertIn("transcript.md", captured["prompt"])
        self.assertIn("report.md", captured["prompt"])
        self.assertLess(len(captured["prompt"]), 20_000)

    def test_the_tool_allowlist_is_never_empty(self):
        """`--tools ""` reads as "unset" and restricts nothing.

        Under the old argv that left all 26 built-ins live, including
        `run_terminal_command` and `spawn_subagent`, on a call whose whole job
        is to read one file and write another.
        """
        captured = {}
        report = "## Overview\n\n" + "body " * 200
        with patch.object(models, "popen",
                          side_effect=self._spawn(captured, report=report)):
            GrokCliModel("grok", timeout=10).ask("sys", "user")

        argv = captured["argv"]
        tools = argv[argv.index("--tools") + 1]
        self.assertTrue(tools)
        self.assertEqual(sorted(tools.split(",")),
                         sorted(GrokCliModel.TOOLS))
        for banned in ("run_terminal_command", "spawn_subagent", "web_search",
                       "image_gen"):
            self.assertNotIn(banned, tools)

    def test_more_than_one_turn_is_allowed(self):
        """`--max-turns 1` cannot succeed against an offloaded prompt.

        The single turn is spent reading the file the CLI substituted for the
        prompt, so the run is cancelled before an answer exists. Every report
        of the 2026-09-02 recording died exactly here.
        """
        captured = {}
        report = "## Overview\n\n" + "body " * 200
        with patch.object(models, "popen",
                          side_effect=self._spawn(captured, report=report)):
            GrokCliModel("grok", timeout=10, max_turns=40).ask("sys", "user")

        argv = captured["argv"]
        self.assertGreater(int(argv[argv.index("--max-turns") + 1]), 1)
        # A configured budget below the floor is raised rather than honoured:
        # one turn is not a smaller budget, it is a guaranteed failure.
        self.assertGreaterEqual(
            GrokCliModel("grok", max_turns=1).max_turns, 2)

    def test_another_applications_mcp_servers_are_kept_out(self):
        """Grok adopts the Claude Code configuration unless told not to.

        On the reference install that put 297 Premiere Pro and After Effects
        tools into the context of every report -- tools that edit a real
        project, offered to a model whose job is to write Markdown.
        """
        captured = {}
        report = "## Overview\n\n" + "body " * 200
        with patch.object(models, "popen",
                          side_effect=self._spawn(captured, report=report)):
            GrokCliModel("grok", timeout=10).ask("sys", "user")

        env = captured["env"]
        self.assertEqual(env["GROK_CLAUDE_MCPS_ENABLED"], "0")
        self.assertEqual(env["GROK_CLAUDE_SKILLS_ENABLED"], "0")
        self.assertEqual(env["GROK_MEMORY"], "0")

    def test_the_answer_is_the_file_not_the_narration(self):
        """`--output-format plain` would have published the process notes.

        stdout carries every assistant message run together, so a *successful*
        run under the old argv would have written "I'll start by reading..."
        onto the front of the report. The report is read back from the file the
        model was told to write.
        """
        captured = {}
        report = "## Overview\n\n" + "the real report " * 40
        stdout = json.dumps({
            "text": "I'll start by reading the transcript." + report,
            "stopReason": "end_turn",
        }).encode()
        with patch.object(models, "popen",
                          side_effect=self._spawn(captured, report=report,
                                                  stdout=stdout)):
            result = GrokCliModel("grok", timeout=10).ask("sys", "user")

        self.assertEqual(result, report.strip())
        self.assertNotIn("I'll start by reading", result)

    def test_a_report_written_before_the_turns_ran_out_is_kept(self):
        """The expensive part is done; a missing "DONE" must not discard it."""
        captured = {}
        report = "## Overview\n\n" + "body " * 200
        with patch.object(models, "popen",
                          side_effect=self._spawn(
                              captured, returncode=1, report=report,
                              stderr=b"Error: max turns reached")):
            result = GrokCliModel("grok", timeout=10, max_retries=1).ask(
                "sys", "user")
        self.assertEqual(result, report.strip())

    def test_an_inline_answer_is_salvaged_from_the_first_heading(self):
        captured = {}
        report = "## Overview\n\n" + "body " * 200
        stdout = json.dumps({
            "text": "I'll write the report now.\n" + report,
            "stopReason": "end_turn",
        }).encode()
        with patch.object(models, "popen",
                          side_effect=self._spawn(captured, stdout=stdout)):
            result = GrokCliModel("grok", timeout=10).ask("sys", "user")
        self.assertTrue(result.startswith("## Overview"))
        self.assertNotIn("I'll write the report now", result)

    def test_a_stub_answer_is_refused_rather_than_published(self):
        captured = {}
        with patch.object(models, "popen",
                          side_effect=self._spawn(captured, report="## Overview\nno.")):
            with self.assertRaises(ModelError):
                GrokCliModel("grok", timeout=10, max_retries=1).ask("sys", "user")

    def test_the_failure_names_what_stopped_the_run(self):
        """`grok -p failed (1):` with nothing after the colon is the failure
        mode this project has had to diagnose twice. The JSON payload carries a
        `stopReason` even on a non-zero exit, so say it."""
        captured = {}
        stdout = json.dumps({
            "text": "I'll read the prompt file first.",
            "stopReason": "cancelled",
            "num_turns": 1,
        }).encode()
        with patch.object(models, "popen",
                          side_effect=self._spawn(
                              captured, returncode=1, stdout=stdout,
                              stderr=b"Error: max turns reached")):
            with self.assertRaises(ModelError) as caught:
                GrokCliModel("grok", timeout=10, max_retries=1).ask("sys", "user")

        message = str(caught.exception)
        self.assertIn("max turns reached", message)
        self.assertIn("cancelled", message)
        self.assertIn("report.md", message)

    def test_the_working_directory_is_removed_even_on_failure(self):
        captured = {}
        with patch.object(models, "popen",
                          side_effect=self._spawn(captured, returncode=1)):
            with self.assertRaises(ModelError):
                GrokCliModel("grok", timeout=10, max_retries=1).ask("sys", "user")
        work = Path(captured["argv"][captured["argv"].index("--cwd") + 1])
        self.assertFalse(work.exists())

    def test_a_named_grok_model_is_passed_through(self):
        captured = {}
        report = "## Overview\n\n" + "body " * 200
        with patch.object(models, "popen",
                          side_effect=self._spawn(captured, report=report)):
            GrokCliModel("grok", model="grok-4").ask("sys", "user")
        self.assertEqual(captured["argv"][captured["argv"].index("-m") + 1],
                         "grok-4")


if __name__ == "__main__":
    unittest.main()
