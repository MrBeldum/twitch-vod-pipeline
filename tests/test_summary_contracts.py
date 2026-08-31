"""Structured rundown eligibility, generation binding, and seam regeneration."""

from __future__ import annotations

import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from vodpipe.config import DEFAULTS, Config, deep_merge
from vodpipe.exports import write_exports
from vodpipe.pipeline import Pipeline
from vodpipe.state import DONE, ERROR, PENDING, RUNNING, Chunk, Session
from vodpipe.summarize import (
    build_model_input,
    build_header,
    rundown_generation,
    write_rundown,
)
from vodpipe.transcribe import BLOCKED, COMPLETE_, AdvanceResult
from vodpipe.transcript import Word, load_words


class SummaryFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="vodpipe-summary-contract-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.config = Config(deep_merge(DEFAULTS, {
            "paths": {
                "masters_root": str(self.tmp / "masters"),
                "work_root": str(self.tmp / "work"),
                "censor_master_list": str(self.tmp / "none.txt"),
            },
            "recording": {"free_space_floor_gb": 0, "hard_reserve_gb": 0},
            "proxies": {"enabled": False},
            "transcription": {
                "enabled": True,
                "stitch_chunk_boundaries": True,
            },
            "summary": {
                "enabled": True,
                "provider": "claude-cli",
                "min_words": 1,
            },
            "secrets": {"deepgram_api_key": "test-key"},
            "watcher": {"enabled": False},
        }), self.tmp / "config.json")
        self.config.masters_root.mkdir(parents=True)
        self.pipeline = Pipeline(self.config)
        self.addCleanup(
            lambda: [pool.stop(timeout=10, drain=False)
                     for pool in self.pipeline.pools])
        self.directory = self.config.masters_root / "chan" / "sess"
        (self.directory / "master").mkdir(parents=True)
        self.session = self.pipeline.store.add(Session(
            session_id="sess", channel="chan", started_at=1000.0,
            directory=str(self.directory), status="complete"))

    def chunk(self, index: int, *, offset: float | None = None) -> Chunk:
        chunk = Chunk(
            index=index, session_id="sess", channel="chan", started_at=1000.0,
            master_name=f"chan_c{index:03d}.mp4", duration=10.0,
            session_offset=float(index * 10 if offset is None else offset),
            status="complete", transcript_status=DONE,
        )
        (self.directory / "master" / chunk.master_name).write_bytes(b"media")
        self.pipeline.store.add_chunk(self.session, chunk)
        return chunk

    def publish(self, chunk: Chunk, texts: list[str], *, complete: bool = True,
                starts: list[float] | None = None) -> str:
        starts = starts or [float(index) for index in range(len(texts))]
        words = [Word(text, start, 0.5, 0.9)
                 for text, start in zip(texts, starts)]
        output = self.pipeline.transcriber.output_dir(self.session, chunk)
        write_exports(
            output, words,
            meta={
                "channel": self.session.channel,
                "session_id": self.session.session_id,
                "chunk": chunk.label,
                "session_offset": chunk.session_offset,
                "complete": complete,
                "source": chunk.master_name,
            },
            words_meta={
                "channel": self.session.channel,
                "session_id": self.session.session_id,
                "chunk": chunk.label,
                "session_offset": chunk.session_offset,
                "language": "en",
                "complete": complete,
                "covered_seconds": chunk.duration if complete else 5.0,
                "expected_seconds": chunk.duration,
            },
        )
        self.pipeline.store.update_chunk(
            self.session, chunk,
            transcript_status=DONE if complete else RUNNING,
            word_count=len(words),
            transcribed_through=chunk.duration if complete else 5.0,
        )
        _, meta = load_words(output / "source" / "words.json")
        return str(meta["generation"])

    def rundown(self, chunk: Chunk, generation: str, body: str = "# Rundown") -> Path:
        output = self.pipeline.transcriber.output_dir(self.session, chunk)
        path = output / "report.md"
        header = build_header(
            self.session.channel, self.session.session_id, chunk.label,
            chunk.session_offset, chunk.duration, self.session.started_at)
        write_rundown(path, body, header, generation)
        return path

    @staticmethod
    def wait(job, timeout: float = 10.0):
        deadline = time.time() + timeout
        while job.status in ("queued", "running") and time.time() < deadline:
            time.sleep(0.02)
        return job


