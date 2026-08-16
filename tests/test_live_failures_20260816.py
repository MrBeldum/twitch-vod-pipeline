"""Regressions for the failures a live 8-hour hasanabi recording produced.

Four defects showed up on 2026-08-16 that no fixture had exercised, because
each of them needs either a real ASR provider, a real multi-gigabyte chunk, or
a proxy encode long enough to overlap another job:

* every second rolling pass failed on a Deepgram word ending past the response's
  reported audio duration -- covered by the parsing tests in
  `test_confirmed_audit_fixes` and `test_media_asr_coverage_contracts`;
* every chunk finalised incomplete over a 1 ms or 34 ms measurement difference,
  abandoning its last 68-96 seconds untranscribed -- covered in
  `test_media_asr_coverage_contracts`;
* every proxy was refused by the disk guard, which asked for 319 GB on a 200 GB
  drive for an encode that came to 538 MB;
* the one chunk that got as far as a rundown lost it to a 60-second wait on a
  chunk mutation lock the proxy encode was holding for its whole run.

The last two are here.
"""

from __future__ import annotations

import math
import shutil
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from vodpipe import media
from vodpipe.config import DEFAULTS, Config, deep_merge
from vodpipe.locks import (
    ResourceBusy,
    ResourceLock,
    chunk_lock_path,
    media_lock_path,
)
from vodpipe.pipeline import Pipeline
from vodpipe.state import COMPLETE, Chunk, Session
from vodpipe.transcript import Word, save_words
from vodpipe.util import Tools


TOOLS = Tools("ffmpeg", "ffprobe", "streamlink", None)

# 1080p60 downscaled to 540p, which is what `proxies.height` defaults to.
SOURCE_1080P60 = {
    "streams": [
        {"index": 0, "codec_type": "video", "codec_name": "h264",
         "width": 1920, "height": 1080, "avg_frame_rate": "60/1",
         "r_frame_rate": "60/1", "duration": "7200.0", "start_time": "0.0",
         "disposition": {"default": 1}},
        {"index": 1, "codec_type": "audio", "codec_name": "aac",
         "channels": 2, "channel_layout": "stereo", "duration": "7200.0",
         "start_time": "0.0", "disposition": {"default": 1},
         "tags": {"language": "eng"}},
    ],
    "format": {"duration": "7200.0"},
}


