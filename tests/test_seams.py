"""Words spoken across a chunk boundary (AUD-013).

Rolling slices overlap, so a word crossing a *slice* seam is heard whole by one of
them. A word crossing a *chunk* boundary is not: the two chunks are separate files
transcribed independently, and each hears only its half -- "world" comes out as
"wor" at the end of one and "ld" at the start of the next, or disappears from
both.

The repair transcribes a short passage built from the tail of one file and the
head of the next, then hands each word to whichever chunk it was mostly spoken in.
These tests cover the ownership rule, the refusal to act on a bad seam pass, and
the whole thing end to end against real media.
"""

from __future__ import annotations

import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from vodpipe.config import DEFAULTS, Config, deep_merge
from vodpipe.exports import GENERATION_FILES
from vodpipe.state import Chunk, Session, SessionStore
from vodpipe.transcribe import RollingTranscriber
from vodpipe.transcript import Word, load_words, save_words, stitch_seam
from vodpipe.util import resolve_tools, run

CHUNK_SECONDS = 10
SEAM_SECONDS = 6.0


def w(text, start, duration=0.4):
    return Word(text, start, duration, 0.9)


class StitchSeamTests(unittest.TestCase):
    """The ownership rule, in isolation."""

    def stitch(self, previous, following, seam, *, seam_start=4.0,
               pivot=6.0, following_lead=6.0):
        # seam_start + pivot = 10.0, the previous chunk's length.
        return stitch_seam(previous, following, seam, seam_start=seam_start,
                           pivot=pivot, following_lead=following_lead)

    def test_a_straddling_word_lands_in_exactly_one_transcript(self):
        # "world" begins just before the join and ends just after it: most of it
        # was spoken in the following chunk, so that is where it belongs.
        previous = [w("hello", 5.0), w("wor", 9.8, 0.2)]
        following = [w("ld", 0.0, 0.2), w("again", 7.0)]
        seam = [w("hello", 1.0), w("world", 5.8, 0.5)]

        new_previous, new_following = self.stitch(previous, following, seam)

        self.assertEqual([item.text for item in new_previous], ["hello"])
        self.assertEqual([item.text for item in new_following], ["world", "again"])
        # Once, and only once.
        everything = [item.text for item in new_previous + new_following]
        self.assertEqual(everything.count("world"), 1)
        self.assertNotIn("wor", everything)
        self.assertNotIn("ld", everything)

    def test_a_word_mostly_in_the_previous_chunk_stays_there(self):
        previous = [w("say", 5.0), w("hel", 9.7, 0.3)]
        following = [w("lo", 0.0, 0.1), w("there", 7.0)]
        # Midpoint at 5.4, before the pivot of 6.0.
        seam = [w("say", 1.0), w("hello", 5.0, 0.8)]

        new_previous, new_following = self.stitch(previous, following, seam)

        self.assertEqual([item.text for item in new_previous], ["say", "hello"])
        self.assertEqual([item.text for item in new_following], ["there"])

    def test_seam_words_are_placed_on_the_session_timeline_correctly(self):
        previous = [w("early", 1.0)]
        following = [w("later", 8.0)]
        seam = [w("tail", 2.0), w("head", 7.0)]

        new_previous, new_following = self.stitch(previous, following, seam)

        # The seam audio starts at previous_duration - pivot = 4.0.
        tail = next(item for item in new_previous if item.text == "tail")
        self.assertAlmostEqual(tail.start, 6.0, places=3)
        # And the following chunk's own clock restarts at the pivot.
        head = next(item for item in new_following if item.text == "head")
        self.assertAlmostEqual(head.start, 1.0, places=3)

    def test_a_short_tail_slice_does_not_shift_the_seam_words(self):
        """The offset comes from where the slice started, not from the duration.

        If the tail slice comes back shorter than was asked for -- a chunk whose
        media is slightly shorter than its recorded duration -- deriving the
        offset from the duration instead would move every seam word.
        """
        previous = [w("early", 1.0)]
        following = [w("later", 8.0)]
        seam = [w("tail", 1.0), w("head", 6.0)]

        # Asked for 6s from 4.0, but only 5s of audio was there.
        new_previous, new_following = self.stitch(
            previous, following, seam, seam_start=4.0, pivot=5.0)

        tail = next(item for item in new_previous if item.text == "tail")
        self.assertAlmostEqual(tail.start, 5.0, places=3)
        head = next(item for item in new_following if item.text == "head")
        self.assertAlmostEqual(head.start, 1.0, places=3)

    def test_an_empty_seam_pass_changes_nothing(self):
        """ASR is not deterministic; silence from it is not evidence of silence."""
        previous = [w("keep", 9.5)]
        following = [w("these", 0.2)]
        new_previous, new_following = self.stitch(previous, following, [])
        self.assertEqual([item.text for item in new_previous], ["keep"])
        self.assertEqual([item.text for item in new_following], ["these"])

    def test_a_one_sided_seam_only_rewrites_that_side(self):
        previous = [w("original", 9.5)]
        following = [w("untouched", 0.5)]
        seam = [w("tail", 1.0)]          # nothing after the pivot

        new_previous, new_following = self.stitch(previous, following, seam)

        self.assertEqual([item.text for item in new_previous], ["tail"])
        self.assertEqual([item.text for item in new_following], ["untouched"])

    def test_words_outside_the_seam_region_are_left_alone(self):
        previous = [w("far", 1.0), w("back", 2.0), w("edge", 9.5)]
        following = [w("edge2", 0.5), w("far", 8.0), w("ahead", 9.0)]
        seam = [w("A", 1.0), w("B", 7.0)]

        new_previous, new_following = self.stitch(previous, following, seam)

        self.assertEqual([item.text for item in new_previous][:2], ["far", "back"])
        self.assertEqual([item.text for item in new_following][-2:], ["far", "ahead"])

    def test_stitching_twice_gives_the_same_answer(self):
        """Idempotence is what makes retries and recovery safe."""
        previous = [w("hello", 5.0), w("wor", 9.8, 0.2)]
        following = [w("ld", 0.0, 0.2), w("again", 7.0)]
        seam = [w("hello", 1.0), w("world", 5.8, 0.5)]

        once_previous, once_following = self.stitch(previous, following, seam)
        twice_previous, twice_following = self.stitch(once_previous, once_following,
                                                      seam)

        self.assertEqual([item.text for item in once_previous],
                         [item.text for item in twice_previous])
        self.assertEqual([item.text for item in once_following],
                         [item.text for item in twice_following])

    def test_the_result_stays_ordered_and_non_overlapping(self):
        previous = [w("a", 3.0), w("b", 9.0, 1.0)]
        following = [w("c", 0.0, 2.0), w("d", 9.0)]
        seam = [w("x", 1.0, 3.0), w("y", 5.5, 2.0), w("z", 8.0)]

        for stream in self.stitch(previous, following, seam):
            for first, second in zip(stream, stream[1:]):
                self.assertLessEqual(first.end, second.start + 1e-6)
                self.assertGreater(second.duration, 0.0)

    def test_same_word_with_modest_timing_drift_is_replaced_once(self):
        previous = [w("steady", 5.0, 0.4), w("edge", 9.7, 0.3)]
        following = [w("keep", 2.0)]
        seam = [w("steady", 1.55, 0.4), w("fixed", 5.8, 0.5)]

        new_previous, _ = self.stitch(previous, following, seam)
        self.assertEqual([item.text for item in new_previous], ["steady"])
        self.assertAlmostEqual(new_previous[0].start, 5.55, places=3)

    def test_temporal_overlap_does_not_delete_an_unrelated_word(self):
        previous = [w("content", 5.0, 0.6), w("edge", 9.7, 0.3)]
        following = [w("keep", 2.0)]
        seam = [w("different", 1.2, 0.6), w("fixed", 5.8, 0.5)]

        new_previous, _ = self.stitch(previous, following, seam)
        self.assertIn("content", [item.text for item in new_previous])
        self.assertNotIn("different", [item.text for item in new_previous])

    def test_straddling_span_is_clamped_to_the_owner_media(self):
        previous = [w("fragment", 9.8, 0.2)]
        following = [w("fragment2", 0.0, 0.2)]
        seam = [w("whole", 5.8, 0.6)]

        _, new_following = self.stitch(previous, following, seam,
                                       following_lead=0.25)
        self.assertEqual([item.text for item in new_following], ["whole"])
        self.assertGreaterEqual(new_following[0].start, 0.0)
        self.assertLessEqual(new_following[0].end, 0.25)