class StructuredInputTests(unittest.TestCase):
    def test_every_model_line_has_a_range_and_uses_the_segment_end(self):
        words = [
            Word("first", 1.2, 1.4, 0.9),
            Word("second", 8.0, 2.2, 0.9),
        ]

        lines = build_model_input(words, 3600.0).splitlines()

        self.assertEqual(lines, [
            "[01:00:01-01:00:02] first",
            "[01:00:08-01:00:10] second",
        ])
        for line in lines:
            self.assertRegex(
                line, r"^\[\d{2}:\d{2}:\d{2}-\d{2}:\d{2}:\d{2}\] .+")


class EligibilityTests(SummaryFixture):
    def test_status_metadata_no_speech_and_structured_count_are_all_required(self):
        # An installed engine is a property of the machine; the eligibility
        # reasons under test are only reached once capability passes.
        capable = patch.object(self.pipeline, "_summary_capability",
                               return_value=(True, ""))
        capable.start()
        self.addCleanup(capable.stop)
        chunk = self.chunk(0)
        self.config.set("summary.min_words", 2)
        self.publish(chunk, ["only"])
        chunk.word_count = 999
        source, reason = self.pipeline._summary_source(self.session, chunk)
        self.assertIsNone(source)
        self.assertIn("2 are required", reason)

        self.publish(chunk, ["one", "two"])
        chunk.transcript_status = PENDING
        self.assertIsNone(self.pipeline._summary_source(self.session, chunk)[0])

        self.publish(chunk, ["one", "two"], complete=False)
        chunk.transcript_status = DONE
        self.assertIsNone(self.pipeline._summary_source(self.session, chunk)[0])

        self.config.set("summary.min_words", 0)
        self.publish(chunk, [])
        source, reason = self.pipeline._summary_source(self.session, chunk)
        self.assertIsNone(source)
        self.assertIn("no speech", reason)
        with self.assertRaisesRegex(RuntimeError, "no speech"):
            self.pipeline.resummarize(self.session, chunk)

    def test_retirement_failure_is_an_artifact_error_not_skipped(self):
        chunk = self.chunk(0)
        generation = self.publish(chunk, ["speech"])
        rundown = self.rundown(chunk, generation)
        self.publish(chunk, [])

        with patch.object(
                self.pipeline, "_retire_rundown",
                side_effect=OSError("locked rundown")):
            with self.assertRaisesRegex(OSError, "locked rundown"):
                self.pipeline._summarize(self.session, chunk)

        self.assertTrue(rundown.exists())
        self.assertEqual(chunk.summary_status, ERROR)
        self.assertIn("locked rundown", chunk.summary_error)


class GenerationRecoveryTests(SummaryFixture):
    def test_restart_adopts_only_a_matching_rundown_generation(self):
        chunk = self.chunk(0)
        generation = self.publish(chunk, ["speech"])
        rundown = self.rundown(chunk, generation)

        self.assertFalse(self.pipeline._recover_summary_state(
            self.session, chunk, complete=True))
        self.assertEqual(chunk.summary_status, DONE)

        write_rundown(
            rundown, "# stale", "old header", "0" * 16)
        self.assertTrue(self.pipeline._recover_summary_state(
            self.session, chunk, complete=True))
        self.assertFalse(rundown.exists())
        self.assertEqual(chunk.summary_status, PENDING)

    def test_failed_retranscription_preserves_the_current_generation_and_rundown(self):
        chunk = self.chunk(0)
        generation = self.publish(chunk, ["original"])
        rundown = self.rundown(chunk, generation)
        before = rundown.read_bytes()

        with patch.object(
                self.pipeline.transcriber, "finalize",
                return_value=AdvanceResult(
                    BLOCKED, covered_through=0.0, expected=10.0,
                    detail="provider failed")):
            job = self.pipeline.retranscribe(self.session, chunk)
            self.wait(job)

        self.assertEqual(job.status, "failed")
        self.assertEqual(
            self.pipeline._current_export_generation(self.session, chunk),
            generation)
        self.assertEqual(rundown.read_bytes(), before)
        self.assertEqual(chunk.transcript_status, DONE)


