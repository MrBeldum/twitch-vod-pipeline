"""Focused regressions for the confirmed media, ASR, and words-file fixes."""

from __future__ import annotations

import io
import json
import shutil
import subprocess
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from vodpipe import asr, media
from vodpipe.asr import DeepgramProvider, TranscriptionError, parse_deepgram
from vodpipe.transcript import CorruptWordsFile, load_words, words_from_json
from vodpipe.util import Tools, ffprobe_json, resolve_tools, run


TOOLS = Tools("ffmpeg", "ffprobe", "streamlink", None)


def probe_stream(index, kind, duration=None, *, default=0):
    stream = {
        "index": index,
        "codec_type": kind,
        "codec_name": "h264" if kind == "video" else "aac",
        "start_time": "0.0",
        "disposition": {"default": default},
    }
    if kind == "video":
        stream.update(width=1920, height=1080)
    elif kind == "audio":
        stream.update(channels=2, channel_layout="stereo")
    if duration is not None:
        stream["duration"] = str(duration)
    return stream


class MasterTailValidationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="vodpipe-master-tail-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.path = self.tmp / "master.mp4"
        self.path.write_bytes(b"x" * 4096)

    def validate(self, streams, expected=10.0):
        probe = {"format": {"duration": "10.0"}, "streams": streams}
        with patch.object(media, "ffprobe_json", return_value=probe):
            media.validate_master(TOOLS, self.path, expected)

    def test_container_and_audio_cannot_mask_truncated_video(self):
        with self.assertRaisesRegex(RuntimeError, "video stream.*short by"):
            self.validate([
                probe_stream(0, "video", 4.0),
                probe_stream(1, "audio", 10.0),
            ])

    def test_unknown_video_duration_cannot_authorize_source_deletion(self):
        with self.assertRaisesRegex(RuntimeError, "video stream.*duration"):
            self.validate([
                probe_stream(0, "video"),
                probe_stream(1, "audio", 10.0),
            ])

    def test_each_timed_media_stream_is_validated(self):
        with self.assertRaisesRegex(RuntimeError, "audio stream.*short by"):
            self.validate([
                probe_stream(0, "video", 10.0),
                probe_stream(1, "audio", 3.0),
            ])

    def test_complete_video_and_audio_pass(self):
        self.validate([
            probe_stream(0, "video", 10.0),
            probe_stream(1, "audio", 10.0),
        ])


class JoinTopologyTests(unittest.TestCase):
    def test_every_piece_and_the_join_map_video_and_all_audio(self):
        root = Path(tempfile.mkdtemp(prefix="vodpipe-join-map-"))
        self.addCleanup(shutil.rmtree, root, True)
        commands = []

        def fake_run(command, **kwargs):
            commands.append(list(command))
            output = Path(command[-1])
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(b"x" * 2048)
            return subprocess.CompletedProcess(command, 0, "", "")

        streams = [probe_stream(0, "video", 4.0),
                   probe_stream(1, "audio", 4.0),
                   probe_stream(2, "audio", 4.0)]
        with patch.object(media, "run", side_effect=fake_run), \
                patch.object(media, "ffprobe_json",
                             return_value={"streams": streams}):
            media.cut_and_join(
                TOOLS,
                [(root / "one.ts", 0.0, 2.0),
                 (root / "two.ts", 0.0, 2.0)],
                root / "joined.mp4",
                work_dir=root / "work",
            )

        self.assertEqual(len(commands), 3)
        for command in commands:
            self.assertIn("0:0", command)
            self.assertIn("0:1", command)
            self.assertIn("0:2", command)
            self.assertEqual(command.count("-map"), 3)

    def test_real_join_preserves_two_audio_tracks(self):
        tools = resolve_tools()
        root = Path(tempfile.mkdtemp(prefix="vodpipe-join-topology-"))
        self.addCleanup(shutil.rmtree, root, True)
        source = root / "source.ts"
        run([
            tools.ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=30",
            "-f", "lavfi", "-i", "sine=frequency=440",
            "-f", "lavfi", "-i", "sine=frequency=880",
            "-t", "4", "-map", "0:v:0", "-map", "1:a:0", "-map", "2:a:0",
            "-c:v", "libx264", "-preset", "ultrafast", "-g", "30",
            "-c:a", "aac", "-f", "mpegts", str(source),
        ], check=True, timeout=180)

        destination = root / "joined.mp4"
        media.cut_and_join(
            tools,
            [(source, 0.0, 1.5), (source, 2.0, 3.5)],
            destination,
            work_dir=root / "work",
        )
        streams = ffprobe_json(tools.ffprobe, destination)["streams"]
        self.assertEqual(sum(stream.get("codec_type") == "video"
                             for stream in streams), 1)
        self.assertEqual(sum(stream.get("codec_type") == "audio"
                             for stream in streams), 2)


class AudioSelectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="vodpipe-audio-map-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.source = self.tmp / "source.mp4"
        self.destination = self.tmp / "slice.flac"

    def extract(self, streams):
        commands = []

        def fake_run(command, **kwargs):
            commands.append(list(command))
            self.destination.write_bytes(b"flac")
            return subprocess.CompletedProcess(command, 0, "", "")

        with patch.object(media, "ffprobe_json",
                          return_value={"streams": streams}), \
                patch.object(media, "run", side_effect=fake_run):
            media.extract_audio_slice(
                TOOLS, self.source, self.destination, 2.0, 3.0)
        return commands[0]

    def test_default_audio_is_mapped_even_when_it_is_not_first(self):
        command = self.extract([
            probe_stream(1, "audio", 10.0),
            probe_stream(2, "audio", 10.0, default=1),
        ])
        self.assertEqual(command[command.index("-map") + 1], "0:2")

    def test_first_audio_is_the_deterministic_fallback(self):
        command = self.extract([
            probe_stream(4, "audio", 10.0),
            probe_stream(7, "audio", 10.0),
        ])
        self.assertEqual(command[command.index("-map") + 1], "0:4")

    def test_no_audio_fails_before_ffmpeg_runs(self):
        with patch.object(media, "ffprobe_json", return_value={"streams": [
                probe_stream(0, "video", 10.0)]}), \
                patch.object(media, "run") as run_mock:
            with self.assertRaisesRegex(RuntimeError, "no readable audio"):
                media.extract_audio_slice(
                    TOOLS, self.source, self.destination, 0.0, 1.0)
        run_mock.assert_not_called()

    def test_probe_failure_does_not_fall_back_to_implicit_selection(self):
        with patch.object(media, "ffprobe_json", return_value={}), \
                patch.object(media, "run") as run_mock:
            with self.assertRaisesRegex(RuntimeError, "could not probe"):
                media.extract_audio_slice(
                    TOOLS, self.source, self.destination, 0.0, 1.0)
        run_mock.assert_not_called()

    def test_multiple_defaults_are_a_clear_ambiguity_error(self):
        streams = [probe_stream(1, "audio", 10.0, default=1),
                   probe_stream(2, "audio", 10.0, default=1)]
        with patch.object(media, "ffprobe_json", return_value={"streams": streams}), \
                patch.object(media, "run") as run_mock:
            with self.assertRaisesRegex(RuntimeError, "multiple defaults"):
                media.extract_audio_slice(
                    TOOLS, self.source, self.destination, 0.0, 1.0)
        run_mock.assert_not_called()


def deepgram_response(words, duration=None):
    response = {
        "results": {"channels": [{"alternatives": [{"words": words}]}]},
    }
    if duration is not None:
        response["metadata"] = {"duration": duration}
    return response


def deepgram_word(text="hello", start=0.0, end=0.4, confidence=0.9):
    return {"word": text, "start": start, "end": end,
            "confidence": confidence}