class BoundaryIntegrationTests(unittest.TestCase):
    """The whole pass against real media: cut, join, transcribe, republish."""

    @classmethod
    def setUpClass(cls):
        cls.tools = resolve_tools()
        cls.media_root = Path(tempfile.mkdtemp(prefix="vodpipe-seam-media-"))
        cls.media = cls.media_root / "chunk.mp4"
        run([cls.tools.ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
             "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=30",
             "-f", "lavfi", "-i", "sine=frequency=440",
             "-t", str(CHUNK_SECONDS), "-c:v", "libx264", "-preset", "ultrafast",
             "-c:a", "aac", str(cls.media)], check=True, timeout=240)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.media_root, ignore_errors=True)

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="vodpipe-seam-"))
        self.config = Config(deep_merge(DEFAULTS, {
            "paths": {"masters_root": str(self.tmp / "m"),
                      "work_root": str(self.tmp / "w"),
                      "censor_master_list": str(self.tmp / "none.txt")},
            "transcription": {"seam_seconds": SEAM_SECONDS},
        }), self.tmp / "config.json")

        self.session_dir = self.tmp / "m" / "chan" / "sess"
        (self.session_dir / "master").mkdir(parents=True)
        self.store = SessionStore(self.config.masters_root)
        self.session = Session(session_id="sess", channel="chan",
                               started_at=time.time(),
                               directory=str(self.session_dir), status="complete")

        for index in range(2):
            label = f"c{index:03d}"
            shutil.copy(self.media, self.session_dir / "master" / f"chan_{label}.mp4")
            self.session.chunks.append(Chunk(
                index=index, session_id="sess", channel="chan",
                started_at=time.time(), ts_name=f"chan_{label}.ts",
                master_name=f"chan_{label}.mp4", duration=float(CHUNK_SECONDS),
                session_offset=float(index * CHUNK_SECONDS), status="complete"))
        self.store.add(self.session)

        self.transcriber = RollingTranscriber(self.config, self.tools, self.store)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write_words(self, chunk, words, *, complete=True):
        save_words(self.transcriber.words_path(self.session, chunk), words, {
            "channel": "chan", "session_id": "sess", "chunk": chunk.label,
            "session_offset": chunk.session_offset, "language": "en",
            "covered_seconds": float(CHUNK_SECONDS),
            "expected_seconds": float(CHUNK_SECONDS),
            "complete": complete,
        })

    def words_for(self, chunk):
        words, _ = load_words(self.transcriber.words_path(self.session, chunk))
        return [item.text for item in words]

    def use(self, seam_words):
        class SeamProvider:
            def __init__(self, words):
                self.words = words
                self.calls = 0

            def transcribe(self, audio: Path):
                self.calls += 1
                return list(self.words)

        provider = SeamProvider(seam_words)
        self.transcriber.provider_override = provider
        return provider

    def test_a_clipped_boundary_word_is_repaired_in_both_transcripts(self):
        previous, following = self.session.chunks
        self.write_words(previous, [w("hello", 5.0), w("wor", 9.7, 0.3)])
        self.write_words(following, [w("ld", 0.0, 0.2), w("keep", 8.0)])

        provider = self.use([w("hello", 1.0), w("world", 5.8, 0.5)])
        changed = self.transcriber.stitch_with_previous(self.session, following)

        self.assertTrue(changed)
        self.assertEqual(provider.calls, 1, "one extra request per boundary")
        self.assertEqual(self.words_for(previous), ["hello"])
        self.assertEqual(self.words_for(following), ["world", "keep"])

    def test_the_repaired_transcripts_are_republished_for_premiere(self):
        previous, following = self.session.chunks
        self.write_words(previous, [w("hello", 5.0), w("wor", 9.7, 0.3)])
        self.write_words(following, [w("ld", 0.0, 0.2), w("keep", 8.0)])

        self.use([w("hello", 1.0), w("world", 5.8, 0.5)])
        self.transcriber.stitch_with_previous(self.session, following)

        for chunk, expected in ((previous, "hello"), (following, "world")):
            published = (self.session_dir / "transcripts" / chunk.label
                         / "premiere.json").read_text(encoding="utf-8")
            self.assertIn(expected, published)
        self.assertNotIn(
            "wor\"",
            (self.session_dir / "transcripts" / previous.label
             / "premiere.json").read_text(encoding="utf-8"))

    def test_the_first_chunk_has_no_boundary_to_repair(self):
        first = self.session.chunks[0]
        self.write_words(first, [w("alone", 1.0)])
        provider = self.use([w("anything", 1.0)])
        self.assertFalse(self.transcriber.stitch_with_previous(self.session, first))
        self.assertEqual(provider.calls, 0)

    def test_an_incomplete_transcript_is_left_for_its_own_pass_to_finish(self):
        previous, following = self.session.chunks
        self.write_words(previous, [w("hello", 5.0)])
        self.write_words(following, [w("partial", 0.5)], complete=False)
        provider = self.use([w("x", 1.0)])
        self.assertFalse(
            self.transcriber.stitch_with_previous(self.session, following))
        self.assertEqual(provider.calls, 0)

    def test_incomplete_neighbour_is_not_applicable_even_under_strict(self):
        # P7: an incomplete neighbour means "not yet applicable", never a failure.
        # A manual retranscription (strict=True) of one chunk must not report
        # itself failed just because the adjacent chunk is still transcribing.
        previous, following = self.session.chunks
        self.write_words(previous, [w("hello", 5.0)], complete=False)
        self.write_words(following, [w("there", 0.5)])
        provider = self.use([w("x", 1.0)])
        self.assertFalse(
            self.transcriber.stitch_with_previous(
                self.session, following, strict=True))
        self.assertEqual(provider.calls, 0)

    def test_the_feature_can_be_switched_off(self):
        previous, following = self.session.chunks
        self.write_words(previous, [w("hello", 5.0)])
        self.write_words(following, [w("there", 0.5)])
        provider = self.use([w("x", 1.0)])
        self.config.set("transcription.stitch_chunk_boundaries", False)
        self.assertFalse(
            self.transcriber.stitch_with_previous(self.session, following))
        self.assertEqual(provider.calls, 0)

    def test_a_failing_seam_transcription_leaves_both_transcripts_intact(self):
        from vodpipe.asr import TranscriptionError

        previous, following = self.session.chunks
        self.write_words(previous, [w("hello", 5.0), w("wor", 9.7, 0.3)])
        self.write_words(following, [w("ld", 0.0, 0.2), w("keep", 8.0)])

        class Broken:
            def transcribe(self, audio: Path):
                raise TranscriptionError("provider is down")

        self.transcriber.provider_override = Broken()
        self.assertFalse(
            self.transcriber.stitch_with_previous(self.session, following))
        self.assertEqual(self.words_for(previous), ["hello", "wor"])
        self.assertEqual(self.words_for(following), ["ld", "keep"])

    def test_missing_media_for_one_side_is_a_no_op(self):
        previous, following = self.session.chunks
        self.write_words(previous, [w("hello", 5.0)])
        self.write_words(following, [w("there", 0.5)])
        (self.session_dir / "master" / previous.master_name).unlink()

        provider = self.use([w("x", 1.0)])
        self.assertFalse(
            self.transcriber.stitch_with_previous(self.session, following))
        self.assertEqual(provider.calls, 0)

    def test_running_it_twice_does_not_change_the_second_answer(self):
        previous, following = self.session.chunks
        self.write_words(previous, [w("hello", 5.0), w("wor", 9.7, 0.3)])
        self.write_words(following, [w("ld", 0.0, 0.2), w("keep", 8.0)])

        self.use([w("hello", 1.0), w("world", 5.8, 0.5)])
        self.transcriber.stitch_with_previous(self.session, following)
        first = (self.words_for(previous), self.words_for(following))

        self.transcriber.stitch_with_previous(self.session, following)
        self.assertEqual((self.words_for(previous), self.words_for(following)),
                         first)

    def test_duration_and_confidence_changes_trigger_republication(self):
        previous, following = self.session.chunks
        self.write_words(previous, [w("hello", 5.0), w("wor", 9.7, 0.3)])
        self.write_words(following, [w("ld", 0.0, 0.2), w("keep", 8.0)])

        revised = Word("hello", 1.0, 0.65, 0.42)
        self.use([revised, w("world", 5.8, 0.5)])
        self.assertTrue(
            self.transcriber.stitch_with_previous(self.session, following))
        words, _ = load_words(self.transcriber.words_path(self.session, previous))
        hello = next(item for item in words if item.text == "hello")
        self.assertAlmostEqual(hello.duration, 0.65, places=3)
        self.assertAlmostEqual(hello.confidence, 0.42, places=3)

    def test_failure_publishing_second_chunk_restores_both_generations(self):
        previous, following = self.session.chunks
        self.write_words(previous, [w("hello", 5.0), w("wor", 9.7, 0.3)])
        self.write_words(following, [w("ld", 0.0, 0.2), w("keep", 8.0)])
        self.transcriber.republish(self.session, previous)
        self.transcriber.republish(self.session, following)

        def snapshot(target):
            directory = self.transcriber.output_dir(self.session, target)
            return {name: ((directory / name).read_bytes()
                           if (directory / name).exists() else None)
                    for name in GENERATION_FILES}

        before_previous = snapshot(previous)
        before_following = snapshot(following)
        self.use([w("hello", 1.0), w("world", 5.8, 0.5)])

        from vodpipe import transcript
        original = transcript._replace_published_file

        def fail_on_following(staged, target):
            if target.parent.name == following.label:
                raise OSError("injected second-generation failure")
            return original(staged, target)

        with patch("vodpipe.transcript._replace_published_file",
                   side_effect=fail_on_following):
            self.assertFalse(
                self.transcriber.stitch_with_previous(self.session, following))

        self.assertEqual(snapshot(previous), before_previous)
        self.assertEqual(snapshot(following), before_following)

    def test_tail_and_head_are_both_measured_and_head_bounds_are_used(self):
        previous, following = self.session.chunks
        self.write_words(previous, [w("edge", 9.7, 0.3)])
        self.write_words(following, [w("keep", 8.0)])
        self.use([w("tail", 1.0), w("head", 6.8, 1.0)])

        with patch("vodpipe.transcribe.media_duration",
                   side_effect=[5.0, 2.0, 7.0]) as measured:
            self.assertTrue(
                self.transcriber.stitch_with_previous(self.session, following))

        self.assertEqual(measured.call_count, 3)
        words, _ = load_words(self.transcriber.words_path(self.session, following))
        head = next(item for item in words if item.text == "head")
        self.assertLessEqual(head.end, 2.0)