class BlockingSummarizer:
    def __init__(self, started: threading.Event, release: threading.Event):
        self.started = started
        self.release = release
        self.inputs: list[str] = []

    def summarize(self, transcript: str, header: str) -> str:
        self.inputs.append(transcript)
        if "alpha" in transcript:
            self.started.set()
            if not self.release.wait(10):
                raise RuntimeError("test release timed out")
            return "# stale alpha rundown"
        return "# current beta rundown"


class SummaryRaceTests(SummaryFixture):
    def test_blocked_old_model_cannot_overwrite_retranscribed_generation(self):
        self.config.set("transcription.stitch_chunk_boundaries", False)
        chunk = self.chunk(0)
        first_generation = self.publish(chunk, ["alpha"])
        self.rundown(chunk, first_generation, "# previous alpha rundown")
        started = threading.Event()
        release = threading.Event()
        self.addCleanup(release.set)
        summarizer = BlockingSummarizer(started, release)

        def rebuild(session, target):
            self.publish(target, ["beta"])
            return AdvanceResult(
                COMPLETE_, covered_through=10.0, expected=10.0)

        with patch("vodpipe.pipeline.build_summarizer", return_value=summarizer), \
                patch.object(self.pipeline.transcriber, "finalize",
                             side_effect=rebuild):
            old_job = self.pipeline._queue_summary(self.session, chunk)
            self.assertTrue(started.wait(5), "old summary never reached the model")

            retranscribe = self.pipeline.retranscribe(self.session, chunk)
            self.wait(retranscribe)
            self.assertEqual(
                retranscribe.status, "done",
                "retranscription waited for the blocked model call")

            current_generation = self.pipeline._current_export_generation(
                self.session, chunk)
            self.assertNotEqual(current_generation, first_generation)
            current_key = (
                f"summary:{self.session.session_id}:{chunk.label}:"
                f"{current_generation}")
            current_job = self.pipeline.media_jobs.get(current_key)
            self.assertIsNotNone(current_job)
            self.wait(current_job)
            self.assertEqual(current_job.status, "done", current_job.error)
            self.assertEqual(
                rundown_generation(
                    self.pipeline.transcriber.output_dir(
                        self.session, chunk) / "report.md"),
                current_generation)

            release.set()
            self.wait(old_job)

        rundown = self.pipeline.transcriber.output_dir(
            self.session, chunk) / "report.md"
        self.assertIn("current beta", rundown.read_text(encoding="utf-8"))
        self.assertNotIn("stale alpha", rundown.read_text(encoding="utf-8"))
        self.assertEqual(chunk.summary_status, DONE)


class ThreeChunkRetranscriptionTests(SummaryFixture):
    def setUp(self):
        super().setUp()
        self.chunks = [self.chunk(index) for index in range(3)]
        self.generations = [
            self.publish(chunk, [f"original-{chunk.index}"])
            for chunk in self.chunks
        ]
        self.rundowns = [
            self.rundown(chunk, generation)
            for chunk, generation in zip(self.chunks, self.generations)
        ]

    def run_middle_retranscription(self, stitch, *, expected="done"):
        middle = self.chunks[1]

        def rebuild(session, target):
            self.publish(target, ["middle-rebuilt"])
            return AdvanceResult(
                COMPLETE_, covered_through=10.0, expected=10.0)

        queued: list[int] = []
        with patch.object(self.pipeline.transcriber, "finalize",
                          side_effect=rebuild), \
                patch.object(self.pipeline.transcriber, "stitch_with_previous",
                             side_effect=stitch), \