class DeepgramStrictParsingTests(unittest.TestCase):
    def test_literal_empty_words_is_silence(self):
        self.assertEqual(parse_deepgram(deepgram_response([], 3.0)), [])

    def test_blank_and_malformed_entries_are_rejected(self):
        malformed = [
            deepgram_word("   "),
            {"word": "missing timing", "start": 0.0, "confidence": 0.9},
            {"word": "string timing", "start": "0", "end": 0.2,
             "confidence": 0.9},
            "not an object",
        ]
        for entry in malformed:
            with self.subTest(entry=entry), self.assertRaises(TranscriptionError):
                parse_deepgram(deepgram_response([entry]))

    def test_reversed_timings_are_rejected(self):
        words = [deepgram_word("one", 1.0, 1.4),
                 deepgram_word("two", 0.5, 0.8)]
        with self.assertRaises(TranscriptionError):
            parse_deepgram(deepgram_response(words))

    def test_overlapping_timings_are_normalised(self):
        parsed = parse_deepgram(deepgram_response([
            deepgram_word("one", 0.0, 1.0),
            deepgram_word("two", 0.9, 1.2),
        ]))
        self.assertEqual([word.text for word in parsed], ["one", "two"])
        self.assertLessEqual(parsed[0].end, parsed[1].start + 1e-6)

    def test_confidence_must_be_between_zero_and_one(self):
        for confidence in (-0.01, 1.01):
            with self.subTest(confidence=confidence), \
                    self.assertRaises(TranscriptionError):
                parse_deepgram(deepgram_response([
                    deepgram_word(confidence=confidence)]))

    def test_an_overrunning_word_end_is_clamped_not_rejected(self):
        """CORRECTED 2026-08-16 -- this used to require a TranscriptionError.

        A word's end time is an estimate and the last word of a passage
        routinely runs past the audio, while `metadata.duration` is reported
        rounded to the second. Rejecting on either failed about half of every
        rolling pass in a real eight-hour recording. The word was heard; it is
        kept, capped at the audio it was heard in.
        """
        parsed = parse_deepgram(deepgram_response([
            deepgram_word(start=1.8, end=2.1)], duration=2.0))
        self.assertEqual([word.text for word in parsed], ["hello"])
        self.assertAlmostEqual(parsed[0].end, 2.0, places=6)

    def test_a_word_starting_past_the_audio_is_still_rejected(self):
        """The response describes a longer recording than the one submitted."""
        with self.assertRaisesRegex(TranscriptionError, "starts beyond"):
            parse_deepgram(deepgram_response([
                deepgram_word(start=9.0, end=9.4)], duration=2.0))


class FakeResponse:
    def __init__(self, body):
        self.body = body
        self.sent = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read1(self, size):
        if self.sent:
            return b""
        self.sent = True
        return self.body


class DeepgramRetryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="vodpipe-deepgram-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.audio = self.tmp / "audio.flac"
        self.audio.write_bytes(b"audio")
        self.body = json.dumps(deepgram_response([])).encode("utf-8")

    @staticmethod
    def http_error(code, headers=None):
        return urllib.error.HTTPError(
            "https://example.invalid", code, "error", headers or {},
            io.BytesIO(b"failure"))

    def provider(self, **kwargs):
        return DeepgramProvider("key", max_retries=4, timeout=60.0, **kwargs)

    def test_http_400_is_attempted_once(self):
        with patch.object(asr.urllib.request, "urlopen",
                          side_effect=self.http_error(400)) as urlopen:
            with self.assertRaisesRegex(TranscriptionError, "HTTP 400"):
                self.provider().transcribe(self.audio)
        self.assertEqual(urlopen.call_count, 1)

    def test_http_500_is_retried(self):
        with patch.object(asr.urllib.request, "urlopen", side_effect=[
                self.http_error(500), FakeResponse(self.body)]) as urlopen, \
                patch.object(asr.time, "sleep") as sleep:
            self.assertEqual(self.provider().transcribe(self.audio), [])
        self.assertEqual(urlopen.call_count, 2)
        sleep.assert_called_once_with(2.0)

    def test_http_408_is_retried(self):
        with patch.object(asr.urllib.request, "urlopen", side_effect=[
                self.http_error(408), FakeResponse(self.body)]) as urlopen, \
                patch.object(asr.time, "sleep"):
            self.assertEqual(self.provider().transcribe(self.audio), [])
        self.assertEqual(urlopen.call_count, 2)

    def test_transport_error_is_retried(self):
        with patch.object(asr.urllib.request, "urlopen", side_effect=[
                urllib.error.URLError("offline"),
                FakeResponse(self.body)]) as urlopen, \
                patch.object(asr.time, "sleep"):
            self.assertEqual(self.provider().transcribe(self.audio), [])
        self.assertEqual(urlopen.call_count, 2)

    def test_malformed_success_is_not_retried(self):
        malformed = json.dumps({"results": {}}).encode("utf-8")
        with patch.object(asr.urllib.request, "urlopen",
                          return_value=FakeResponse(malformed)) as urlopen:
            with self.assertRaises(TranscriptionError):
                self.provider().transcribe(self.audio)
        self.assertEqual(urlopen.call_count, 1)

    def test_http_429_honours_retry_after(self):
        with patch.object(asr.urllib.request, "urlopen", side_effect=[
                self.http_error(429, {"Retry-After": "7"}),
                FakeResponse(self.body)]), \
                patch.object(asr.time, "sleep") as sleep:
            self.assertEqual(self.provider().transcribe(self.audio), [])
        sleep.assert_called_once_with(7.0)

    def test_response_reading_obeys_one_total_deadline(self):
        class Clock:
            now = 0.0

            def monotonic(self):
                return self.now

        class SlowResponse(FakeResponse):
            def __init__(self, clock):
                super().__init__(b"")
                self.clock = clock
                self.reads = 0

            def read1(self, size):
                self.clock.now += 0.6
                self.reads += 1
                return b"{" if self.reads < 3 else b""

        clock = Clock()
        slow = SlowResponse(clock)
        with patch.object(asr.time, "monotonic", side_effect=clock.monotonic), \
                patch.object(asr.urllib.request, "urlopen", return_value=slow) as urlopen:
            provider = DeepgramProvider(
                "key", max_retries=4, timeout=1.0)
            with self.assertRaisesRegex(TranscriptionError, "deadline"):
                provider.transcribe(self.audio)
        self.assertEqual(urlopen.call_count, 1)


