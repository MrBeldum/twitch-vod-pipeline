"""Media timeline correctness (AUD-041, AUD-042, AUD-020, AUD-036).

The seek tests use MPEG-TS with a deliberately nonzero container start time,
because that is exactly the case a finished zero-based MP4 fixture cannot catch.
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from vodpipe.media import (
    extract_audio_slice,
    cut_range,
    ffconcat_quote,
    live_duration,
)
from vodpipe.recorder import parse_segment_row, read_segment_rows
from vodpipe.util import media_duration, resolve_tools, run

# Well clear of the ~1.4s MPEG-TS default, so a doubled offset is unmistakable.
TS_START_OFFSET = 2.8
CLIP_SECONDS = 10


class NonZeroPtsSeekTests(unittest.TestCase):
    """A live .ts starts at a nonzero PTS; `-ss` is nonetheless file-relative."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="vodpipe-seek-"))
        cls.tools = resolve_tools()
        cls.ts = cls.tmp / "src.ts"
        run([
            cls.tools.ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=30",
            "-f", "lavfi", "-i", "sine=frequency=440",
            "-t", str(CLIP_SECONDS),
            "-c:v", "libx264", "-preset", "ultrafast", "-g", "30",
            "-c:a", "aac",
            "-muxpreload", str(TS_START_OFFSET), "-muxdelay", str(TS_START_OFFSET),
            "-f", "mpegts", str(cls.ts),
        ], check=True, timeout=180)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_fixture_really_has_a_nonzero_start_time(self):
        from vodpipe.util import ffprobe_json
        probe = ffprobe_json(self.tools.ffprobe, self.ts)
        start = float(probe["format"]["start_time"])
        self.assertGreater(start, 1.0,
                           "fixture is not exercising the nonzero-PTS case")

    def test_duration_is_elapsed_not_final_timestamp(self):
        # Would be ~12.8 if we reported the last PTS instead of elapsed time.
        self.assertAlmostEqual(media_duration(self.tools.ffprobe, self.ts),
                               CLIP_SECONDS, delta=0.5)

    def test_tail_slice_is_not_truncated(self):
        """The regression: the last window used to come back nearly empty."""
        out = self.tmp / "tail.flac"
        extract_audio_slice(self.tools, self.ts, out, 8.0, 2.0)
        self.assertAlmostEqual(media_duration(self.tools.ffprobe, out), 2.0,
                               delta=0.25)

    def test_first_slice_is_not_shifted(self):
        out = self.tmp / "head.flac"
        extract_audio_slice(self.tools, self.ts, out, 0.0, 3.0)
        self.assertAlmostEqual(media_duration(self.tools.ffprobe, out), 3.0,
                               delta=0.25)

    def test_middle_slice_lands_where_asked(self):
        out = self.tmp / "mid.flac"
        extract_audio_slice(self.tools, self.ts, out, 4.0, 3.0)
        self.assertAlmostEqual(media_duration(self.tools.ffprobe, out), 3.0,
                               delta=0.25)

    def test_slices_tile_the_whole_clip(self):
        """Every second of audio must be reachable by some window."""
        total = 0.0
        for start in (0.0, 2.5, 5.0, 7.5):
            out = self.tmp / f"tile_{start}.flac"
            extract_audio_slice(self.tools, self.ts, out, start, 2.5)
            total += media_duration(self.tools.ffprobe, out)
        self.assertAlmostEqual(total, CLIP_SECONDS, delta=0.6)

    def test_snapshot_cut_honours_file_relative_times(self):
        out = self.tmp / "cut.mp4"
        cut_range(self.tools, self.ts, out, 6.0, 10.0)
        self.assertAlmostEqual(media_duration(self.tools.ffprobe, out), 4.0,
                               delta=1.5)

    def test_live_duration_does_not_scan_by_default(self):
        self.assertGreater(live_duration(self.tools, self.ts), 0.0)


class SegmentListParsingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="vodpipe-csv-"))
        self.path = self.tmp / "segments.csv"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, text: str) -> None:
        self.path.write_text(text, encoding="utf-8")

    def test_plain_rows(self):
        self.write("chan_c000.ts,0.000000,7200.000000\n"
                   "chan_c001.ts,7200.000000,14400.000000\n")
        rows = read_segment_rows(self.path)
        self.assertEqual(len(rows), 2)
        self.assertEqual(parse_segment_row(rows[0]), ("chan_c000.ts", 0.0, 7200.0))

    def test_quoted_filename_containing_a_comma(self):
        """ffmpeg quotes these correctly; split(',') did not."""
        self.write('"cha,nnel_c000.ts",0.000000,1.200000\n')
        rows = read_segment_rows(self.path)
        parsed = parse_segment_row(rows[0])
        self.assertIsNotNone(parsed)
        name, start, end = parsed
        self.assertEqual(name, "cha,nnel_c000.ts")
        self.assertAlmostEqual(start, 0.0)
        self.assertAlmostEqual(end, 1.2)

    def test_partial_final_row_is_withheld_until_complete(self):
        self.write("chan_c000.ts,0.000000,8.000000\nchan_c001.ts,8.000")
        rows = read_segment_rows(self.path)
        self.assertEqual(len(rows), 1, "incomplete trailing row must not be consumed")

        self.write("chan_c000.ts,0.000000,8.000000\n"
                   "chan_c001.ts,8.000000,16.000000\n")
        rows = read_segment_rows(self.path)
        self.assertEqual(len(rows), 2)
        self.assertEqual(parse_segment_row(rows[1])[0], "chan_c001.ts")

    def test_empty_and_blank_files(self):
        self.write("")
        self.assertEqual(read_segment_rows(self.path), [])
        self.write("\n\n")
        self.assertEqual(read_segment_rows(self.path), [])

    def test_missing_file_is_not_an_error(self):
        self.assertEqual(read_segment_rows(self.tmp / "nope.csv"), [])

    def test_unparsable_rows_are_rejected_not_guessed(self):
        for row in ([], ["only"], ["a", "b"], ["a", "x", "y"], ["a", "5.0", "1.0"]):
            self.assertIsNone(parse_segment_row(row), row)

    def test_row_order_is_preserved(self):
        self.write("".join(f"c{i}.ts,{i}.0,{i + 1}.0\n" for i in range(5)))
        rows = read_segment_rows(self.path)
        self.assertEqual([parse_segment_row(row)[0] for row in rows],
                         [f"c{i}.ts" for i in range(5)])


class FFConcatQuotingTests(unittest.TestCase):
    def test_plain_path_is_quoted_with_posix_separators(self):
        quoted = ffconcat_quote(Path("C:/vods/a.ts"))
        self.assertEqual(quoted, "'C:/vods/a.ts'")

    def test_backslashes_never_reach_the_concat_list(self):
        """The tokenizer treats backslash as an escape, so separators must be `/`."""
        self.assertNotIn("\\", ffconcat_quote(Path(r"C:\vods\sub\a.ts")))

    def test_apostrophe_is_escaped(self):
        quoted = ffconcat_quote(Path("C:/Dan's/a.ts"))
        self.assertIn("'\\''", quoted)
        self.assertTrue(quoted.startswith("'") and quoted.endswith("'"))

    def test_spaces_and_unicode_survive(self):
        quoted = ffconcat_quote(Path("C:/my vods/ünïcode.ts"))
        self.assertIn("my vods", quoted)
        self.assertIn("ünïcode", quoted)


