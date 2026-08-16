"""Secret redaction, summary policy, and UI-facing state (AUD-023, AUD-028, AUD-030)."""

from __future__ import annotations

import logging
import shutil
import tempfile
import unittest
from pathlib import Path

from vodpipe.config import DEFAULTS, Config, deep_merge
from vodpipe.media import streamlink_command
from vodpipe.util import Tools, redact, render_command

TOKEN = "abcdef0123456789deadbeef"


class RedactionTests(unittest.TestCase):
    """AUD-023: the Twitch token travels in argv and was logged verbatim."""

    def tools(self) -> Tools:
        return Tools(ffmpeg="ffmpeg", ffprobe="ffprobe",
                     streamlink="streamlink", claude=None)

    def test_recorder_command_does_not_leak_the_token(self):
        cmd = streamlink_command(self.tools(), "https://twitch.tv/x", "best",
                                 oauth_token=TOKEN)
        self.assertIn(TOKEN, " ".join(cmd), "sanity: the token is in argv")
        self.assertNotIn(TOKEN, render_command(cmd))
        self.assertIn("<redacted>", render_command(cmd))

    def test_oauth_prefix_is_also_redacted(self):
        cmd = streamlink_command(self.tools(), "https://twitch.tv/x", "best",
                                 oauth_token=f"oauth:{TOKEN}")
        self.assertNotIn(TOKEN, render_command(cmd))

    def test_deepgram_style_token_header_is_redacted(self):
        self.assertNotIn(TOKEN, redact(f"Authorization=Token {TOKEN}"))

    def test_ordinary_commands_are_untouched(self):
        cmd = ["ffmpeg", "-i", "in.ts", "-c", "copy", "out.mp4"]
        self.assertEqual(render_command(cmd), "ffmpeg -i in.ts -c copy out.mp4")

    def test_debug_logging_a_recorder_command_never_prints_the_token(self):
        """The real path: -v turns on debug logging for every spawned command."""
        records: list[str] = []

        class Capture(logging.Handler):
            def emit(self, record):
                records.append(record.getMessage())

        logger = logging.getLogger("vodpipe")
        handler = Capture()
        previous = logger.level
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
        try:
            from vodpipe.util import LOG
            cmd = streamlink_command(self.tools(), "https://twitch.tv/x", "best",
                                     oauth_token=TOKEN)
            LOG.debug("popen: %s", render_command(cmd))
        finally:
            logger.removeHandler(handler)
            logger.setLevel(previous)

        self.assertTrue(records)
        self.assertNotIn(TOKEN, "\n".join(records))


class SummaryPolicyTests(unittest.TestCase):
    """AUD-030: `none` is a choice, and byte length was a poor content test."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="vodpipe-sum-"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def config(self, **summary):
        data = deep_merge(DEFAULTS, {
            "paths": {"masters_root": str(self.tmp / "m"),
                      "work_root": str(self.tmp / "w")},
            "summary": summary,
        })
        return Config(data, self.tmp / "config.json")

    def test_provider_none_builds_a_null_summarizer(self):
        from vodpipe.summarize import NullSummarizer, build_summarizer
        summarizer = build_summarizer(self.config(provider="none"), None)
        self.assertIsInstance(summarizer, NullSummarizer)

    def test_min_words_is_configurable_and_validated(self):
        from vodpipe.schema import ConfigError
        config = self.config()
        config.apply({"summary": {"min_words": 5}})
        self.assertEqual(config.get("summary.min_words"), 5)
        with self.assertRaises(ConfigError):
            config.apply({"summary": {"min_words": -1}})

    def test_missing_claude_executable_is_reported_clearly(self):
        from vodpipe.models import ClaudeCliModel
        with self.assertRaises(RuntimeError) as caught:
            ClaudeCliModel("")
        self.assertIn("claude", str(caught.exception).lower())

    def test_anthropic_provider_requires_a_key(self):
        from vodpipe.summarize import build_summarizer
        config = self.config(provider="anthropic-api")
        with self.assertRaises(RuntimeError):
            build_summarizer(config, None)


class ArtifactErrorTests(unittest.TestCase):
    """AUD-022: one artifact's success must not erase another's failure."""

    def chunk(self):
        from vodpipe.state import Chunk
        return Chunk(index=0, session_id="s", channel="c", started_at=0.0)

    def test_errors_are_reported_per_artifact(self):
        chunk = self.chunk()
        chunk.transcript_error = "deepgram refused the key"
        chunk.proxy_error = "encoder failed"
        self.assertEqual(set(chunk.errors), {"transcript", "proxy"})

    def test_a_clean_chunk_reports_nothing(self):
        self.assertEqual(self.chunk().errors, {})

    def test_remux_success_cannot_clear_a_transcript_failure(self):
        chunk = self.chunk()
        chunk.transcript_error = "asr outage"
        chunk.master_error = ""          # a successful remux clears only its own
        self.assertIn("transcript", chunk.errors)

    def test_errors_survive_the_state_round_trip(self):
        from vodpipe.state import Session
        chunk = self.chunk()
        chunk.summary_error = "claude timed out"
        session = Session(session_id="s", channel="c", started_at=0.0, directory="")
        session.chunks.append(chunk)
        restored = Session.from_dict(session.to_dict())
        self.assertEqual(restored.chunks[0].summary_error, "claude timed out")

    def test_a_legacy_shared_error_is_attributed_to_the_master(self):
        from vodpipe.state import Session
        restored = Session.from_dict({
            "session_id": "s", "channel": "c", "started_at": 0.0, "directory": "",
            "chunks": [{"index": 0, "session_id": "s", "channel": "c",
                        "started_at": 0.0, "error": "old style failure"}],
        })
        self.assertEqual(restored.chunks[0].master_error, "old style failure")


if __name__ == "__main__":
    unittest.main()
