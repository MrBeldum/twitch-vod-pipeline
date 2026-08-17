"""Regression tests for the audit/product findings closed on 2026-08-15.

Each test pins a specific defect that was verified open against the current code
and then fixed, so a future change that reintroduces it fails here.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from vodpipe.asr import TranscriptionError, parse_deepgram
from vodpipe.exports import publication_is_consistent, write_exports
from vodpipe.media import _fs_limit
from vodpipe.models import _is_retryable_status
from vodpipe.snapshot import SnapshotRequest, SnapshotService, precise_output_cap
from vodpipe.state import Session
from vodpipe.transcript import Word, load_words


class ModelRetryTests(unittest.TestCase):
    """AUD2-033: every 5xx model response is retryable, not a hand-picked four."""

    def test_all_5xx_are_retryable(self):
        for code in (500, 502, 503, 504, 505, 507, 508, 510, 511, 520, 529, 599):
            self.assertTrue(_is_retryable_status(code), code)

    def test_timeout_and_rate_limit_are_retryable(self):
        self.assertTrue(_is_retryable_status(408))
        self.assertTrue(_is_retryable_status(429))

    def test_client_errors_are_not_retryable(self):
        for code in (400, 401, 403, 404, 409, 422):
            self.assertFalse(_is_retryable_status(code), code)


def _deepgram(words, transcript=None):
    alt = {"words": words}
    if transcript is not None:
        alt["transcript"] = transcript
    return {"results": {"channels": [{"alternatives": [alt]}]}}


class DeepgramContradictionTests(unittest.TestCase):
    """AUD2-007: empty words with a non-blank transcript is not silence."""

    def test_transcript_text_without_word_timings_is_refused(self):
        with self.assertRaises(TranscriptionError):
            parse_deepgram(_deepgram([], transcript="hello world"))

    def test_true_silence_is_still_accepted(self):
        self.assertEqual(parse_deepgram(_deepgram([], transcript="")), [])
        self.assertEqual(parse_deepgram(_deepgram([], transcript="   ")), [])
        self.assertEqual(parse_deepgram(_deepgram([])), [])


class PreciseSnapshotCapTests(unittest.TestCase):
    """AUD2-018: a precise re-encode is capped by the admitted reservation."""

    def test_fs_limit_only_applies_to_precise_with_a_cap(self):
        self.assertEqual(_fs_limit(True, 5_000_000), ["-fs", "5000000"])
        self.assertEqual(_fs_limit(False, 5_000_000), [])   # copy: never cap
        self.assertEqual(_fs_limit(True, None), [])
        self.assertEqual(_fs_limit(True, 0), [])

    def test_precise_cap_is_positive_and_scales_with_duration(self):
        short = precise_output_cap(_Tools(), [(Path("x"), 0.0, 10.0)])
        long = precise_output_cap(_Tools(), [(Path("x"), 0.0, 100.0)])
        self.assertGreater(short, 0)
        self.assertGreater(long, short)


class _Tools:
    ffmpeg = ffprobe = streamlink = ""
    claude = None


class PublicationStructureTests(unittest.TestCase):
    """P3: publication consistency validates premiere.json structure, not just hashes."""

    def _published(self, directory: Path):
        words = [Word("Hello", 0.0, 0.4, 0.95), Word("world.", 0.5, 0.4, 0.95)]
        write_exports(
            directory, words, language="en",
            meta={"chunk": "c000", "complete": True},
            words_meta={"language": "en", "complete": True,
                        "covered_seconds": 1.0, "expected_seconds": 1.0})
        return load_words(directory / "source" / "words.json")

    def test_valid_set_is_consistent(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            words, meta = self._published(directory)
            self.assertTrue(publication_is_consistent(directory, words, meta))

    def test_corrupt_premiere_json_is_inconsistent(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            words, meta = self._published(directory)
            (directory / "premiere.json").write_text("{ not json",
                                                     encoding="utf-8")
            self.assertFalse(publication_is_consistent(directory, words, meta))

    def test_structurally_empty_premiere_json_is_inconsistent(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            words, meta = self._published(directory)
            (directory / "premiere.json").write_text(
                json.dumps({"language": "en-us", "segments": [],
                            "speakers": [{"id": "x", "name": "y"}]}),
                encoding="utf-8")
            self.assertFalse(publication_is_consistent(directory, words, meta))


class ResolveRangeFrozenTests(unittest.TestCase):
    """AUD2-066: a finished session's explicit end is not silently clamped."""

    def _service(self, *, live: bool, extent: float) -> SnapshotService:
        service = SnapshotService(config=None, tools=None)  # resolve_range uses neither
        service.session_extent = lambda session: extent
        service._is_live = lambda session: live
        return service

    def _session(self):
        return Session(session_id="s", channel="c", started_at=0.0, directory="/x")

    def test_explicit_end_beyond_extent_is_not_clamped_when_finished(self):
        service = self._service(live=False, extent=20.0)
        start, end = service.resolve_range(
            self._session(), SnapshotRequest(session_id="s", start=0.0, end=600.0))
        self.assertEqual((start, end), (0.0, 600.0))

    def test_open_ended_request_still_takes_the_extent(self):
        service = self._service(live=False, extent=20.0)
        start, end = service.resolve_range(
            self._session(), SnapshotRequest(session_id="s", start=0.0))
        self.assertEqual(end, 20.0)

    def test_live_edge_margin_still_applies(self):
        service = self._service(live=True, extent=20.0)
        _, end = service.resolve_range(
            self._session(), SnapshotRequest(session_id="s", start=0.0))
        self.assertEqual(end, 18.0)


if __name__ == "__main__":
    unittest.main()
