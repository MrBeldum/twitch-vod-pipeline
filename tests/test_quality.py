"""Capture resolution: what Twitch offered, what we took, and saying so.

The failure this guards against is not hypothetical. On 2026-08-13 four
consecutive two-hour chunks of `examplechannel` were recorded at 1280x720 and nothing
in the product mentioned it; it was discovered by opening a master. The log had
the answer all along:

    [cli][info] Available streams: audio_only, 160p (worst), 360p, 480p, 720p60 (best)
    [cli][info] Opening stream: 720p60 (hls)

Two very different causes produce a low-resolution master, and conflating them
sends the operator chasing a setting that cannot help. These tests pin the
distinction.
"""

from __future__ import annotations

import shutil
import tempfile
import time
import unittest
from pathlib import Path

from vodpipe.config import DEFAULTS, Config, deep_merge
from vodpipe.quality import (
    LOW_QUALITY_POLICIES,
    QualityReport,
    best_height,
    parse_available,
    parse_opening,
    rendition_fps,
    rendition_height,
)
from vodpipe.recorder import Recorder
from vodpipe.state import Session, SessionStore
from vodpipe.util import Tools

# Verbatim from the recording that exposed the problem.
HASANABI_LADDER = (
    "[cli][info] Available streams: audio_only, 160p (worst), 360p, 480p, "
    "720p60 (best)"
)
HASANABI_OPENING = "[cli][info] Opening stream: 720p60 (hls)"
# Verbatim from a channel on a custom transcode stack, probed the same day.
FULL_LADDER = (
    "[cli][info] Available streams: audio_only, 160p30 (worst), 360p30, 480p30, "
    "720p60, 1080p60 (best)"
)
FULL_OPENING = "[cli][info] Opening stream: 1080p60 (hls)"


class ParsingTests(unittest.TestCase):
    def test_ladder_is_parsed_without_its_annotations(self):
        self.assertEqual(
            parse_available(HASANABI_LADDER),
            ["audio_only", "160p", "360p", "480p", "720p60"],
        )

    def test_frame_rate_suffixes_survive(self):
        self.assertEqual(
            parse_available(FULL_LADDER),
            ["audio_only", "160p30", "360p30", "480p30", "720p60", "1080p60"],
        )

    def test_a_single_rendition_ladder_annotates_both_ends(self):
        """`720p60 (worst, best)` splits on its own comma; the fragment is dropped."""
        line = "[cli][info] Available streams: 720p60 (worst, best)"
        self.assertEqual(parse_available(line), ["720p60"])

    def test_unrelated_lines_are_not_ladders(self):
        self.assertIsNone(parse_available("[cli][info] Opening stream: 720p60 (hls)"))
        self.assertIsNone(parse_available("[stream.hls][debug] Reloading playlist"))
        self.assertIsNone(parse_available(""))

    def test_opening_line_yields_the_rendition(self):
        self.assertEqual(parse_opening(HASANABI_OPENING), "720p60")
        self.assertEqual(parse_opening(FULL_OPENING), "1080p60")

    def test_opening_is_none_for_other_lines(self):
        self.assertIsNone(parse_opening(HASANABI_LADDER))
        self.assertIsNone(parse_opening("[cli][info] Found matching plugin twitch"))

    def test_heights_and_frame_rates(self):
        self.assertEqual(rendition_height("1080p60"), 1080)
        self.assertEqual(rendition_height("720p60"), 720)
        self.assertEqual(rendition_height("480p"), 480)
        self.assertEqual(rendition_fps("1080p60"), 60)
        self.assertEqual(rendition_fps("480p"), 0)

    def test_audio_only_is_not_a_low_resolution(self):
        """It is the absence of video, not a degraded picture."""
        self.assertEqual(rendition_height("audio_only"), 0)
        self.assertEqual(rendition_height("audio"), 0)

    def test_aliases_have_no_knowable_height(self):
        for alias in ("best", "worst", "source"):
            self.assertEqual(rendition_height(alias), 0, alias)

    def test_best_height_ignores_audio_only(self):
        self.assertEqual(best_height(["audio_only", "160p", "720p60"]), 720)
        self.assertEqual(best_height(["audio_only"]), 0)
        self.assertEqual(best_height([]), 0)


