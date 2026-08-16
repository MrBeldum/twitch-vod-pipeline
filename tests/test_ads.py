"""Ad-handling semantics (AUD-008, AUD-014, AUD-031).

An earlier build turned streamlink's ad log lines into time ranges and cut those
ranges out of transcripts and masters. That was wrong in two independent ways,
both asserted here:

1. streamlink's Twitch plugin filters ad segments out of the stream *before* it
   reaches our recorder, so no interval of our file is an ad. There is nothing to
   exclude, and excluding anything removes real content.
2. "Will skip ad segments" is logged unconditionally when the HLS reader starts,
   so pattern-matching it opened a phantom ad range at the top of every recording.

These tests are the guard rail against reintroducing that mapping.
"""

from __future__ import annotations

import shutil
import tempfile
import time
import unittest
from pathlib import Path

from vodpipe.config import DEFAULTS
from vodpipe.state import Session, SessionStore


STREAMLINK_STARTUP_LINE = (
    "[plugins.twitch][info] Will skip ad segments"
)
STREAMLINK_BREAK_LINE = (
    "[plugins.twitch][info] Detected advertisement break of 30 seconds"
)
STREAMLINK_PREROLL_LINE = (
    "[plugins.twitch][info] Waiting for pre-roll ads to finish, be patient"
)


def patterns() -> list[str]:
    return [str(item).lower() for item in DEFAULTS["ads"]["event_patterns"]]


def matches(line: str) -> bool:
    lowered = line.lower()
    return any(pattern in lowered for pattern in patterns())


class AdPatternTests(unittest.TestCase):
    def test_startup_notice_is_not_treated_as_an_ad_event(self):
        """The reader logs this for every stream; it means nothing about ads served."""
        self.assertFalse(matches(STREAMLINK_STARTUP_LINE))

    def test_real_ad_lines_are_recognised(self):
        self.assertTrue(matches(STREAMLINK_BREAK_LINE))
        self.assertTrue(matches(STREAMLINK_PREROLL_LINE))

    def test_no_pattern_can_match_the_startup_notice(self):
        # Guards against someone re-adding a loose pattern like "skip ad".
        for pattern in patterns():
            self.assertNotIn(pattern, STREAMLINK_STARTUP_LINE.lower(), pattern)


class NoMediaExclusionTests(unittest.TestCase):
    """The exclusion machinery must be gone, not merely disabled by default."""

    def test_no_config_key_re_enables_media_exclusion(self):
        self.assertNotIn("exclude_ad_ranges", DEFAULTS["transcription"])
        self.assertNotIn("write_adfree_master", DEFAULTS["ads"])
        for removed in ("pad_start_seconds", "pad_end_seconds", "idle_seconds"):
            self.assertNotIn(removed, DEFAULTS["ads"], removed)

    def test_transcriber_has_no_ad_exclusion_step(self):
        from vodpipe import transcribe
        self.assertFalse(hasattr(transcribe.RollingTranscriber, "_exclude_ads"))

    def test_pipeline_cannot_write_an_adfree_master(self):
        from vodpipe import pipeline
        self.assertFalse(hasattr(pipeline.Pipeline, "_write_adfree_master"))

    def test_session_has_no_ad_ranges_field(self):
        session = Session(session_id="s", channel="c", started_at=0.0, directory="")
        self.assertFalse(hasattr(session, "ad_ranges"))
        self.assertEqual(session.ad_events, [])

    def test_legacy_ad_ranges_on_disk_are_not_carried_forward(self):
        """Old state files must not resurrect invalid exclusion intervals."""
        restored = Session.from_dict({
            "session_id": "s", "channel": "c", "started_at": 0.0,
            "directory": "", "ad_ranges": [[10.0, 40.0]],
        })
        self.assertEqual(restored.ad_events, [])
        self.assertFalse(hasattr(restored, "ad_ranges"))


class AdEventRecordingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="vodpipe-ads-"))
        self.store = SessionStore(self.tmp)
        self.session = self.store.add(Session(
            session_id="sess", channel="chan", started_at=time.time(),
            directory=str(self.tmp / "chan" / "sess"),
        ))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_events_are_metadata_not_intervals(self):
        self.store.add_ad_event(self.session, "break", STREAMLINK_BREAK_LINE,
                                1000.0, 42.0)
        event = self.session.ad_events[0]
        self.assertEqual(event["kind"], "break")
        # An event carries a single approximate instant, never a start/end pair
        # that downstream code could mistake for a cut.
        self.assertIn("approx_session_seconds", event)
        self.assertNotIn("start", event)
        self.assertNotIn("end", event)

    def test_events_round_trip_through_state(self):
        self.store.add_ad_event(self.session, "preroll", "x", 1.0, 2.0)
        restored = Session.from_dict(self.session.to_dict())
        self.assertEqual(len(restored.ad_events), 1)
        self.assertEqual(restored.ad_events[0]["kind"], "preroll")


if __name__ == "__main__":
    unittest.main()