class PipelineWiringTests(unittest.TestCase):
    """The boundary pass has to actually be reached when a chunk closes."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="vodpipe-wire-"))
        self.config = Config(deep_merge(DEFAULTS, {
            "paths": {"masters_root": str(self.tmp / "m"),
                      "work_root": str(self.tmp / "w"),
                      "censor_master_list": str(self.tmp / "none.txt")},
            "watcher": {"enabled": False},
            "summary": {"provider": "none"},
            "secrets": {"deepgram_api_key": "test-key"},
        }), self.tmp / "config.json")
        self.config.masters_root.mkdir(parents=True, exist_ok=True)

        from vodpipe.pipeline import Pipeline
        self.pipeline = Pipeline(self.config)
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.addCleanup(lambda: [pool.stop(timeout=10, drain=False)
                                 for pool in self.pipeline.pools])

        session_dir = self.tmp / "m" / "chan" / "sess"
        session_dir.mkdir(parents=True)
        self.session = Session(session_id="sess", channel="chan",
                               started_at=time.time(),
                               directory=str(session_dir), status="complete")
        for index in range(2):
            self.session.chunks.append(Chunk(
                index=index, session_id="sess", channel="chan",
                started_at=time.time(), master_name=f"chan_c{index:03d}.mp4",
                duration=10.0, session_offset=index * 10.0, status="complete"))
        self.pipeline.store.add(self.session)

        self.seen: list[str] = []
        self.pipeline.transcriber.stitch_with_previous = (
            lambda session, chunk: self.seen.append(chunk.label) or True)

    def test_closing_a_later_chunk_repairs_its_boundary(self):
        self.pipeline._stitch_boundary(self.session, self.session.chunks[1])
        self.assertEqual(self.seen, ["c001"])

    def test_the_first_chunk_has_no_previous_chunk(self):
        self.pipeline._stitch_boundary(self.session, self.session.chunks[0])
        self.assertEqual(self.seen, [])

    def test_no_key_means_no_boundary_request(self):
        self.config.set("secrets.deepgram_api_key", "")
        self.pipeline._stitch_boundary(self.session, self.session.chunks[1])
        self.assertEqual(self.seen, [])

    def test_transcription_off_means_no_boundary_request(self):
        self.config.set("transcription.enabled", False)
        self.pipeline._stitch_boundary(self.session, self.session.chunks[1])
        self.assertEqual(self.seen, [])

    def test_a_failing_stitch_never_fails_the_chunk(self):
        def explode(session, chunk):
            raise RuntimeError("ffmpeg fell over")

        self.pipeline.transcriber.stitch_with_previous = explode
        # No exception: the transcripts are already published and usable.
        self.pipeline._stitch_boundary(self.session, self.session.chunks[1])

    def test_boundary_waits_for_the_previous_chunks_kernel_mutation_lock(self):
        from vodpipe.locks import ResourceLock, chunk_lock_path

        previous, following = self.session.chunks
        held = ResourceLock(
            chunk_lock_path(self.session.path, previous.label)).acquire()
        thread = threading.Thread(
            target=self.pipeline._stitch_boundary,
            args=(self.session, following),
        )
        try:
            thread.start()
            time.sleep(0.2)
            self.assertTrue(thread.is_alive())
            self.assertEqual(self.seen, [])
        finally:
            held.release()
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(self.seen, [following.label])


if __name__ == "__main__":
    unittest.main()