class ConcatThroughAwkwardPathTests(unittest.TestCase):
    """Prove the quoting works against real ffmpeg, not just as a string."""

    def test_cross_part_join_from_a_directory_with_an_apostrophe(self):
        from vodpipe.media import cut_and_join

        tools = resolve_tools()
        root = Path(tempfile.mkdtemp(prefix="vodpipe-quote-"))
        awkward = root / "Dan's VODs"
        awkward.mkdir(parents=True)
        try:
            source = awkward / "src.ts"
            run([tools.ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                 "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=30",
                 "-f", "lavfi", "-i", "sine=frequency=440",
                 "-t", "8", "-c:v", "libx264", "-preset", "ultrafast", "-g", "30",
                 "-c:a", "aac", "-f", "mpegts", str(source)],
                check=True, timeout=180)

            out = awkward / "joined.mp4"
            cut_and_join(tools, [(source, 0.0, 2.0), (source, 4.0, 6.0)], out,
                         work_dir=awkward / "work")
            self.assertTrue(out.exists())
            self.assertAlmostEqual(media_duration(tools.ffprobe, out), 4.0, delta=1.5)
        finally:
            shutil.rmtree(root, ignore_errors=True)


class RemuxMappingTests(unittest.TestCase):
    """AUD-021: what enters the master is a decision, not ffmpeg's default."""

    @staticmethod
    def stream(index, codec_type, codec_name):
        return {"index": index, "codec_type": codec_type, "codec_name": codec_name}

    def test_h264_plus_aac_is_mapped_explicitly(self):
        from vodpipe.media import plan_remux_maps
        maps, dropped = plan_remux_maps([
            self.stream(0, "video", "h264"), self.stream(1, "audio", "aac"),
        ])
        self.assertEqual(maps, ["-map", "0:0", "-map", "0:1"])
        self.assertEqual(dropped, [])

    def test_second_audio_track_is_kept(self):
        """ffmpeg's automatic selection would silently drop this."""
        from vodpipe.media import plan_remux_maps
        maps, dropped = plan_remux_maps([
            self.stream(0, "video", "h264"),
            self.stream(1, "audio", "aac"),
            self.stream(2, "audio", "aac"),
        ])
        self.assertEqual(maps.count("-map"), 3)
        self.assertEqual(dropped, [])

    def test_non_h264_video_is_refused(self):
        from vodpipe.media import plan_remux_maps
        for codec in ("hevc", "av1", "vp9"):
            with self.assertRaises(RuntimeError) as caught:
                plan_remux_maps([self.stream(0, "video", codec),
                                 self.stream(1, "audio", "aac")])
            self.assertIn(codec, str(caught.exception))

    def test_missing_video_is_refused(self):
        from vodpipe.media import plan_remux_maps
        with self.assertRaises(RuntimeError):
            plan_remux_maps([self.stream(0, "audio", "aac")])

    def test_audio_mp4_cannot_carry_is_refused_not_dropped(self):
        """Losing an audio track must fail the remux, so the .ts survives.

        This previously returned a video-only plan and listed the Opus track in
        `dropped`. The caller then published that master, validated it, and
        deleted the .ts -- so a commentary or second-language track was gone for
        good and only a log line mentioned it.
        """
        from vodpipe.media import plan_remux_maps
        with self.assertRaises(RuntimeError) as caught:
            plan_remux_maps([
                self.stream(0, "video", "h264"),
                self.stream(1, "audio", "aac"),
                self.stream(2, "audio", "opus"),
                self.stream(3, "data", "bin_data"),
            ])
        message = str(caught.exception)
        self.assertIn("opus", message)
        self.assertIn(".ts has been kept", message)

    def test_sole_unsupported_audio_track_is_also_refused(self):
        from vodpipe.media import plan_remux_maps
        with self.assertRaises(RuntimeError):
            plan_remux_maps([self.stream(0, "video", "h264"),
                             self.stream(1, "audio", "opus")])

    def test_non_audio_extras_are_still_dropped_silently(self):
        """Data and subtitle streams carry no editorial value and MP4 rejects them."""
        from vodpipe.media import plan_remux_maps
        maps, dropped = plan_remux_maps([
            self.stream(0, "video", "h264"),
            self.stream(1, "audio", "aac"),
            self.stream(2, "data", "bin_data"),
        ])
        self.assertEqual(maps.count("-map"), 2)
        self.assertTrue(any("bin_data" in item for item in dropped))