\
                patch.object(
                    self.pipeline, "_queue_summary",
                    side_effect=lambda session, chunk, **kwargs:
                    queued.append(chunk.index)):
            job = self.pipeline.retranscribe(self.session, middle)
            self.wait(job)
        self.assertEqual(job.status, expected, job.error)
        return queued, job

    def test_middle_retranscription_repairs_both_seams_and_requeues_all_changes(self):
        calls: list[int] = []

        def stitch(session, boundary, *, strict=False):
            calls.append(boundary.index)
            self.assertTrue(strict)
            if boundary.index == 1:
                self.publish(self.chunks[0], ["left-repaired"])
                self.publish(self.chunks[1], ["middle-left-repaired"])
            else:
                self.publish(self.chunks[1], ["middle-right-repaired"])
                self.publish(self.chunks[2], ["right-repaired"])
            return True

        queued, _ = self.run_middle_retranscription(stitch)

        self.assertEqual(calls, [1, 2])
        self.assertEqual(queued, [0, 1, 2])
        self.assertTrue(all(not path.exists() for path in self.rundowns))

    def test_second_seam_failure_restores_all_three_generations_and_rundowns(self):
        calls: list[int] = []
        rundown_bytes = [path.read_bytes() for path in self.rundowns]
        summary_states = [(chunk.summary_status, chunk.summary_error)
                          for chunk in self.chunks]

        def stitch(session, boundary, *, strict=False):
            calls.append(boundary.index)
            self.assertTrue(strict)
            if boundary.index == 1:
                self.publish(self.chunks[0], ["left-repaired"])
                self.publish(self.chunks[1], ["middle-left-repaired"])
                return True
            self.publish(self.chunks[1], ["middle-second-attempt"])
            raise OSError("injected second seam publication failure")

        queued, _ = self.run_middle_retranscription(stitch, expected="failed")

        self.assertEqual(calls, [1, 2])
        self.assertEqual(queued, [])
        self.assertEqual([
            self.pipeline._current_export_generation(self.session, chunk)
            for chunk in self.chunks
        ], self.generations)
        self.assertEqual([path.read_bytes() for path in self.rundowns],
                         rundown_bytes)
        self.assertEqual(
            [(chunk.summary_status, chunk.summary_error)
             for chunk in self.chunks],
            summary_states)

    def test_provider_build_and_empty_second_seams_all_roll_back(self):
        from vodpipe.asr import TranscriptionError

        rundown_bytes = [path.read_bytes() for path in self.rundowns]
        failures = (
            TranscriptionError("seam provider failed"),
            RuntimeError("could not build boundary audio"),
            RuntimeError("boundary transcription returned no words"),
        )
        for failure in failures:
            with self.subTest(failure=str(failure)):
                def stitch(session, boundary, *, strict=False):
                    self.assertTrue(strict)
                    if boundary.index == 1:
                        self.publish(self.chunks[0], ["temporary-left"])
                        self.publish(self.chunks[1], ["temporary-middle"])
                        return True
                    raise failure

                queued, _ = self.run_middle_retranscription(
                    stitch, expected="failed")
                self.assertEqual(queued, [])
                self.assertEqual([
                    self.pipeline._current_export_generation(self.session, item)
                    for item in self.chunks
                ], self.generations)
                self.assertEqual(
                    [path.read_bytes() for path in self.rundowns], rundown_bytes)


class CapabilityTests(SummaryFixture):
    def test_capability_follows_the_claude_executable(self):
        """The engine spends a subscription, so the whole question is whether
        `claude -p` can be run. It used to be whether an API key was set."""
        import dataclasses

        self.config.set("summary.provider", "claude-cli")
        self.pipeline.tools = dataclasses.replace(
            self.pipeline.tools, claude="")
        payload = self.pipeline.state_payload()
        self.assertFalse(payload["capabilities"]["claude_cli"])
        self.assertFalse(payload["capabilities"]["summary_available"])

        self.pipeline.tools = dataclasses.replace(
            self.pipeline.tools, claude="claude.exe")
        payload = self.pipeline.state_payload()
        self.assertTrue(payload["capabilities"]["claude_cli"])
        self.assertTrue(payload["capabilities"]["summary_available"])

    def test_switching_rundowns_off_is_reported_as_disabled(self):
        self.config.set("summary.provider", "none")
        payload = self.pipeline.state_payload()
        self.assertFalse(payload["capabilities"]["summary_available"])
        self.assertIn(
            "disabled", payload["capabilities"]["summary_unavailable_reason"])


if __name__ == "__main__":
    unittest.main()