class PolicyTests(unittest.TestCase):
    def report(self, selected, available, floor=1080):
        return QualityReport(selected=selected, available=available, floor=floor)

    def test_a_capture_at_the_floor_passes(self):
        report = self.report("1080p60", ["720p60", "1080p60"])
        self.assertTrue(report.meets_floor)
        self.assertEqual(report.describe(), "")

    def test_twitch_cap_is_named_as_such(self):
        """The real examplechannel case: nothing better was on the ladder."""
        report = self.report("720p60", ["audio_only", "160p", "360p", "480p", "720p60"])
        self.assertFalse(report.meets_floor)
        self.assertTrue(report.capped_by_twitch)
        message = report.describe()
        self.assertIn("Twitch offered nothing better", message)
        # Must not send the user to a setting that cannot help.
        self.assertNotIn("recording.quality", message)

    def test_picking_below_an_available_rendition_blames_the_setting(self):
        report = self.report("720p60", ["720p60", "1080p60"])
        self.assertFalse(report.meets_floor)
        self.assertFalse(report.capped_by_twitch)
        message = report.describe()
        self.assertIn("1080p was available", message)
        self.assertIn("recording.quality", message)

    def test_floor_of_zero_disables_the_check(self):
        report = self.report("160p", ["160p"], floor=0)
        self.assertTrue(report.meets_floor)
        self.assertEqual(report.describe(), "")

    def test_nothing_is_judged_before_a_rendition_is_known(self):
        """An `Available streams:` line alone must not raise a false alarm."""
        report = self.report("", ["720p60"])
        self.assertFalse(report.known)
        self.assertTrue(report.meets_floor)
        self.assertEqual(report.describe(), "")

    def test_audio_only_selection_is_not_reported_as_low_quality(self):
        report = self.report("audio_only", ["audio_only"])
        self.assertFalse(report.known)
        self.assertEqual(report.describe(), "")

    def test_payload_carries_both_causes_apart(self):
        capped = self.report("720p60", ["480p", "720p60"]).to_dict()
        self.assertTrue(capped["capped_by_twitch"])
        self.assertEqual(capped["height"], 720)
        self.assertEqual(capped["best_available"], 720)
        ours = self.report("720p60", ["720p60", "1080p60"]).to_dict()
        self.assertFalse(ours["capped_by_twitch"])
        self.assertEqual(ours["best_available"], 1080)


class ConfigTests(unittest.TestCase):
    def test_defaults_expect_1080_and_warn(self):
        self.assertEqual(DEFAULTS["recording"]["min_height"], 1080)
        self.assertEqual(DEFAULTS["recording"]["on_low_quality"], "warn")

    def test_policy_choices_are_shared_with_the_schema(self):
        from vodpipe.schema import SCHEMA
        validator = SCHEMA["recording.on_low_quality"]
        for policy in LOW_QUALITY_POLICIES:
            self.assertEqual(validator(policy, "recording.on_low_quality"), policy)
        with self.assertRaises(Exception):
            validator("ignore", "recording.on_low_quality")

    def test_a_fallback_chain_is_an_acceptable_quality(self):
        from vodpipe.schema import SCHEMA
        validator = SCHEMA["recording.quality"]
        self.assertEqual(validator("1080p60,best", "recording.quality"),
                         "1080p60,best")


