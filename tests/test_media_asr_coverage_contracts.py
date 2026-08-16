"""Remaining media topology and ASR coverage contracts."""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from vodpipe import media
from vodpipe.asr import (
    DeepgramProvider,
    TranscriptionError,
    transcribe_audio,
)
from vodpipe.config import DEFAULTS, Config, deep_merge
from vodpipe.snapshot import (
    SnapshotRequest,
    SnapshotService,
    allowed_snapshot_shortfall,
)
from vodpipe.state import (
    COMPLETE,
    DONE,
    ERROR,
    RECORDING,
    Chunk,
    Session,
    SessionStore,
)
from vodpipe.transcribe import (
    BLOCKED,
    COMPLETE_,
    IDLE,
    PROGRESSED,
    RollingTranscriber,
)
from vodpipe.transcript import Word
from vodpipe.util import Tools, ffprobe_json, resolve_tools, run


TOOLS = Tools("ffmpeg", "ffprobe", "streamlink", None)


def stream(index: int, kind: str, *, codec: str | None = None,
           language: str = "und", layout: str = "stereo",
           channels: int = 2, default: int = 0,
           duration: float = 10.0) -> dict:
    result = {
        "index": index,
        "codec_type": kind,
        "codec_name": codec or ("h264" if kind == "video" else "aac"),
        "start_time": "0.0",
        "duration": str(duration),
        "disposition": {"default": default},
    }
    if kind == "video":
        result.update(width=320, height=180, avg_frame_rate="30/1")
    else:
        result.update(channels=channels, channel_layout=layout,
                      tags={"language": language})
    return result


def inventory(*, audio: int = 2, second_language: str = "spa") -> dict:
    streams = [stream(0, "video")]
    if audio >= 1:
        streams.append(stream(1, "audio", language="eng", default=1))
    if audio >= 2:
        streams.append(stream(2, "audio", language=second_language))
    return {"format": {"duration": "10.0"}, "streams": streams}


class FakeProbeTopologyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="vodpipe-topology-fake-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.source = self.tmp / "source.ts"
        self.source.write_bytes(b"s" * 2048)

    @staticmethod
    def write_output(command, **kwargs):
        output = Path(command[-1])
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"o" * 2048)
        return subprocess.CompletedProcess(command, 0, "", "")

    def test_master_missing_source_audio_fails_inventory_validation(self):
        master = self.tmp / "master.mp4"
        master.write_bytes(b"m" * 2048)
        with patch.object(media, "ffprobe_json",
                          side_effect=[inventory(audio=1), inventory(audio=2)]):
            with self.assertRaisesRegex(RuntimeError, "expected 2"):
                media.validate_master(TOOLS, master, 10.0, source=self.source)

    def test_remux_candidate_missing_audio_never_replaces_destination(self):
        destination = self.tmp / "master.mp4"
        destination.write_bytes(b"previous")
        with patch.object(media, "ffprobe_json",
                          side_effect=[inventory(audio=2), inventory(audio=1)]), \
                patch.object(media, "run", side_effect=self.write_output):
            with self.assertRaisesRegex(RuntimeError, "expected 2"):
                media.remux_to_mp4(TOOLS, self.source, destination, 10.0)
        self.assertEqual(destination.read_bytes(), b"previous")
        self.assertFalse(destination.with_suffix(".partial.mp4").exists())

    def test_proxy_candidate_missing_audio_never_commits(self):
        destination = self.tmp / "proxy.mp4"
        destination.write_bytes(b"previous")
        with patch.object(media, "ffprobe_json",
                          side_effect=[inventory(audio=2), inventory(audio=1)]), \
                patch.object(media, "run", side_effect=self.write_output):
            with self.assertRaisesRegex(RuntimeError, "expected 2"):
                media.make_proxy(TOOLS, self.source, destination, height=180)
        self.assertEqual(destination.read_bytes(), b"previous")

    def test_master_short_stream_is_not_hidden_by_other_complete_streams(self):
        master = self.tmp / "short-master.mp4"
        master.write_bytes(b"m" * 2048)
        probe = inventory(audio=1)
        probe["streams"][0]["duration"] = "0.1"
        with patch.object(media, "ffprobe_json", return_value=probe):
            with self.assertRaisesRegex(RuntimeError, "video stream.*short by"):
                media.validate_master(TOOLS, master, 0.6)

    def test_nonzero_start_time_does_not_count_as_elapsed_coverage(self):
        master = self.tmp / "offset-master.mp4"
        master.write_bytes(b"m" * 2048)
        probe = inventory(audio=1)
        for item in probe["streams"]:
            item["start_time"] = "9.0"
            item["duration"] = "0.1"
        with patch.object(media, "ffprobe_json", return_value=probe):
            with self.assertRaisesRegex(RuntimeError, "covers 0.100s"):
                media.validate_master(TOOLS, master, 0.6)

    def test_master_shortfall_threshold_is_inclusive_and_exact(self):
        master = self.tmp / "threshold-master.mp4"
        master.write_bytes(b"m" * 2048)
        at_limit = inventory(audio=1)
        below_limit = inventory(audio=1)
        for item in at_limit["streams"]:
            item["duration"] = "99.9"
        for item in below_limit["streams"]:
            item["duration"] = "99.899"
        with patch.object(media, "ffprobe_json", return_value=at_limit):
            media.validate_master(TOOLS, master, 100.0)
        with patch.object(media, "ffprobe_json", return_value=below_limit):
            with self.assertRaisesRegex(RuntimeError, "more than the 0.100s"):
                media.validate_master(TOOLS, master, 100.0)

    def test_proxy_validation_checks_dimensions_and_each_stream_tail(self):
        proxy = self.tmp / "existing-proxy.mp4"
        proxy.write_bytes(b"p" * 2048)
        short = inventory(audio=2)
        short["streams"][2]["duration"] = "1.0"
        with patch.object(media, "ffprobe_json",
                          side_effect=[inventory(audio=2), short]):
            with self.assertRaisesRegex(RuntimeError, "audio stream 2.*short by"):
                media.validate_proxy(TOOLS, self.source, proxy, height=180)
        with patch.object(media, "ffprobe_json",
                          side_effect=[inventory(audio=2), inventory(audio=2)]):
            with self.assertRaisesRegex(RuntimeError, "video topology changed"):
                media.validate_proxy(TOOLS, self.source, proxy, height=540)

    def test_precise_cut_candidate_missing_audio_never_commits(self):
        destination = self.tmp / "cut.mp4"
        destination.write_bytes(b"previous")
        with patch.object(media, "ffprobe_json",
                          side_effect=[inventory(audio=2), inventory(audio=1)]), \
                patch.object(media, "run", side_effect=self.write_output):
            with self.assertRaisesRegex(RuntimeError, "expected 2"):
                media.cut_range(
                    TOOLS, self.source, destination, 0.0, 2.0, precise=True)
        self.assertEqual(destination.read_bytes(), b"previous")

    def test_both_single_cut_modes_reject_a_short_required_stream(self):
        for precise in (False, True):
            with self.subTest(precise=precise):
                destination = self.tmp / f"short-cut-{precise}.mp4"
                destination.write_bytes(b"previous")
                short = inventory(audio=2)
                short["streams"][1]["duration"] = "0.2"
                with patch.object(media, "ffprobe_json", side_effect=[
                        inventory(audio=2), short,
                ]), patch.object(media, "run", side_effect=self.write_output):
                    with self.assertRaisesRegex(
                            RuntimeError, "audio stream 1.*short by"):
                        media.cut_range(
                            TOOLS, self.source, destination, 0.0, 2.0,
                            precise=precise)
                self.assertEqual(destination.read_bytes(), b"previous")

    def test_incompatible_join_pieces_are_rejected_before_ffmpeg(self):
        other = self.tmp / "other.ts"
        other.write_bytes(b"s" * 2048)
        with patch.object(media, "ffprobe_json", side_effect=[
                inventory(audio=2, second_language="spa"),
                inventory(audio=2, second_language="fra"),
        ]), patch.object(media, "run") as run_mock:
            with self.assertRaisesRegex(RuntimeError, "incompatible"):
                media.cut_and_join(
                    TOOLS,
                    [(self.source, 0.0, 1.0), (other, 0.0, 1.0)],
                    self.tmp / "joined.mp4")
        run_mock.assert_not_called()

    def test_join_candidate_missing_audio_never_replaces_destination(self):
        other = self.tmp / "other.ts"
        other.write_bytes(b"s" * 2048)
        destination = self.tmp / "joined.mp4"
        destination.write_bytes(b"previous")
        full = inventory(audio=2)
        with patch.object(media, "ffprobe_json", side_effect=[
                full, full, full, full, full, inventory(audio=1),
        ]), patch.object(media, "run", side_effect=self.write_output):
            with self.assertRaisesRegex(RuntimeError, "expected 2"):
                media.cut_and_join(
                    TOOLS,
                    [(self.source, 0.0, 1.0), (other, 0.0, 1.0)],
                    destination, work_dir=self.tmp / "join-work")
        self.assertEqual(destination.read_bytes(), b"previous")

    def test_join_candidate_rejects_a_short_required_stream(self):
        other = self.tmp / "other-short.ts"
        other.write_bytes(b"s" * 2048)
        destination = self.tmp / "joined-short.mp4"
        destination.write_bytes(b"previous")
        full = inventory(audio=2)
        short = inventory(audio=2)
        short["streams"][2]["duration"] = "0.5"
        with patch.object(media, "ffprobe_json", side_effect=[
                full, full, full, full, full, short,
        ]), patch.object(media, "run", side_effect=self.write_output):
            with self.assertRaisesRegex(RuntimeError, "audio stream 2.*short by"):
                media.cut_and_join(
                    TOOLS,
                    [(self.source, 0.0, 1.0), (other, 0.0, 1.0)],
                    destination, work_dir=self.tmp / "join-short-work")
        self.assertEqual(destination.read_bytes(), b"previous")


class RealProbeTopologyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tools = resolve_tools()
        cls.tmp = Path(tempfile.mkdtemp(prefix="vodpipe-topology-real-"))
        cls.source = cls.tmp / "source.ts"
        run([
            cls.tools.ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=30",
            "-f", "lavfi", "-i", "sine=frequency=440",
            "-f", "lavfi", "-i", "sine=frequency=880",
            "-t", "2", "-map", "0:v:0", "-map", "1:a:0", "-map", "2:a:0",
            "-metadata:s:a:0", "language=eng",
            "-metadata:s:a:1", "language=spa",
            "-c:v", "libx264", "-preset", "ultrafast", "-g", "30",
            "-c:a", "aac", "-f", "mpegts", str(cls.source),
        ], check=True, timeout=180)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def assert_two_audio(self, path: Path, *, audio_codec: str = "aac") -> None:
        streams = ffprobe_json(self.tools.ffprobe, path)["streams"]
        video = [item for item in streams if item.get("codec_type") == "video"]
        audio = [item for item in streams if item.get("codec_type") == "audio"]
        self.assertEqual(len(video), 1)
        self.assertEqual(len(audio), 2)
        self.assertEqual([item.get("codec_name") for item in audio],
                         [audio_codec, audio_codec])
        self.assertEqual([item.get("tags", {}).get("language") for item in audio],
                         ["eng", "spa"])

    def test_real_remux_preserves_both_logical_audio_tracks(self):
        output = self.tmp / "master.mp4"
        media.remux_to_mp4(self.tools, self.source, output, 2.0)
        self.assert_two_audio(output)
        source_streams = ffprobe_json(
            self.tools.ffprobe, self.source)["streams"]
        output_streams = ffprobe_json(
            self.tools.ffprobe, output)["streams"]
        _, source_identity = media.asr_stream_identity(source_streams)
        _, output_identity = media.asr_stream_identity(output_streams)
        self.assertEqual(source_identity, output_identity)

    def test_real_proxy_and_both_cut_modes_preserve_audio_topology(self):
        proxy = self.tmp / "proxy.mp4"
        copied = self.tmp / "copied.mp4"
        precise = self.tmp / "precise.mp4"
        media.make_proxy(
            self.tools, self.source, proxy, height=180, encoder="libx264")
        media.cut_range(self.tools, self.source, copied, 0.0, 1.5)
        media.cut_range(
            self.tools, self.source, precise, 0.0, 1.5, precise=True)
        for output in (proxy, copied, precise):
            self.assert_two_audio(output)


class DeepgramSubmittedDurationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="vodpipe-dg-duration-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.audio = self.tmp / "slice.flac"
        self.audio.write_bytes(b"audio")

    def test_caller_duration_clamps_an_overrunning_word_without_metadata(self):
        response = {"results": {"channels": [{"alternatives": [{"words": [{
            "word": "late", "start": 0.9, "end": 1.1, "confidence": 0.9,
        }]}]}]}}
        provider = DeepgramProvider("key", max_retries=1)
        with patch.object(provider, "_post", return_value=response):
            words = provider.transcribe(self.audio, expected_duration=1.0)
        self.assertEqual([word.text for word in words], ["late"])
        self.assertAlmostEqual(words[0].end, 1.0, places=6)

    def test_our_measurement_outranks_the_responses_own_duration(self):
        """nova-3 rounds `metadata.duration` down; ffprobe does not.

        The submitted duration is the one we measured ourselves, so it is what
        bounds the words. A response claiming 2s of audio for a 63s slice must
        not retire 61 seconds of real speech.
        """
        response = {
            "metadata": {"duration": 2.0},
            "results": {"channels": [{"alternatives": [{"words": [{
                "word": "kept", "start": 40.0, "end": 40.5, "confidence": 0.9,
            }]}]}]},
        }
        provider = DeepgramProvider("key", max_retries=1)
        with patch.object(provider, "_post", return_value=response):
            words = provider.transcribe(self.audio, expected_duration=63.0)
        self.assertEqual([word.text for word in words], ["kept"])
        self.assertAlmostEqual(words[0].end, 40.5, places=6)

    def test_a_word_starting_past_the_submitted_audio_is_rejected(self):
        response = {"results": {"channels": [{"alternatives": [{"words": [{
            "word": "elsewhere", "start": 30.0, "end": 30.4, "confidence": 0.9,
        }]}]}]}}
        provider = DeepgramProvider("key", max_retries=1)
        with patch.object(provider, "_post", return_value=response):
            with self.assertRaisesRegex(TranscriptionError, "starts beyond"):
                provider.transcribe(self.audio, expected_duration=1.0)

    def test_old_style_test_provider_is_supported_by_adapter(self):
        class Provider:
            def transcribe(self, audio):
                return [Word("ok", 0.0, 0.2, 1.0)]

        self.assertEqual(
            [word.text for word in transcribe_audio(Provider(), self.audio, 1.0)],
            ["ok"])


class AudioSelectorTests(unittest.TestCase):
    def test_language_and_ordinal_selectors_resolve_explicit_indices(self):
        streams = inventory(audio=2)["streams"]
        self.assertEqual(media.choose_asr_stream(streams, "spa"), 2)
        self.assertEqual(media.choose_asr_stream(streams, 1), 2)

    def test_signature_tolerates_container_index_renumbering(self):
        first = [stream(0, "video"),
                 stream(4, "audio", language="eng", default=1)]
        remuxed = [stream(7, "video"),
                   stream(1, "audio", language="eng", default=1)]
        first_index, first_identity = media.asr_stream_identity(first)
        second_index, second_identity = media.asr_stream_identity(remuxed)
        self.assertNotEqual(first_index, second_index)
        self.assertEqual(first_identity, second_identity)
        self.assertNotIn("index", first_identity)

    def test_config_accepts_auto_ordinal_and_language(self):
        config = Config(deep_merge(DEFAULTS, {}), Path("unused.json"))
        for value, expected in (("auto", "auto"), (1, 1), ("2", 2),
                                ("EN_us", "en-us")):
            config.apply({"transcription": {"audio_stream": value}})
            self.assertEqual(config.get("transcription.audio_stream"), expected)
        for value in (-1, "", True, 1.5):
            with self.subTest(value=value), self.assertRaises(ValueError):
                config.apply({"transcription": {"audio_stream": value}})


