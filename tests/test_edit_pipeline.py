"""How the edited cut is scheduled, adopted, refused and recovered.

The scheduling rule worth understanding before changing anything here: a chunk
is not cut until its *seam* is settled. Boundary repair rewrites the tail of a
chunk when its successor's transcript completes, so cutting earlier spends a
whole re-encode -- forty minutes for a two-hour chunk -- to be redone for one
word at the join. `_seam_settled` is what buys that back, and the manual
`recut()` path deliberately ignores it.
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from vodpipe.config import DEFAULTS, Config, deep_merge
from vodpipe.edit import EditRefused
from vodpipe.exports import write_exports
from vodpipe.jobs import Job
from vodpipe.pipeline import Pipeline
from vodpipe.state import (
    COMPLETE,
    DONE,
    ERROR,
    PENDING,
    RECORDING,
    SKIPPED,
    Chunk,
    Session,
)
from vodpipe.transcript import Word


class EditPipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="vodpipe-edit-pipe-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.config = Config(deep_merge(DEFAULTS, {
            "paths": {
                "masters_root": str(self.tmp / "masters"),
                "work_root": str(self.tmp / "work"),
                "censor_master_list": str(self.tmp / "none.txt"),
            },
            "recording": {"free_space_floor_gb": 0, "hard_reserve_gb": 0},
            "proxies": {"enabled": False},
            "transcription": {"enabled": False},
            "summary": {"enabled": False, "provider": "none"},
            "watcher": {"enabled": False},
        }), self.tmp / "config.json")
        self.config.masters_root.mkdir(parents=True)
        self.pipeline = Pipeline(self.config)
        self.addCleanup(self.pipeline.shutdown, job_timeout=10)
        self.directory = self.config.masters_root / "chan" / "sess"
        for name in ("live", "master", "transcripts"):
            (self.directory / name).mkdir(parents=True, exist_ok=True)
        self.session = self.pipeline.store.add(Session(
            session_id="sess", channel="chan", started_at=1.0,
            directory=str(self.directory), status=COMPLETE))
        self.queued = self._capture_media_jobs()

    def _capture_media_jobs(self) -> list[str]:
        keys: list[str] = []

        def submit(key, label, kind, work):
            keys.append(key)
            return Job(key, label, kind)

        patcher = patch.object(self.pipeline.media_jobs, "submit",
                               side_effect=submit)
        patcher.start()
        self.addCleanup(patcher.stop)
        return keys

    def chunk(self, index: int = 0, **changes) -> Chunk:
        values = dict(index=index, session_id="sess", channel="chan",
                      started_at=1.0, ts_name=f"chan_c{index:03d}.ts",
                      master_name=f"chan_c{index:03d}.mp4", duration=10.0,
                      status=COMPLETE)
        values.update(changes)
        item = Chunk(**values)
        self.pipeline.store.add_chunk(self.session, item)
        (self.directory / "master" / item.master_name).write_bytes(b"m" * 64)
        return item

    def publish(self, chunk: Chunk) -> str:
        output = self.pipeline.transcriber.output_dir(self.session, chunk)
        write_exports(
            output, [Word("hello", 0.0, 0.5, 0.9)], language="en",
            meta={"channel": "chan", "session_id": "sess",
                  "chunk": chunk.label, "complete": True,
                  "session_offset": chunk.session_offset},
            words_meta={"channel": "chan", "session_id": "sess",
                        "chunk": chunk.label, "language": "en",
                        "session_offset": chunk.session_offset,
                        "complete": True, "covered_seconds": chunk.duration,
                        "expected_seconds": chunk.duration})
        return self.pipeline._current_export_generation(self.session, chunk)

    def write_edit(self, chunk: Chunk, generation: str) -> Path:
        destination = self.pipeline._edit_destination(self.session, chunk)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"edited")
        output = self.pipeline.transcriber.output_dir(self.session, chunk)
        write_exports(
            output / "edited", [Word("hello", 0.0, 0.5, 0.9)], language="en",
            meta={"source": destination.name, "complete": True},
            words_meta={"source": destination.name, "language": "en",
                        "complete": True, "covered_seconds": 5.0,
                        "expected_seconds": 5.0,
                        "edited_from_generation": generation})
        return destination

    # -- scheduling ---------------------------------------------------------

    def test_disabled_marks_the_chunk_skipped_and_queues_nothing(self):
        self.config.set("edit.enabled", False)
        chunk = self.chunk(transcript_status=DONE)
        self.publish(chunk)
        self.assertIsNone(self.pipeline._queue_edit(self.session, chunk))
        self.assertEqual(chunk.edit_status, SKIPPED)
        self.assertEqual(self.queued, [])

    def test_an_incomplete_transcript_is_not_cut(self):
        chunk = self.chunk(transcript_status=PENDING)
        self.assertIsNone(self.pipeline._queue_edit(self.session, chunk))
        self.assertEqual(self.queued, [])

    def test_a_chunk_waits_for_its_seam_before_being_cut(self):
        self.session.status = RECORDING
        first = self.chunk(0, transcript_status=DONE)
        second = self.chunk(1, transcript_status=PENDING)
        self.publish(first)

        self.assertIsNone(self.pipeline._queue_edit(self.session, first))
        self.assertEqual(self.queued, [])

        second.transcript_status = DONE
        self.assertIsNotNone(self.pipeline._queue_edit(self.session, first))
        self.assertEqual(len(self.queued), 1)
        self.assertTrue(self.queued[0].startswith("edit:sess:c000:"))

    def test_the_last_chunk_is_cut_once_the_session_cannot_grow(self):
        self.session.status = RECORDING
        chunk = self.chunk(0, transcript_status=DONE)
        self.publish(chunk)
        self.assertIsNone(self.pipeline._queue_edit(self.session, chunk))

        self.session.status = COMPLETE
        self.assertIsNotNone(self.pipeline._queue_edit(self.session, chunk))

    def test_without_stitching_a_chunk_is_cut_immediately(self):
        self.config.set("transcription.stitch_chunk_boundaries", False)
        self.session.status = RECORDING
        chunk = self.chunk(0, transcript_status=DONE)
        self.publish(chunk)
        self.assertIsNotNone(self.pipeline._queue_edit(self.session, chunk))

    def test_the_job_key_carries_the_generation(self):
        chunk = self.chunk(0, transcript_status=DONE)
        generation = self.publish(chunk)
        self.pipeline._queue_edit(self.session, chunk)
        self.assertEqual(self.queued, [f"edit:sess:c000:{generation}"])

    def test_the_edit_runs_on_the_media_pool_not_the_capture_pool(self):
        chunk = self.chunk(0, transcript_status=DONE)
        self.publish(chunk)
        before = len(self.pipeline.jobs.snapshot())
        self.pipeline._queue_edit(self.session, chunk)
        self.assertEqual(len(self.queued), 1)
        self.assertEqual(len(self.pipeline.jobs.snapshot()), before)

    def test_session_end_queues_the_edits_that_were_waiting(self):
        self.session.status = COMPLETE
        chunk = self.chunk(0, transcript_status=DONE)
        self.publish(chunk)
        self.pipeline._queue_final_edits(self.session)
        self.assertEqual(len(self.queued), 1)

    # -- recovery -----------------------------------------------------------

    def test_recovery_adopts_an_edit_built_from_the_current_transcript(self):
        chunk = self.chunk(0, transcript_status=DONE)
        generation = self.publish(chunk)
        destination = self.write_edit(chunk, generation)

        actions = self.pipeline._recover_edit_state(
            self.session, chunk, complete=True)

        self.assertEqual(chunk.edit_status, DONE)
        self.assertEqual(chunk.edit_name, destination.name)
        self.assertEqual(self.queued, [])
        self.assertTrue(any("adopted" in action for action in actions))

    def test_recovery_rebuilds_an_edit_from_a_replaced_transcript(self):
        chunk = self.chunk(0, transcript_status=DONE)
        self.publish(chunk)
        self.write_edit(chunk, "a-generation-that-is-not-current")

        actions = self.pipeline._recover_edit_state(
            self.session, chunk, complete=True)

        self.assertEqual(chunk.edit_status, PENDING)
        self.assertEqual(len(self.queued), 1)
        self.assertTrue(any("stale" in action for action in actions))

    def test_recovery_requeues_a_missing_edit(self):
        chunk = self.chunk(0, transcript_status=DONE)
        self.publish(chunk)
        self.pipeline._recover_edit_state(self.session, chunk, complete=True)
        self.assertEqual(len(self.queued), 1)

    def test_recovery_skips_the_edit_when_it_is_switched_off(self):
        self.config.set("edit.enabled", False)
        chunk = self.chunk(0, transcript_status=DONE)
        self.publish(chunk)
        self.pipeline._recover_edit_state(self.session, chunk, complete=True)
        self.assertEqual(chunk.edit_status, SKIPPED)
        self.assertEqual(self.queued, [])

    def test_an_interrupted_edit_does_not_stay_running_forever(self):
        chunk = self.chunk(0, transcript_status=PENDING, edit_status="running")
        self.pipeline._recover_edit_state(self.session, chunk, complete=False)
        self.assertEqual(chunk.edit_status, PENDING)

    # -- manual re-cut ------------------------------------------------------

    def test_recut_removes_the_previous_file_so_a_rebuild_rebuilds(self):
        chunk = self.chunk(0, transcript_status=DONE)
        generation = self.publish(chunk)
        destination = self.write_edit(chunk, generation)

        self.pipeline.recut(self.session, chunk)

        self.assertFalse(destination.exists())
        self.assertEqual(len(self.queued), 1)

    def test_recut_ignores_the_seam_gate(self):
        self.session.status = RECORDING
        first = self.chunk(0, transcript_status=DONE)
        self.chunk(1, transcript_status=PENDING)
        self.publish(first)
        self.assertIsNone(self.pipeline._queue_edit(self.session, first))
        self.assertIsNotNone(self.pipeline.recut(self.session, first))

    def test_recut_refuses_without_a_complete_transcript(self):
        chunk = self.chunk(0, transcript_status=PENDING)
        with self.assertRaises(RuntimeError) as caught:
            self.pipeline.recut(self.session, chunk)
        self.assertIn("complete transcript", str(caught.exception))

    def test_recut_refuses_when_the_feature_is_off(self):
        self.config.set("edit.enabled", False)
        chunk = self.chunk(0, transcript_status=DONE)
        self.publish(chunk)
        with self.assertRaises(RuntimeError):
            self.pipeline.recut(self.session, chunk)

    # -- failure handling ---------------------------------------------------

    def test_a_refusal_is_a_skip_rather_than_an_error(self):
        chunk = self.chunk(0, transcript_status=DONE)
        generation = self.publish(chunk)
        with patch.object(self.pipeline, "_build_edit",
                          side_effect=EditRefused("removes 99%")):
            self.pipeline._make_edit(Job("k", "l", "edit"), self.session,
                                     chunk, generation)
        self.assertEqual(chunk.edit_status, SKIPPED)
        self.assertIn("removes 99%", chunk.edit_error)

    def test_a_failure_reaches_the_chunk_state_and_is_re_raised(self):
        chunk = self.chunk(0, transcript_status=DONE)
        generation = self.publish(chunk)
        with patch.object(self.pipeline, "_build_edit",
                          side_effect=RuntimeError("encoder exploded")):
            with self.assertRaises(RuntimeError):
                self.pipeline._make_edit(Job("k", "l", "edit"), self.session,
                                         chunk, generation)
        self.assertEqual(chunk.edit_status, ERROR)
        self.assertIn("encoder exploded", chunk.edit_error)

    def test_the_edit_error_is_reported_separately_from_the_others(self):
        chunk = self.chunk(0)
        self.pipeline.store.update_chunk(
            self.session, chunk, edit_status=ERROR, edit_error="no encoder")
        self.assertEqual(chunk.errors.get("edit"), "no encoder")
        self.assertNotIn("proxy", chunk.errors)


class EditManifestTests(unittest.TestCase):
    """A new persisted field needs the field set, the validator, `to_dict` and
    the rebuild updated together -- the manifest schema is strict, so missing
    one of them makes every session with an edit unreadable."""

    def session_with(self, **changes) -> Session:
        session = Session(session_id="s", channel="chan", started_at=1.0,
                          directory="d", status=COMPLETE)
        session.chunks.append(Chunk(index=0, session_id="s", channel="chan",
                                    started_at=1.0, **changes))
        return Session.from_dict(session.to_dict())

    def test_the_edit_artifact_survives_a_manifest_round_trip(self):
        restored = self.session_with(edit_status=DONE, edit_error="",
                                     edit_name="c000_Edited.mp4")
        self.assertEqual(restored.chunks[0].edit_status, DONE)
        self.assertEqual(restored.chunks[0].edit_name, "c000_Edited.mp4")

    def test_an_edit_error_round_trips_and_is_validated(self):
        from vodpipe.state import _validate_chunk

        payload = self.session_with(edit_status=ERROR,
                                    edit_error="no encoder").to_dict()
        self.assertEqual(payload["chunks"][0]["errors"]["edit"], "no encoder")
        _validate_chunk(payload["chunks"][0], 0, "s", "chan", set())

    def test_an_edit_name_that_escapes_the_session_is_dropped(self):
        """`edit_name` is joined onto the session directory and recovery deletes
        what it points at, exactly as `proxy_name` does."""
        session = Session(session_id="s", channel="chan", started_at=1.0,
                          directory="d", status=COMPLETE)
        session.chunks.append(Chunk(index=0, session_id="s", channel="chan",
                                    started_at=1.0))
        data = session.to_dict()
        data["chunks"][0]["edit_name"] = "../../important.mp4"
        self.assertEqual(Session.from_dict(data).chunks[0].edit_name, "")


if __name__ == "__main__":
    unittest.main()


class EditFolderSafetyTests(unittest.TestCase):
    """The edited cut is a deliverable and must not expire like a proxy."""

    def test_the_edit_folder_may_not_be_the_proxy_folder(self):
        from vodpipe.schema import ConfigError, validate

        with self.assertRaises(ConfigError) as caught:
            validate(deep_merge(DEFAULTS, {"edit": {"folder_name": "Proxies"}}))
        self.assertIn("retention", str(caught.exception))

    def test_the_defaults_do_not_collide(self):
        from vodpipe.schema import validate

        cleaned = validate(deep_merge(DEFAULTS, {}))
        self.assertNotEqual(cleaned["edit"]["folder_name"],
                            cleaned["proxies"]["folder_name"])


class RecutSafetyTests(EditPipelineTests):
    def test_recut_refuses_while_a_build_is_in_flight_without_deleting(self):
        """Pressing it twice must not leave the operator with neither the old
        cut nor a queued replacement."""
        from vodpipe.jobs import QUEUED

        chunk = self.chunk(0, transcript_status=DONE)
        generation = self.publish(chunk)
        destination = self.write_edit(chunk, generation)

        running = Job(f"edit:sess:c000:{generation}", "l", "edit")
        running.status = QUEUED
        with patch.object(self.pipeline.media_jobs, "get",
                          return_value=running):
            with self.assertRaises(RuntimeError) as caught:
                self.pipeline.recut(self.session, chunk)

        self.assertIn("already being built", str(caught.exception))
        self.assertTrue(destination.exists(),
                        "the finished cut must survive a refused re-cut")


class NoTranscriptTests(EditPipelineTests):
    """An edit without a transcript has no word veto, so there is no useful
    degraded mode -- it must say so rather than sit at pending forever."""

    def test_a_skipped_transcript_skips_the_edit_with_a_reason(self):
        chunk = self.chunk(0, transcript_status=SKIPPED)
        self.assertIsNone(self.pipeline._queue_edit(self.session, chunk))
        self.assertEqual(chunk.edit_status, SKIPPED)
        self.assertIn("needs a transcript", chunk.edit_error)
        self.assertEqual(self.queued, [])

    def test_recovery_agrees(self):
        chunk = self.chunk(0, transcript_status=SKIPPED)
        self.pipeline._recover_edit_state(self.session, chunk, complete=False)
        self.assertEqual(chunk.edit_status, SKIPPED)
        self.assertIn("needs a transcript", chunk.edit_error)