class RecorderWiringTests(unittest.TestCase):
    """The log pump must turn those two lines into persisted session state."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="vodpipe-quality-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.store = SessionStore(self.tmp)

    def recorder(self, **recording):
        config = Config(deep_merge(DEFAULTS, {
            "paths": {"masters_root": str(self.tmp)},
            "recording": recording,
        }))
        rec = Recorder(config, Tools(ffmpeg="ffmpeg", ffprobe="ffprobe",
                                     streamlink="streamlink", claude=""),
                       self.store, "chan")
        directory = self.tmp / "chan" / "s"
        directory.mkdir(parents=True, exist_ok=True)
        rec.session = self.store.add(Session(
            session_id="s", channel="chan", started_at=time.time(),
            directory=str(directory)))
        return rec

    def test_ladder_and_selection_are_recorded(self):
        rec = self.recorder(min_height=1080)
        rec._note_quality(HASANABI_LADDER)
        rec._note_quality(HASANABI_OPENING)
        session = rec.session
        self.assertEqual(session.quality_selected, "720p60")
        self.assertEqual(session.quality_available,
                         ["audio_only", "160p", "360p", "480p", "720p60"])
        self.assertIn("Twitch offered nothing better", session.quality_warning)

    def test_state_survives_a_round_trip_to_disk(self):
        rec = self.recorder(min_height=1080)
        rec._note_quality(HASANABI_LADDER)
        rec._note_quality(HASANABI_OPENING)
        restored = Session.from_dict(rec.session.to_dict())
        self.assertEqual(restored.quality_selected, "720p60")
        self.assertEqual(restored.quality_available, rec.session.quality_available)
        self.assertEqual(restored.quality_warning, rec.session.quality_warning)

    def test_a_good_capture_records_no_warning(self):
        rec = self.recorder(min_height=1080)
        rec._note_quality(FULL_LADDER)
        rec._note_quality(FULL_OPENING)
        self.assertEqual(rec.session.quality_selected, "1080p60")
        self.assertEqual(rec.session.quality_warning, "")

    def test_the_ladder_alone_does_not_warn(self):
        rec = self.recorder(min_height=1080)
        rec._note_quality(HASANABI_LADDER)
        self.assertEqual(rec.session.quality_warning, "")
        self.assertEqual(rec.session.quality_selected, "")

    def test_refuse_policy_stops_the_recording(self):
        rec = self.recorder(min_height=1080, on_low_quality="refuse")
        rec._note_quality(HASANABI_LADDER)
        rec._note_quality(HASANABI_OPENING)
        self.assertTrue(rec._stop.is_set())
        self.assertIn("720p", rec._stop_reason)

    def test_warn_policy_keeps_recording(self):
        rec = self.recorder(min_height=1080, on_low_quality="warn")
        rec._note_quality(HASANABI_LADDER)
        rec._note_quality(HASANABI_OPENING)
        self.assertFalse(rec._stop.is_set())

    def test_repeated_lines_are_idempotent(self):
        """streamlink reopens the stream on its own retries."""
        rec = self.recorder(min_height=1080)
        for _ in range(3):
            rec._note_quality(HASANABI_LADDER)
            rec._note_quality(HASANABI_OPENING)
        self.assertEqual(rec.session.quality_available,
                         ["audio_only", "160p", "360p", "480p", "720p60"])
        self.assertEqual(rec.session.quality_selected, "720p60")

    def test_disabled_floor_records_the_ladder_but_never_warns(self):
        rec = self.recorder(min_height=0)
        rec._note_quality(HASANABI_LADDER)
        rec._note_quality(HASANABI_OPENING)
        self.assertEqual(rec.session.quality_selected, "720p60")
        self.assertEqual(rec.session.quality_warning, "")
        self.assertFalse(rec._stop.is_set())


class ChunkDimensionTests(unittest.TestCase):
    def test_chunk_carries_measured_dimensions(self):
        from vodpipe.state import Chunk
        chunk = Chunk(index=0, session_id="s", channel="c", started_at=0.0,
                      width=1280, height=720)
        self.assertEqual(chunk.to_dict()["height"], 720)

    def test_dimensions_default_to_unknown(self):
        from vodpipe.state import Chunk
        chunk = Chunk(index=0, session_id="s", channel="c", started_at=0.0)
        self.assertEqual((chunk.width, chunk.height), (0, 0))


if __name__ == "__main__":
    unittest.main()