class RollingShortReadTests(unittest.TestCase):
    SIGNATURE = {
        "ordinal": 0, "codec": "aac", "language": "eng",
        "channels": 2, "layout": "stereo", "default": True,
    }

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="vodpipe-short-read-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        config = deep_merge(DEFAULTS, {
            "paths": {"masters_root": str(self.tmp / "m"),
                      "work_root": str(self.tmp / "w"),
                      "censor_master_list": str(self.tmp / "none.txt")},
            "transcription": {"slice_seconds": 10,
                              "min_slice_seconds": 1,
                              "overlap_seconds": 2},
        })
        self.config = Config(config, self.tmp / "config.json")
        directory = self.tmp / "m" / "chan" / "sess"
        (directory / "master").mkdir(parents=True)
        self.master = directory / "master" / "chan_c000.mp4"
        self.master.write_bytes(b"media")
        self.store = SessionStore(self.config.masters_root)
        self.session = self.store.add(Session(
            session_id="sess", channel="chan", started_at=time.time(),
            directory=str(directory), status=COMPLETE))
        self.chunk = Chunk(
            index=0, session_id="sess", channel="chan", started_at=time.time(),
            ts_name="chan_c000.ts", master_name=self.master.name,
            duration=10.0, status=COMPLETE)
        self.store.add_chunk(self.session, self.chunk)
        self.transcriber = RollingTranscriber(
            self.config, TOOLS, self.store)
        self.provider = Mock()
        self.provider.transcribe.return_value = []
        self.transcriber.provider_override = self.provider

    @staticmethod
    def extract(tools, source, destination, start, duration, **kwargs):
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"audio")
        return destination

    def test_closed_point_six_second_tail_gap_is_error_and_blocked(self):
        with patch.object(self.transcriber, "available_seconds", return_value=9.4), \
                patch("vodpipe.transcribe.probe_asr_stream",
                      return_value=(1, self.SIGNATURE)):
            result = self.transcriber.advance(
                self.session, self.chunk, final=True)
        self.assertEqual(result.status, BLOCKED)
        self.assertEqual(self.chunk.transcript_status, ERROR)
        self.assertIn("requested 10.000s", self.chunk.transcript_error)
        self.assertIn("measured 9.400s", self.chunk.transcript_error)
        self.provider.transcribe.assert_not_called()

    def test_closed_point_six_source_with_only_point_one_readable_cannot_complete(self):
        self.chunk.duration = 0.6
        with patch.object(self.transcriber, "available_seconds", return_value=0.1), \
                patch("vodpipe.transcribe.probe_asr_stream",
                      return_value=(1, self.SIGNATURE)):
            result = self.transcriber.advance(
                self.session, self.chunk, final=True)
        self.assertEqual(result.status, BLOCKED)
        self.assertEqual(self.chunk.transcript_status, ERROR)
        self.assertEqual(self.chunk.transcribed_through, 0.0)
        self.provider.transcribe.assert_not_called()

    def test_exact_old_half_second_source_shortfall_is_not_tolerated(self):
        with patch.object(self.transcriber, "available_seconds", return_value=9.5), \
                patch("vodpipe.transcribe.probe_asr_stream",
                      return_value=(1, self.SIGNATURE)):
            result = self.transcriber.advance(
                self.session, self.chunk, final=True)
        self.assertEqual(result.status, BLOCKED)
        self.assertIn("0.500s short", result.detail)

    def test_closed_extraction_short_read_is_error_without_advancing(self):
        with patch.object(self.transcriber, "available_seconds", return_value=10.0), \
                patch("vodpipe.transcribe.probe_asr_stream",
                      return_value=(1, self.SIGNATURE)), \
                patch("vodpipe.transcribe.extract_audio_slice",
                      side_effect=self.extract), \
                patch("vodpipe.transcribe.media_duration", return_value=9.4):
            result = self.transcriber.advance(
                self.session, self.chunk, final=True)
        self.assertEqual(result.status, BLOCKED)
        self.assertEqual(self.chunk.transcribed_through, 0.0)
        self.assertIn("requested 10.000s", result.detail)
        self.assertIn("measured 9.400s", result.detail)

    def test_exact_old_half_second_extraction_shortfall_is_blocked(self):
        with patch.object(self.transcriber, "available_seconds", return_value=10.0), \
                patch("vodpipe.transcribe.probe_asr_stream",
                      return_value=(1, self.SIGNATURE)), \
                patch("vodpipe.transcribe.extract_audio_slice",
                      side_effect=self.extract), \
                patch("vodpipe.transcribe.media_duration", return_value=9.5):
            result = self.transcriber.advance(
                self.session, self.chunk, final=True)
        self.assertEqual(result.status, BLOCKED)
        self.assertEqual(self.chunk.transcribed_through, 0.0)
        self.assertIn("0.500s short", result.detail)

    def test_frame_quantisation_shortfall_completes_instead_of_blocking(self):
        """The 2026-08-16 live failure: 34 ms of disagreement lost 96 s of tail.

        A closed container's duration is its longest stream -- the video -- and
        the audio track ends a fraction of a frame earlier. The recorder's own
        figure disagrees again. None of that is missing content, but it used to
        block finalisation outright, so every chunk of an eight-hour recording
        finished incomplete with its last minute or two never sent to ASR.
        """
        self.chunk.duration = 7200.901
        # One window wide enough to reach the end, so this exercises the
        # completion arithmetic rather than the windowing.
        self.config.set("transcription.slice_seconds", 8000.0)
        with patch.object(self.transcriber, "available_seconds",
                          return_value=7200.867), \
                patch("vodpipe.transcribe.probe_asr_stream",
                      return_value=(1, self.SIGNATURE)), \
                patch("vodpipe.transcribe.extract_audio_slice",
                      side_effect=self.extract), \
                patch("vodpipe.transcribe.media_duration", return_value=7200.867):
            result = self.transcriber.advance(
                self.session, self.chunk, final=True)
        self.assertEqual(result.status, COMPLETE_)
        self.assertEqual(self.chunk.transcript_status, DONE)
        self.assertEqual(self.chunk.transcript_error, "")
        # Coverage is reported against the audio that exists, not the claim.
        self.assertAlmostEqual(result.covered_through, 7200.867, places=3)
        self.provider.transcribe.assert_called_once()

    def test_a_cursor_at_the_end_finishes_instead_of_chasing_a_sub_frame(self):
        """The 16:15 restart failure, with c001/c002's exact numbers.

        `remaining` is 7200.001 - 7200.000, which is 0.0010000000002 as a float
        and so fails a `<= 0.001` guard. The pass then asked for a 3.001s slice
        of which only 3.000s exists, made no progress, and blocked a chunk whose
        every readable sample was already transcribed.
        """
        self.chunk.duration = 7200.001
        self.chunk.transcribed_through = 7200.000
        with patch.object(self.transcriber, "available_seconds",
                          return_value=7200.001), \
                patch("vodpipe.transcribe.probe_asr_stream",
                      return_value=(1, self.SIGNATURE)), \
                patch("vodpipe.transcribe.extract_audio_slice",
                      side_effect=self.extract) as extract:
            result = self.transcriber.advance(
                self.session, self.chunk, final=True)
        self.assertEqual(result.status, COMPLETE_)
        self.assertEqual(self.chunk.transcript_status, DONE)
        self.assertEqual(self.chunk.transcript_error, "")
        extract.assert_not_called()

    def test_a_tail_one_audio_frame_short_of_the_container_completes(self):
        """c000's exact numbers: ffmpeg cannot emit that last partial frame.

        The container advertises 7200.867s but extraction stops at 7200.842s,
        and no further pass can ever close a gap the decoder cannot produce.
        """
        self.chunk.duration = 7200.901
        self.chunk.transcribed_through = 7104.933
        # The production values, so the slice really is 98.934s requested
        # against 98.909s of extractable audio.
        self.config.set("transcription.slice_seconds", 300.0)
        self.config.set("transcription.overlap_seconds", 3.0)
        with patch.object(self.transcriber, "available_seconds",
                          return_value=7200.867), \
                patch("vodpipe.transcribe.probe_asr_stream",
                      return_value=(1, self.SIGNATURE)), \
                patch("vodpipe.transcribe.extract_audio_slice",
                      side_effect=self.extract), \
                patch("vodpipe.transcribe.media_duration", return_value=98.909):
            result = self.transcriber.advance(
                self.session, self.chunk, final=True)
        self.assertEqual(result.status, COMPLETE_)
        self.assertEqual(self.chunk.transcript_status, DONE)
        self.assertAlmostEqual(result.covered_through, 7200.842, places=3)
        self.provider.transcribe.assert_called_once()

    def test_a_real_gap_still_blocks_rather_than_completing(self):
        """The tolerance must not become a licence to drop a real tail."""
        self.chunk.duration = 7200.0
        self.chunk.transcribed_through = 7195.0
        self.config.set("transcription.slice_seconds", 300.0)
        with patch.object(self.transcriber, "available_seconds",
                          return_value=7200.0), \
                patch("vodpipe.transcribe.probe_asr_stream",
                      return_value=(1, self.SIGNATURE)), \
                patch("vodpipe.transcribe.extract_audio_slice",
                      side_effect=self.extract), \
                patch("vodpipe.transcribe.media_duration", return_value=3.0):
            result = self.transcriber.advance(
                self.session, self.chunk, final=True)
        self.assertEqual(result.status, BLOCKED)
        self.assertEqual(self.chunk.transcript_status, ERROR)

    def test_one_millisecond_extraction_shortfall_is_not_a_short_read(self):
        """`-ss`/`-t` are passed rounded to the millisecond; ffprobe is not."""
        with patch.object(self.transcriber, "available_seconds", return_value=10.0), \
                patch("vodpipe.transcribe.probe_asr_stream",
                      return_value=(1, self.SIGNATURE)), \
                patch("vodpipe.transcribe.extract_audio_slice",
                      side_effect=self.extract), \
                patch("vodpipe.transcribe.media_duration", return_value=9.999):
            result = self.transcriber.advance(
                self.session, self.chunk, final=True)
        self.assertEqual(result.status, COMPLETE_)
        self.assertEqual(self.chunk.transcript_error, "")

    def test_live_short_read_is_retryable_idle_without_false_progress(self):
        self.chunk.status = RECORDING
        with patch.object(self.transcriber, "available_seconds", return_value=10.0), \
                patch("vodpipe.transcribe.probe_asr_stream",
                      return_value=(1, self.SIGNATURE)), \
                patch("vodpipe.transcribe.extract_audio_slice",
                      side_effect=self.extract), \
                patch("vodpipe.transcribe.media_duration", return_value=9.4):
            result = self.transcriber.advance(self.session, self.chunk)
        self.assertEqual(result.status, IDLE)
        self.assertEqual(self.chunk.transcribed_through, 0.0)
        self.provider.transcribe.assert_not_called()

    def test_track_change_blocks_but_index_renumber_does_not(self):
        self.config.set("transcription.slice_seconds", 5)
        changed = {**self.SIGNATURE, "language": "spa"}
        with patch.object(self.transcriber, "available_seconds", return_value=10.0), \
                patch("vodpipe.transcribe.probe_asr_stream",
                      side_effect=[(4, self.SIGNATURE), (1, self.SIGNATURE),
                                   (2, changed)]) as probe, \
                patch("vodpipe.transcribe.extract_audio_slice",
                      side_effect=self.extract), \
                patch("vodpipe.transcribe.media_duration",
                      side_effect=[5.0, 7.0]):
            first = self.transcriber.advance(self.session, self.chunk)
            self.config.set("transcription.audio_stream", 1)
            second = self.transcriber.advance(self.session, self.chunk)
            third = self.transcriber.advance(self.session, self.chunk)
        self.assertEqual(first.status, PROGRESSED)
        self.assertEqual(second.status, PROGRESSED)
        self.assertEqual(third.status, BLOCKED)
        self.assertIn("frozen ASR audio track", third.detail)
        identity = json.loads((
            self.transcriber.words_path(self.session, self.chunk)
        ).read_text(encoding="utf-8"))["asr_identity"]
        self.assertEqual(identity["audio_stream"], self.SIGNATURE)
        self.assertEqual(probe.call_args_list[1].args[2], 0)


