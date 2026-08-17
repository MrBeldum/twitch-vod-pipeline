"""Transcript completion honesty (AUD-001, AUD-002, AUD-018, AUD-019).

The headline defect: finalisation ran one slice and then marked the whole chunk
complete. A chunk several windows behind -- after an API outage, a late key, or
queue congestion -- was published and summarised with most of its audio missing.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from vodpipe.config import DEFAULTS, Config, deep_merge
from vodpipe.exports import GENERATION_FILES, write_exports
from vodpipe.state import Chunk, Session, SessionStore
from vodpipe.transcribe import BLOCKED, COMPLETE_, RollingTranscriber
from vodpipe.transcript import Word, merge_streams, normalise
from vodpipe.util import resolve_tools, run

CLIP_SECONDS = 30
SLICE_SECONDS = 10


class WordsPerSecondProvider:
    """Deterministic stand-in: one word per second of audio it is handed."""

    def __init__(self):
        self.calls: list[float] = []

    def transcribe(self, audio: Path):
        from vodpipe.util import media_duration
        duration = media_duration(resolve_tools().ffprobe, audio)
        self.calls.append(round(duration, 2))
        return [Word(f"w{i}", i + 0.05, 0.5, 0.9) for i in range(max(1, int(duration)))]


class SilentProvider:
    """A chunk with no speech still has to reach a terminal state."""

    def __init__(self):
        self.calls: list[float] = []

    def transcribe(self, audio: Path):
        self.calls.append(0.0)
        return []


class FailingProvider:
    def __init__(self):
        self.calls = []

    def transcribe(self, audio: Path):
        from vodpipe.asr import TranscriptionError
        self.calls.append(0.0)
        raise TranscriptionError("provider is down")


class StalledProvider:
    """Succeeds but the cursor cannot advance -- the no-progress case."""

    def __init__(self):
        self.calls = []

    def transcribe(self, audio: Path):
        self.calls.append(0.0)
        return []


class Fixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tools = resolve_tools()
        cls.media_root = Path(tempfile.mkdtemp(prefix="vodpipe-fin-media-"))
        cls.media = cls.media_root / "chunk.mp4"
        run([cls.tools.ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
             "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=30",
             "-f", "lavfi", "-i", "sine=frequency=440",
             "-t", str(CLIP_SECONDS), "-c:v", "libx264", "-preset", "ultrafast",
             "-c:a", "aac", str(cls.media)], check=True, timeout=240)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.media_root, ignore_errors=True)

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="vodpipe-fin-"))
        data = deep_merge(DEFAULTS, {
            "paths": {"masters_root": str(self.tmp / "m"),
                      "work_root": str(self.tmp / "w"),
                      "censor_master_list": str(self.tmp / "none.txt")},
            "transcription": {"slice_seconds": SLICE_SECONDS,
                              "min_slice_seconds": 5,
                              "overlap_seconds": 2},
        })
        self.config = Config(data, self.tmp / "config.json")

        self.session_dir = self.tmp / "m" / "chan" / "sess"
        (self.session_dir / "master").mkdir(parents=True)
        shutil.copy(self.media, self.session_dir / "master" / "chan_c000.mp4")

        self.store = SessionStore(self.config.masters_root)
        self.session = Session(session_id="sess", channel="chan",
                               started_at=time.time(),
                               directory=str(self.session_dir), status="complete")
        self.chunk = Chunk(index=0, session_id="sess", channel="chan",
                           started_at=time.time(), ts_name="chan_c000.ts",
                           master_name="chan_c000.mp4",
                           duration=float(CLIP_SECONDS), status="complete")
        self.session.chunks.append(self.chunk)
        self.store.add(self.session)

        self.transcriber = RollingTranscriber(self.config, self.tools, self.store)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def use(self, provider):
        self.transcriber.provider_override = provider
        return provider

    def words_meta(self):
        path = self.transcriber.words_path(self.session, self.chunk)
        return json.loads(path.read_text(encoding="utf-8"))


class FinalisationCoverageTests(Fixture):
    def test_closed_chunk_readable_duration_is_capped_to_chunk(self):
        source = self.session_dir / "master" / self.chunk.master_name
        with patch("vodpipe.transcribe.live_duration", return_value=31.5):
            available = self.transcriber.available_seconds(
                self.session, self.chunk, source)
        self.assertEqual(available, float(CLIP_SECONDS))

    def test_finalize_covers_every_slice_not_just_one(self):
        """The regression: one window used to be enough to claim completion."""
        provider = self.use(WordsPerSecondProvider())
        result = self.transcriber.finalize(self.session, self.chunk)

        self.assertEqual(result.status, COMPLETE_)
        self.assertGreaterEqual(len(provider.calls), 3,
                                f"30s at {SLICE_SECONDS}s slices needs 3+ requests")
        self.assertGreaterEqual(self.chunk.transcribed_through, CLIP_SECONDS - 1.0)

    def test_intermediate_passes_are_not_marked_complete(self):
        self.use(WordsPerSecondProvider())
        self.transcriber.advance(self.session, self.chunk)
        self.assertNotEqual(self.chunk.transcript_status, "done")
        self.assertFalse(self.words_meta()["complete"])

    def test_completed_metadata_records_expected_and_covered(self):
        self.use(WordsPerSecondProvider())
        self.transcriber.finalize(self.session, self.chunk)
        meta = self.words_meta()
        self.assertTrue(meta["complete"])
        self.assertAlmostEqual(meta["expected_seconds"], CLIP_SECONDS, delta=1.0)
        self.assertGreaterEqual(meta["covered_seconds"], CLIP_SECONDS - 1.0)

    def test_status_is_done_only_after_full_coverage(self):
        self.use(WordsPerSecondProvider())
        self.transcriber.advance(self.session, self.chunk)
        self.assertEqual(self.chunk.transcript_status, "running")
        self.transcriber.finalize(self.session, self.chunk)
        self.assertEqual(self.chunk.transcript_status, "done")

    def test_a_tiny_tail_still_finalises(self):
        """A sub-second remainder used to return without marking anything done."""
        self.use(WordsPerSecondProvider())
        self.chunk.transcribed_through = CLIP_SECONDS - 0.2
        result = self.transcriber.finalize(self.session, self.chunk)
        self.assertEqual(result.status, COMPLETE_)
        self.assertEqual(self.chunk.transcript_status, "done")

    def test_already_covered_chunk_finalises_without_new_requests(self):
        provider = self.use(WordsPerSecondProvider())
        self.chunk.transcribed_through = float(CLIP_SECONDS)
        result = self.transcriber.finalize(self.session, self.chunk)
        self.assertEqual(result.status, COMPLETE_)
        self.assertEqual(provider.calls, [])

    def test_no_speech_chunk_reaches_a_terminal_state(self):
        self.use(SilentProvider())
        result = self.transcriber.finalize(self.session, self.chunk)
        self.assertEqual(result.status, COMPLETE_)
        self.assertEqual(self.chunk.word_count, 0)
        self.assertEqual(self.chunk.transcript_status, "done")
        # Silence is still a published, explained result.
        self.assertTrue((self.session_dir / "transcripts" / "c000"
                         / "source" / "transcript.txt").exists())


class NoProgressTests(Fixture):
    def test_provider_failure_stops_instead_of_looping(self):
        """AUD-002: every failure returned 0, so the old loop never exited."""
        provider = self.use(FailingProvider())
        start = time.time()
        result = self.transcriber.finalize(self.session, self.chunk)
        elapsed = time.time() - start

        self.assertNotEqual(result.status, COMPLETE_)
        self.assertLess(elapsed, 30, "finalisation must not spin")
        self.assertLessEqual(len(provider.calls), 2)

    def test_missing_media_is_blocked_immediately(self):
        (self.session_dir / "master" / "chan_c000.mp4").unlink()
        result = self.transcriber.finalize(self.session, self.chunk)
        self.assertEqual(result.status, BLOCKED)

    def test_stalled_cursor_does_not_mark_the_chunk_done(self):
        """A pass that cannot move the cursor must not be mistaken for success."""
        self.use(StalledProvider())
        self.chunk.duration = 10_000.0        # far more than the media holds
        result = self.transcriber.finalize(self.session, self.chunk)
        self.assertNotEqual(result.status, COMPLETE_)
        self.assertNotEqual(self.chunk.transcript_status, "done")

    def test_finalisation_is_bounded_by_the_work_remaining(self):
        provider = self.use(WordsPerSecondProvider())
        self.transcriber.finalize(self.session, self.chunk)
        # 30s of media at 10s slices: a handful of calls, not an open loop.
        self.assertLess(len(provider.calls), 10)

    def test_zero_slice_seconds_is_blocked_not_infinite(self):
        self.use(WordsPerSecondProvider())
        self.config.set("transcription.slice_seconds", 0)
        result = self.transcriber.advance(self.session, self.chunk)
        self.assertEqual(result.status, BLOCKED)


class SemanticIdentityTests(Fixture):
    def test_first_slice_freezes_all_semantic_asr_settings(self):
        self.use(WordsPerSecondProvider())
        self.transcriber.advance(self.session, self.chunk)
        identity = self.words_meta()["asr_identity"]
        self.assertEqual({key: identity[key] for key in (
            "provider", "model", "language", "filler_words")}, {
            "provider": "deepgram",
            "model": "nova-3",
            "language": "en",
            "filler_words": True,
        })
        self.assertEqual(identity["audio_stream"], {
            "ordinal": 0, "codec": "aac", "language": "und",
            "channels": 1, "layout": "mono", "default": True,
        })

    def test_mid_generation_semantic_change_continues_with_frozen_settings(self):
        provider = self.use(WordsPerSecondProvider())
        self.transcriber.advance(self.session, self.chunk)
        frozen = self.words_meta()["asr_identity"]
        self.config.set("transcription.model", "changed-model")
        self.config.set("transcription.language", "fr")
        self.config.set("transcription.filler_words", False)

        result = self.transcriber.advance(self.session, self.chunk)
        self.assertNotEqual(result.status, BLOCKED)
        self.assertEqual(self.words_meta()["asr_identity"], frozen)
        self.assertEqual(len(provider.calls), 2)

    def test_transport_change_does_not_block_the_generation(self):
        provider = self.use(WordsPerSecondProvider())
        self.transcriber.advance(self.session, self.chunk)
        self.config.set("transcription.max_retries", 7)
        self.config.set("transcription.request_timeout_seconds", 321)

        result = self.transcriber.advance(self.session, self.chunk)
        self.assertNotEqual(result.status, BLOCKED)
        self.assertEqual(len(provider.calls), 2)

    def test_transport_change_rebuilds_the_cached_provider(self):
        first, second = object(), object()
        with patch("vodpipe.transcribe.build_provider",
                   side_effect=[first, second]) as build:
            self.assertIs(self.transcriber.provider(), first)
            self.config.set("transcription.max_retries", 8)
            self.assertIs(self.transcriber.provider(), second)
        self.assertEqual(build.call_count, 2)

    def test_provider_cache_constructs_once_under_concurrent_callers(self):
        entered = threading.Event()
        release = threading.Event()
        self.addCleanup(release.set)
        built = object()

        def build(config, secret, **options):
            entered.set()
            if not release.wait(5):
                raise RuntimeError("provider barrier timed out")
            return built

        results = []
        with patch("vodpipe.transcribe.build_provider", side_effect=build) as factory:
            threads = [threading.Thread(
                target=lambda: results.append(self.transcriber.provider()))
                for _ in range(8)]
            for thread in threads:
                thread.start()
            self.assertTrue(entered.wait(2))
            time.sleep(0.1)
            self.assertEqual(factory.call_count, 1)
            release.set()
            for thread in threads:
                thread.join(5)
        self.assertEqual(results, [built] * 8)
        self.assertEqual(factory.call_count, 1)

    def test_changed_settings_apply_to_the_next_chunk(self):
        self.use(WordsPerSecondProvider())
        self.transcriber.advance(self.session, self.chunk)
        self.config.set("transcription.model", "next-model")
        self.config.set("transcription.language", "fr")
        self.config.set("transcription.filler_words", False)

        second = Chunk(
            index=1, session_id="sess", channel="chan", started_at=time.time(),
            ts_name="chan_c001.ts", master_name="chan_c001.mp4",
            duration=float(CLIP_SECONDS), status="complete",
            session_offset=float(CLIP_SECONDS),
        )
        shutil.copy(self.media, self.session_dir / "master" / second.master_name)
        self.store.add_chunk(self.session, second)
        self.transcriber.advance(self.session, second)
        payload = json.loads(
            self.transcriber.words_path(self.session, second).read_text(
                encoding="utf-8"))
        self.assertEqual(payload["asr_identity"]["model"], "next-model")
        self.assertEqual(payload["asr_identity"]["language"], "fr")
        self.assertFalse(payload["asr_identity"]["filler_words"])


class OneShotCorrectnessTests(Fixture):
    def setUp(self):
        super().setUp()
        probe = patch("vodpipe.transcribe.probe_asr_stream", return_value=(1, {
            "ordinal": 0, "codec": "aac", "language": "und",
            "channels": 2, "layout": "stereo", "default": True,
        }))
        probe.start()
        self.addCleanup(probe.stop)
        self.source = self.tmp / "source.mp4"
        self.source.write_bytes(b"placeholder")
        self.output = self.tmp / "oneshot-output"

    @staticmethod
    def fake_extract(durations):
        measured = {}

        def extract(tools, source, destination, start, duration, **kwargs):
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(b"audio")
            measured[destination] = (durations.pop(0) if durations else duration)
            return destination

        def probe(ffprobe, path, allow_scan=True):
            return measured[path]

        return extract, probe

    def test_sub_second_source_is_sent_to_asr_and_published_complete(self):
        provider = self.use(SilentProvider())
        extract, probe = self.fake_extract([0.4])
        with patch("vodpipe.transcribe.live_duration", return_value=0.4), \
                patch("vodpipe.transcribe.extract_audio_slice", side_effect=extract), \
                patch("vodpipe.transcribe.media_duration", side_effect=probe):
            self.transcriber.transcribe_file(self.source, self.output)

        self.assertEqual(len(provider.calls), 1)
        meta = json.loads((self.output / "source" / "words.json").read_text(encoding="utf-8"))
        self.assertTrue(meta["complete"])
        self.assertEqual(meta["covered_seconds"], meta["expected_seconds"])

    def test_every_slice_advances_by_measured_audio(self):
        provider = self.use(SilentProvider())
        self.config.set("transcription.slice_seconds", 1)
        self.config.set("transcription.overlap_seconds", 0.2)
        extract, probe = self.fake_extract([1.0, 1.2, 0.7])
        with patch("vodpipe.transcribe.live_duration", return_value=2.5), \
                patch("vodpipe.transcribe.extract_audio_slice", side_effect=extract), \
                patch("vodpipe.transcribe.media_duration", side_effect=probe):
            self.transcriber.transcribe_file(self.source, self.output)

        self.assertEqual(len(provider.calls), 3)
        meta = json.loads((self.output / "source" / "words.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["covered_seconds"], 2.5)
        self.assertEqual(meta["expected_seconds"], 2.5)

    def test_significant_short_read_fails_without_publishing(self):
        write_exports(self.output, [Word("old", 0.0, 0.4, 0.9)])
        before = {name: ((self.output / name).read_bytes()
                         if (self.output / name).exists() else None)
                  for name in GENERATION_FILES}
        extract, probe = self.fake_extract([1.0])
        with patch("vodpipe.transcribe.live_duration", return_value=3.0), \
                patch("vodpipe.transcribe.extract_audio_slice", side_effect=extract), \
                patch("vodpipe.transcribe.media_duration", side_effect=probe):
            with self.assertRaisesRegex(RuntimeError, "short audio read"):
                self.transcriber.transcribe_file(self.source, self.output)
        after = {name: ((self.output / name).read_bytes()
                        if (self.output / name).exists() else None)
                 for name in GENERATION_FILES}
        self.assertEqual(after, before)

    def test_overlap_only_read_is_rejected_as_no_progress(self):
        self.use(SilentProvider())
        self.config.set("transcription.slice_seconds", 1)
        self.config.set("transcription.overlap_seconds", 0.2)
        extract, probe = self.fake_extract([1.0, 0.2])
        with patch("vodpipe.transcribe.live_duration", return_value=1.4), \
                patch("vodpipe.transcribe.extract_audio_slice", side_effect=extract), \
                patch("vodpipe.transcribe.media_duration", side_effect=probe):
            with self.assertRaisesRegex(RuntimeError, "short audio read"):
                self.transcriber.transcribe_file(self.source, self.output)
        self.assertFalse((self.output / "source" / "words.json").exists())

    def test_zero_duration_and_no_audio_are_rejected(self):
        with patch("vodpipe.transcribe.live_duration", return_value=0.0):
            with self.assertRaisesRegex(RuntimeError, "zero duration"):
                self.transcriber.transcribe_file(self.source, self.output)

        with patch("vodpipe.transcribe.live_duration", return_value=1.0), \
                patch("vodpipe.transcribe.extract_audio_slice",
                      side_effect=RuntimeError("has no readable audio stream")):
            with self.assertRaisesRegex(RuntimeError, "no readable audio"):
                self.transcriber.transcribe_file(self.source, self.output)
        self.assertFalse((self.output / "source" / "words.json").exists())

    def test_explicit_language_reaches_provider_words_and_exports(self):
        fake_provider = MagicMock()
        fake_provider.transcribe.return_value = [Word("bonjour", 0.0, 0.3, 0.9)]
        extract, probe = self.fake_extract([0.4])
        with patch("vodpipe.transcribe.live_duration", return_value=0.4), \
                patch("vodpipe.transcribe.extract_audio_slice", side_effect=extract), \
                patch("vodpipe.transcribe.media_duration", side_effect=probe), \
                patch("vodpipe.transcribe.DeepgramProvider",
                      return_value=fake_provider) as provider_class:
            self.transcriber.transcribe_file(
                self.source, self.output, language="fr")

        self.assertEqual(provider_class.call_args.kwargs["language"], "fr")
        words = json.loads((self.output / "source" / "words.json").read_text(encoding="utf-8"))
        premiere = json.loads(
            (self.output / "premiere.json").read_text(encoding="utf-8"))
        self.assertEqual(words["language"], "fr")
        self.assertEqual(words["asr_identity"]["language"], "fr")
        self.assertEqual(premiere["language"], "fr-fr")

class SeamPreservationTests(unittest.TestCase):
    """AUD-018: a re-transcription that omits a word must not delete it."""

    @staticmethod
    def w(text, start, duration=0.4):
        return Word(text, start, duration, 0.9)

    def test_word_absent_from_the_new_slice_survives(self):
        existing = [self.w("keep", 7.0), self.w("lost", 8.5)]
        incoming = [self.w("new", 10.0)]
        merged = merge_streams(existing, incoming, overlap_start=8.0)
        self.assertEqual([word.text for word in merged], ["keep", "lost", "new"])

    def test_re_transcribed_word_is_not_duplicated(self):
        existing = [self.w("keep", 7.0), self.w("same", 8.5)]
        incoming = [self.w("same", 8.5), self.w("new", 10.0)]
        merged = merge_streams(existing, incoming, overlap_start=8.0)
        self.assertEqual([word.text for word in merged], ["keep", "same", "new"])

    def test_substitution_prefers_the_newer_reading(self):
        existing = [self.w("hello", 0.0, 0.5), self.w("wor", 1.0, 0.2)]
        incoming = [self.w("world", 1.0, 0.6), self.w("again", 2.0)]
        merged = merge_streams(existing, incoming, overlap_start=0.9)
        self.assertEqual([word.text for word in merged], ["hello", "world", "again"])

    def test_empty_incoming_overlap_keeps_everything(self):
        existing = [self.w("a", 0.0), self.w("b", 1.0)]
        merged = merge_streams(existing, [], overlap_start=0.5)
        self.assertEqual([word.text for word in merged], ["a", "b"])

    def test_result_is_always_ordered_and_non_overlapping(self):
        existing = [self.w("a", 0.0), self.w("b", 1.0), self.w("c", 2.0)]
        incoming = [self.w("c2", 2.0), self.w("d", 3.0)]
        merged = merge_streams(existing, incoming, overlap_start=1.5)
        for previous, following in zip(merged, merged[1:]):
            self.assertLessEqual(previous.end, following.start + 1e-6)


class NormalisationTests(unittest.TestCase):
    """AUD-019: the de-overlap contract must actually hold."""

    def test_equal_start_words_do_not_both_survive(self):
        result = normalise([Word("a", 1.0, 0.5, 0.7), Word("b", 1.0, 0.5, 0.9)])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].text, "b", "higher confidence should win")

    def test_near_equal_starts_are_resolved_too(self):
        # A gap smaller than the minimum word length leaves no room for both.
        result = normalise([Word("a", 1.0, 0.5, 0.9), Word("b", 1.001, 0.5, 0.5)])
        self.assertEqual(len(result), 1)

    def test_adjacent_pairs_never_overlap(self):
        messy = [
            Word("a", 0.0, 5.0, 0.9), Word("b", 1.0, 0.5, 0.9),
            Word("c", 1.0, 2.0, 0.8), Word("d", 1.2, 0.1, 0.9),
            Word("e", 9.0, 0.0, 0.9),
        ]
        result = normalise(messy)
        for previous, following in zip(result, result[1:]):
            self.assertLessEqual(previous.end, following.start + 1e-6,
                                 f"{previous.text} overlaps {following.text}")

    def test_every_word_keeps_a_positive_duration(self):
        for word in normalise([Word("a", 0.0, 0.0, 1.0), Word("b", 0.5, -1.0, 1.0)]):
            self.assertGreater(word.duration, 0.0)


if __name__ == "__main__":
    unittest.main()