class StrictWordsFileTests(unittest.TestCase):
    META = {"complete": True, "covered_seconds": 2.0,
            "expected_seconds": 2.0}
    WORD = {"text": "hello", "start": 0.0, "duration": 0.4,
            "confidence": 0.9}

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="vodpipe-strict-words-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.path = self.tmp / "words.json"

    def write(self, payload):
        self.path.write_text(json.dumps(payload), encoding="utf-8")

    def test_words_from_json_rejects_absent_non_list_and_malformed_entries(self):
        malformed = [None, {}, [None], [{}],
                     [{**self.WORD, "text": " "}],
                     [{**self.WORD, "confidence": 2.0}]]
        for payload in malformed:
            with self.subTest(payload=payload), self.assertRaises(CorruptWordsFile):
                words_from_json(payload)

    def test_load_rejects_missing_or_non_list_words(self):
        for words_value in (None, {}):
            payload = dict(self.META)
            if words_value is not None:
                payload["words"] = words_value
            self.write(payload)
            with self.subTest(payload=payload), self.assertRaises(CorruptWordsFile):
                load_words(self.path)

    def test_load_rejects_malformed_metadata(self):
        cases = [
            {"words": []},
            {"words": [], **self.META, "complete": "yes"},
            {"words": [], **self.META, "covered_seconds": "two"},
            {"words": [], **self.META, "language": " "},
        ]
        for payload in cases:
            self.write(payload)
            with self.subTest(payload=payload), self.assertRaises(CorruptWordsFile):
                load_words(self.path)

    def test_valid_empty_words_file_remains_valid_silence(self):
        self.write({"words": [], **self.META})
        self.assertEqual(load_words(self.path), ([], self.META))

    def test_adjacent_millisecond_timings_survive_float_roundoff(self):
        parsed = words_from_json([
            {**self.WORD, "start": 43.715, "duration": 0.240},
            {**self.WORD, "start": 43.955, "duration": 0.200},
        ])
        self.assertEqual(len(parsed), 2)

    def test_material_persisted_overlap_is_rejected(self):
        with self.assertRaises(CorruptWordsFile):
            words_from_json([
                {**self.WORD, "start": 1.0, "duration": 0.5},
                {**self.WORD, "start": 1.499, "duration": 0.2},
            ])

    def test_missing_file_compatibility_is_preserved(self):
        self.assertEqual(load_words(self.path), ([], {}))


if __name__ == "__main__":
    unittest.main()