class ProxyReservationTests(unittest.TestCase):
    """The reservation has to be one a real drive can actually satisfy."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="vodpipe-proxy-estimate-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.source = self.tmp / "chan_c000.mp4"
        self.source.write_bytes(b"m")

    def estimate(self, **options) -> int:
        with patch.object(media, "ffprobe_json", return_value=SOURCE_1080P60):
            return media.estimate_proxy_peak_bytes(
                TOOLS, self.source, **options)

    def test_two_hour_proxy_fits_on_the_drive_that_refused_it(self):
        """AUD3-002: 319.3 GB was asked for on a drive with 201.2 GB free."""
        needed = self.estimate(height=540, quality=24, audio_bitrate="128k")
        self.assertLess(needed, 20 * 1024 ** 3)
        # 201.2 GB free minus the 10 GB hard reserve was the real situation.
        self.assertLess(needed, 191 * 1024 ** 3)

    def test_the_estimate_still_covers_the_encode_several_times_over(self):
        """c003 was 3532.5s of 1080p60 and its proxy came to 538.2 MB."""
        probe = {
            "streams": SOURCE_1080P60["streams"],
            "format": {"duration": "3532.5"},
        }
        with patch.object(media, "ffprobe_json", return_value=probe):
            needed = media.estimate_proxy_peak_bytes(
                TOOLS, self.source, height=540, quality=24,
                audio_bitrate="128k")
        observed = int(538.2 * 1024 ** 2)
        self.assertGreater(needed, observed * 4)

    def test_lower_quality_numbers_reserve_more_and_raw_is_the_ceiling(self):
        previous = 0
        for quality in (30, 24, 18, 10, 0):
            needed = self.estimate(height=540, quality=quality,
                                   audio_bitrate="128k")
            self.assertGreater(needed, previous)
            previous = needed

        # Nothing may exceed uncompressed yuv420p, which is what the old bound
        # used for every quality setting.
        frames = math.ceil(7200.0 * 60) + 2
        raw = frames * 960 * 540 * 3 // 2
        self.assertLess(self.estimate(height=540, quality=0,
                                      audio_bitrate="128k"),
                        raw * media.PROXY_SAFETY_FACTOR)

    def test_an_unparsable_audio_bitrate_still_reserves_audio(self):
        loose = self.estimate(height=540, quality=24, audio_bitrate="nonsense")
        tight = self.estimate(height=540, quality=24, audio_bitrate="128k")
        self.assertGreater(loose, tight)


class ProxyLockScopeTests(unittest.TestCase):
    """A proxy encode must not sit on the chunk's transcript mutation lock."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="vodpipe-proxy-lock-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.config = Config(deep_merge(DEFAULTS, {
            "paths": {"masters_root": str(self.tmp / "m"),
                      "work_root": str(self.tmp / "w"),
                      "censor_master_list": str(self.tmp / "none.txt")},
            "recording": {"free_space_floor_gb": 0.001,
                          "hard_reserve_gb": 0.0005},
            "watcher": {"enabled": False},
            "dashboard": {"open_browser": False},
        }), self.tmp / "config.json")
        self.config.masters_root.mkdir(parents=True)
        self.pipeline = Pipeline(self.config)
        self.addCleanup(self.pipeline.shutdown, job_timeout=10)
        self.directory = self.config.masters_root / "chan" / "sess"
        (self.directory / "master").mkdir(parents=True)
        self.session = self.pipeline.store.add(Session(
            session_id="sess", channel="chan", started_at=1.0,
            directory=str(self.directory), status=COMPLETE))
        self.chunk = Chunk(
            index=0, session_id="sess", channel="chan", started_at=1.0,
            ts_name="chan_c000.ts", master_name="chan_c000.mp4",
            duration=10.0, status=COMPLETE)
        self.pipeline.store.add_chunk(self.session, self.chunk)
        self.master = self.directory / "master" / self.chunk.master_name
        self.master.write_bytes(b"m" * 2048)

    def running_proxy(self, ownership=None):
        """Queue a proxy the way finalisation does and stall it mid-encode.

        Deliberately goes through `_queue_proxy` rather than calling the encode
        directly: the lock this is about was taken by the job wrapper, not by
        the encode, so a test that bypasses queueing would not have caught it.
        """
        encoding, release = threading.Event(), threading.Event()

        def slow_encode(_tools, _source, destination, **_kwargs):
            encoding.set()
            release.wait(10)
            Path(destination).parent.mkdir(parents=True, exist_ok=True)
            Path(destination).write_bytes(b"p")

        patches = [
            patch("vodpipe.pipeline.estimate_proxy_peak_bytes",
                  return_value=1024),
            patch("vodpipe.pipeline.probe_encoder", return_value="libx264"),
            patch("vodpipe.pipeline.make_proxy", side_effect=slow_encode),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

        job = self.pipeline._queue_proxy(
            self.session, self.chunk, ownership=ownership)
        self.assertIsNotNone(job)
        self.addCleanup(release.set)
        self.assertTrue(encoding.wait(10), "the proxy encode never started")
        return release

    def test_a_running_encode_leaves_the_mutation_lock_free(self):
        """AUD3-001: the rundown used to wait 60 s on this and then fail."""
        release = self.running_proxy()
        lock = ResourceLock(
            chunk_lock_path(self.session.path, self.chunk.label))
        # No timeout: the lock must be free *now*, not eventually. This is the
        # same acquisition `_summarize_inner` makes to commit a rundown.
        lock.acquire().release()
        release.set()

    def test_finalisation_ownership_is_not_inherited_by_the_encode(self):
        """A group holding the chunk lock must not be extended by the proxy.

        `_finalize_chunk` passes its ownership group down. Inheriting it kept
        the chunk lock held for the whole encode just as surely as taking it
        directly did.
        """
        lock = ResourceLock(
            chunk_lock_path(self.session.path, self.chunk.label)).acquire()
        group = self.pipeline._ownership(lock)
        release = self.running_proxy(ownership=group)
        group.populated()
        # The group had nothing but its own token, so the lock is already back.
        peer = ResourceLock(
            chunk_lock_path(self.session.path, self.chunk.label))
        peer.acquire().release()
        release.set()

    def test_two_encoders_cannot_stage_the_same_proxy_at_once(self):
        """Dropping the chunk lock must not drop exclusion on the output."""
        release = self.running_proxy()
        destination = (self.master.parent / "Proxies" /
                       f"{self.master.stem}_Proxy.mp4")
        peer = ResourceLock(
            media_lock_path(self.config.masters_root, destination))
        with self.assertRaises(ResourceBusy):
            peer.acquire()
        release.set()

    def test_startup_clears_an_orphaned_proxy_partial(self):
        """Killing the app mid-encode left a multi-gigabyte file behind.

        Recovery swept `master/*.partial.mp4` but the proxy stages its own into
        `master/Proxies/`, which the glob never reached.
        """
        proxies = self.master.parent / "Proxies"
        proxies.mkdir(exist_ok=True)
        orphan = proxies / f"{self.master.stem}_Proxy.partial.mp4"
        orphan.write_bytes(b"x" * 4096)
        remux_orphan = self.master.parent / "chan_c000.partial.mp4"
        remux_orphan.write_bytes(b"x" * 4096)

        with patch.object(self.pipeline.media_jobs, "submit", return_value=None), \
                patch.object(self.pipeline.jobs, "submit", return_value=None):
            self.pipeline.recover()

        self.assertFalse(orphan.exists(), "proxy partial survived recovery")
        self.assertFalse(remux_orphan.exists(), "remux partial survived recovery")

    def test_startup_adopts_a_transcript_that_covered_the_readable_audio(self):
        """Otherwise recovery re-finalises the same chunk on every boot.

        `advance()` records coverage against the audio ffprobe measured, so a
        complete transcript's `covered_seconds` sits a few milliseconds below
        the recorder's manifest duration. Comparing those two at 1 ms judged
        every such chunk incomplete forever.
        """
        self.chunk.duration = 7200.901
        save_words(
            self.pipeline.transcriber.words_path(self.session, self.chunk),
            [Word("hello", 0.0, 0.5, 0.9)],
            {"complete": True, "covered_seconds": 7200.867,
             "expected_seconds": 7200.867})
        owner = _CountingOwner()

        with patch.object(self.pipeline, "_seam_recovery_needed",
                          return_value=False), \
                patch.object(self.pipeline, "_recover_summary_state",
                             return_value=False), \
                patch.object(self.pipeline.media_jobs, "submit",
                             return_value=None):
            self.pipeline._recover_artifacts(self.session, self.chunk, owner)

        self.assertEqual(self.chunk.transcript_status, "done")
        self.assertFalse(
            [key for key in owner.keys if "recover-transcript" in key],
            "a complete transcript was queued for re-finalisation")


class _CountingOwner:
    def __init__(self):
        self.keys: list[str] = []

    def submit(self, pool, key, label, kind, work):
        self.keys.append(key)
        return None


if __name__ == "__main__":
    unittest.main()


class CaptureResilienceTests(unittest.TestCase):
    """The recording must survive a network stall, not die on one.

    zy0xxx was captured three times in fifteen minutes and every attempt died
    the same way: streamlink logged `Sequence gap of 23 segments ... will result
    in incoherent output data`, the timestamps jumped, and ffmpeg aborted with
    `Application provided invalid, non monotonically increasing dts to muxer in
    stream 2` -> -22.
    """

    def command(self) -> list[str]:
        return media.segment_command(
            TOOLS, Path("out_%05d.ts"), Path("segments.csv"), 7200)

    def test_only_video_and_audio_are_captured(self):
        """Stream 2 was Twitch's timed_id3, and it is dropped at remux anyway."""
        cmd = self.command()
        maps = [cmd[i + 1] for i, a in enumerate(cmd) if a == "-map"]
        self.assertEqual(maps, ["0:v?", "0:a?"])
        self.assertNotIn("0", maps, "-map 0 captures the data stream again")

    def test_corrupt_input_packets_are_discarded_not_fatal(self):
        cmd = self.command()
        flags = cmd[cmd.index("-fflags") + 1]
        self.assertIn("discardcorrupt", flags)
        self.assertIn("genpts", flags)

    def test_a_dropped_data_stream_leaves_nothing_for_remux_to_warn_about(self):
        """The capture change should also retire the per-chunk WARNING."""
        streams = [
            {"index": 0, "codec_type": "video", "codec_name": "h264",
             "width": 1920, "height": 1080, "disposition": {"default": 1}},
            {"index": 1, "codec_type": "audio", "codec_name": "aac",
             "channels": 2, "disposition": {"default": 1}},
        ]
        _, dropped = media.plan_remux_maps(streams)
        self.assertEqual(dropped, [])


class ExitCodeReportingTests(unittest.TestCase):
    def test_windows_unsigned_status_is_shown_as_the_code_ffmpeg_printed(self):
        """`ffmpeg exited 4294967274` appears nowhere in ffmpeg's own output."""
        from vodpipe.recorder import _exit_code
        self.assertEqual(_exit_code(4294967274), "-22")
        self.assertEqual(_exit_code(1), "1")
        self.assertEqual(_exit_code(0), "0")
        self.assertEqual(_exit_code(255), "255")
        self.assertEqual(_exit_code(2 ** 32 - 1), "-1")


class RundownRetryTests(unittest.TestCase):
    """A transient engine failure must not permanently lose a rundown."""

    def model(self, **kwargs):
        from vodpipe.models import ClaudeCliModel
        return ClaudeCliModel("claude.exe", timeout=60.0, **kwargs)

    def test_a_transient_failure_is_retried_and_can_succeed(self):
        from vodpipe.models import ModelError
        model = self.model(max_retries=3)
        calls = []

        def once(system, user, deadline):
            calls.append(1)
            if len(calls) < 3:
                raise ModelError("claude -p failed (1): rate limit")
            return "## Overview\nit worked"

        with patch.object(model, "_ask_once", side_effect=once), \
                patch("vodpipe.models.time.sleep"):
            self.assertEqual(model.ask("sys", "body"), "## Overview\nit worked")
        self.assertEqual(len(calls), 3)

    def test_retries_are_bounded_and_the_last_error_survives(self):
        from vodpipe.models import ModelError
        model = self.model(max_retries=3)
        with patch.object(model, "_ask_once",
                          side_effect=ModelError("claude -p failed (1): nope")), \
                patch("vodpipe.models.time.sleep"):
            with self.assertRaisesRegex(ModelError, "nope"):
                model.ask("sys", "body")

    def test_an_empty_stderr_no_longer_produces_an_empty_reason(self):
        """The exact message from the c000 rundown: `claude -p failed (1):`."""
        from vodpipe.models import ModelError

        class Proc:
            returncode = 1

            def communicate(self, data, timeout=None):
                # The CLI explained itself on stdout, as a --print CLI does.
                return (b"Usage limit reached. Resets at 6pm.", b"")

        model = self.model(max_retries=1)
        with patch("vodpipe.models.popen", return_value=Proc()):
            with self.assertRaises(ModelError) as caught:
                model.ask("sys", "body")
        message = str(caught.exception)
        self.assertIn("Usage limit reached", message)
        self.assertNotRegex(message, r":\s*$")

    def test_exiting_zero_with_no_answer_says_so(self):
        from vodpipe.models import ModelError

        class Proc:
            returncode = 0

            def communicate(self, data, timeout=None):
                return (b"", b"")

        model = self.model(max_retries=1)
        with patch("vodpipe.models.popen", return_value=Proc()):
            with self.assertRaisesRegex(ModelError, "exited 0 without"):
                model.ask("sys", "body")
