"""Regressions for the failures of the 2026-09-02 recording.

Five chunks, ~7100s each, and the capture half of the pipeline was flawless:
five masters, five proxies, five complete transcripts, chat for all five. Every
single report failed, fourteen attempts, always with the same message:

    grok -p failed (1): Max turns reached
    Error: max turns reached

Two defects, and only the first is the one in the log.

**The report engine could never have worked.** `grok -p` is an agent runtime,
and above roughly 24 KB the CLI does not put a `--prompt-file` in the
conversation at all -- it offloads it and hands the model a stub to `read_file`
its way out of. A two-hour transcript is ~104 KB, so every report took that
path, and `--max-turns 1` meant the one turn available was spent on the read.
The run was cancelled before an answer existed. Three more faults sat behind
it: `--tools ""` restricts nothing (an empty allowlist reads as unset, so all
26 built-ins plus 297 MCP tools imported from the Claude Code configuration
stayed live), `--output-format plain` prints every assistant message so stdout
would have carried the model's narration into the published report, and the
shared prompt's "Write `report.md`" read to an agent as a file-writing task.
The transport contract is in `tests/test_models.py`; what is tested here is the
end of the chain -- that a transcript this size still produces a report.

**A boundary stitch queued the report before chat had landed.**
`_reconcile_generation_changes` called `_queue_summary` directly rather than
the chat-gated `_maybe_queue_summary` that finalisation uses. A stitch changes
the generation of *both* chunks it touches, and the newer one's chat job is
still running at that moment, so every chunk of the recording was queued for
two reports: one written without the audience in it, and one to replace it
when chat arrived. In the log they are eight seconds apart. Nobody noticed
because both of them failed.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest.mock import patch

import vodpipe.models as models
from vodpipe.models import GrokCliModel
from vodpipe.pipeline import _SummarySource
from vodpipe.state import DONE, PENDING, RUNNING

from tests.test_remaining_contracts import PipelineFixture


class ReportQueuedBeforeChatTests(PipelineFixture):
    """A stitched chunk waits for its audience like a finalised one does."""

    def _reconcile(self, chunk, *, generation="feedfacefeedface"):
        source = _SummarySource(generation=generation, body="[00:00:00] hi")
        # The shared fixture ships with reports switched off; this suite is
        # about *when* one is queued, not whether an engine exists.
        with patch.object(self.pipeline, "_summary_enabled",
                          return_value=True), \
                patch.object(self.pipeline, "_current_export_generation",
                             return_value=generation), \
                patch.object(self.pipeline, "_summary_source",
                             return_value=(source, "")), \
                patch.object(self.pipeline, "_retire_obsolete_rundown"):
            self.pipeline._reconcile_generation_changes(
                self.session, [chunk], {chunk.index: "0000000000000000"},
                queue=True)

    def test_a_stitched_chunk_does_not_report_while_chat_is_running(self):
        chunk = self.chunk(0, chat_status=RUNNING)
        keys = self.recorded_media_jobs()
        self._reconcile(chunk)
        self.assertEqual(keys, [])

    def test_the_wait_is_visible_rather_than_silent(self):
        """Deferring must still say a report is coming.

        `_maybe_queue_summary` returns None both when it declines and when the
        pool refuses the job, so the caller has to record the intent itself or
        the dashboard shows the chunk as having no report at all.
        """
        chunk = self.chunk(0, chat_status=PENDING)
        self.recorded_media_jobs()
        self._reconcile(chunk)
        stored = self.session.chunk(chunk.index)
        self.assertEqual(stored.summary_status, PENDING)
        self.assertEqual(stored.summary_error, "")

    def test_a_stitched_chunk_reports_once_chat_is_in(self):
        chunk = self.chunk(0, chat_status=DONE)
        keys = self.recorded_media_jobs()
        self._reconcile(chunk)
        self.assertEqual(
            keys, ["summary:sess:c000:feedfacefeedface"])

    def test_chat_completing_is_what_releases_the_deferred_report(self):
        """The gate is only safe because the chat job re-queues on its way out.

        `_capture_chunk_chat` calls `_maybe_queue_summary` from a `finally`, so
        the report follows chat whether chat succeeded, failed or was skipped.
        """
        chunk = self.chunk(0, chat_status=RUNNING)
        keys = self.recorded_media_jobs()
        self._reconcile(chunk)
        self.assertEqual(keys, [])

        chunk = self.pipeline.store.update_chunk(
            self.session, chunk, chat_status=DONE, chat_error="")
        source = _SummarySource(generation="feedfacefeedface",
                                body="[00:00:00] hi")
        with patch.object(self.pipeline, "_summary_enabled",
                          return_value=True), \
                patch.object(self.pipeline, "_summary_source",
                             return_value=(source, "")), \
                patch.object(self.pipeline, "_retire_obsolete_rundown"):
            self.pipeline._maybe_queue_summary(self.session, chunk)
        self.assertEqual(keys, ["summary:sess:c000:feedfacefeedface"])


class OffloadedPromptTests(unittest.TestCase):
    """The size that broke it, against the argv that has to survive it."""

    # `build_model_input` renders a two-hour chunk of ordinary speech at
    # roughly this size; the CLI offloads anything past ~24 KB.
    TWO_HOUR_TRANSCRIPT_CHARS = 104_000

    def test_a_two_hour_transcript_still_produces_a_report(self):
        captured = {}
        report = "## Overview\n\n" + "the report " * 200

        class Process:
            returncode = 0

            def communicate(self, data=None, *, timeout=None):
                cwd = Path(captured["argv"][captured["argv"].index("--cwd") + 1])
                # What the model does with an offloaded prompt: read the file
                # it was pointed at, then write the report beside it.
                assert (cwd / "transcript.md").read_text(encoding="utf-8")
                (cwd / "report.md").write_text(report, encoding="utf-8")
                return json.dumps({"text": "I'll read the transcript.",
                                   "stopReason": "end_turn"}).encode(), b""

        def spawn(argv, **kwargs):
            captured["argv"] = argv
            return Process()

        transcript = "[00:00:00-00:00:04] some speech here\n" * 3200
        self.assertGreater(len(transcript), self.TWO_HOUR_TRANSCRIPT_CHARS)

        with patch.object(models, "popen", side_effect=spawn):
            result = GrokCliModel("grok", timeout=30).ask(
                "instruction", transcript)

        self.assertEqual(result, report.strip())
        argv = captured["argv"]
        # The three settings that made the failure certain.
        self.assertGreater(int(argv[argv.index("--max-turns") + 1]), 1)
        self.assertTrue(argv[argv.index("--tools") + 1])
        self.assertEqual(argv[argv.index("--output-format") + 1], "json")


if __name__ == "__main__":
    unittest.main()