class SnapshotAllowanceTests(unittest.TestCase):
    def test_two_second_request_does_not_get_two_seconds_of_slack(self):
        self.assertLess(allowed_snapshot_shortfall(2.0), 0.1)
        self.assertEqual(allowed_snapshot_shortfall(1000.0), 2.0)

    def test_severely_truncated_two_second_snapshot_is_quarantined(self):
        root = Path(tempfile.mkdtemp(prefix="vodpipe-snapshot-short-"))
        self.addCleanup(shutil.rmtree, root, True)
        config = Config(deep_merge(DEFAULTS, {
            "paths": {"masters_root": str(root / "m"),
                      "work_root": str(root / "w"),
                      "censor_master_list": str(root / "none.txt")},
        }), root / "config.json")
        directory = root / "m" / "chan" / "sess"
        (directory / "master").mkdir(parents=True)
        session = Session(
            session_id="sess", channel="chan", started_at=0.0,
            directory=str(directory), status=COMPLETE)
        chunk = Chunk(
            index=0, session_id="sess", channel="chan", started_at=0.0,
            master_name="chan_c000.mp4", duration=2.0, status=COMPLETE)
        (directory / "master" / chunk.master_name).write_bytes(b"m" * 2048)
        session.chunks.append(chunk)
        service = SnapshotService(config, TOOLS)

        def fake_cut(tools, parts, destination, **kwargs):
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(b"o" * 2048)

        with patch("vodpipe.snapshot.cut_and_join", side_effect=fake_cut), \
                patch("vodpipe.snapshot.validate_media_coverage",
                      return_value=0.2):
            with self.assertRaisesRegex(RuntimeError, "incomplete"):
                service.create(
                    session,
                    SnapshotRequest(
                        "sess", start=0.0, end=2.0, transcribe=False))
        self.assertEqual(len(list((directory / "snapshots").glob(
            "*.partial-shortfall.mp4"))), 1)


if __name__ == "__main__":
    unittest.main()