class MasterValidationTests(unittest.TestCase):
    """The .ts is deleted on the strength of this check, so it must be real."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="vodpipe-valid-"))
        cls.tools = resolve_tools()
        cls.good = cls.tmp / "good.mp4"
        run([cls.tools.ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
             "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=30",
             "-f", "lavfi", "-i", "sine=frequency=440",
             "-t", "5", "-c:v", "libx264", "-preset", "ultrafast",
             "-c:a", "aac", str(cls.good)], check=True, timeout=180)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_valid_master_passes(self):
        from vodpipe.media import validate_master
        validate_master(self.tools, self.good, 5.0)

    def test_missing_file_fails(self):
        from vodpipe.media import validate_master
        with self.assertRaises(RuntimeError):
            validate_master(self.tools, self.tmp / "nope.mp4")

    def test_empty_file_fails(self):
        from vodpipe.media import validate_master
        empty = self.tmp / "empty.mp4"
        empty.write_bytes(b"")
        with self.assertRaises(RuntimeError):
            validate_master(self.tools, empty)

    def test_garbage_file_fails(self):
        from vodpipe.media import validate_master
        junk = self.tmp / "junk.mp4"
        junk.write_bytes(b"\x00" * 8192)
        with self.assertRaises(RuntimeError):
            validate_master(self.tools, junk)

    def test_duration_mismatch_fails(self):
        """A master far shorter than the recording means a truncated remux."""
        from vodpipe.media import validate_master
        with self.assertRaises(RuntimeError) as caught:
            validate_master(self.tools, self.good, expected_duration=600.0)
        self.assertIn("600", str(caught.exception))

    def test_catastrophically_short_master_is_rejected(self):
        """AUD2-005: a flat 5s allowance accepted a 0.2s file for a 4s chunk.

        Reproduced against the old code with a real 0.2 second MP4: the master
        validated, so the .ts became eligible for deletion and 3.8 of 4 seconds
        of video were lost.
        """
        from vodpipe.media import validate_master
        tiny = self.tmp / "tiny.mp4"
        run([self.tools.ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
             "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=30",
             "-t", "0.2", "-c:v", "libx264", "-preset", "ultrafast",
             str(tiny)], check=True, timeout=180)
        with self.assertRaises(RuntimeError) as caught:
            validate_master(self.tools, tiny, expected_duration=4.0)
        self.assertIn("short by", str(caught.exception))

    def test_frame_level_slop_is_still_accepted_on_a_short_chunk(self):
        """The floor must not turn ordinary muxer rounding into a failure."""
        from vodpipe.media import validate_master
        validate_master(self.tools, self.good, expected_duration=5.05)

    def test_long_chunk_keeps_the_absolute_allowance(self):
        from vodpipe.media import allowed_shortfall
        # A 2h chunk reaches the explicit long-file cap...
        self.assertAlmostEqual(allowed_shortfall(7200.0), 2.0)
        # ...while a 4s chunk gets one low-frame-rate frame, not half a second.
        self.assertAlmostEqual(allowed_shortfall(4.0), 1.0 / 15.0)
        # Proportional in between.
        self.assertAlmostEqual(allowed_shortfall(100.0), 0.1)

    def test_remux_failure_leaves_the_source_intact(self):
        """AUD-021: a failed remux must never cost the only copy."""
        from vodpipe.media import remux_to_mp4
        source = self.tmp / "audio_only.ts"
        run([self.tools.ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
             "-f", "lavfi", "-i", "sine=frequency=440", "-t", "3",
             "-c:a", "aac", "-f", "mpegts", str(source)], check=True, timeout=120)
        destination = self.tmp / "out.mp4"

        with self.assertRaises(RuntimeError):
            remux_to_mp4(self.tools, source, destination)
        self.assertTrue(source.exists(), "source must survive a failed remux")
        self.assertFalse(destination.exists(), "no partial master may be published")
        self.assertFalse(destination.with_suffix(".partial.mp4").exists())


if __name__ == "__main__":
    unittest.main()
