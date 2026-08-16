"""Snapshot integrity (AUD-017, AUD-025).

Three defects with one shape -- the early cut reported success it had not earned:

* it preferred a chunk's `.ts` working copy even after the master existed, and the
  pipeline could delete that file mid-cut;
* it concatenated whatever happened to overlap the requested range and never
  proved the range was covered, so a session missing a chunk in the middle
  produced a shorter file with no complaint;
* it reported the duration that was *asked for* rather than the one in the file.

Plus AUD-025: cutting ran on the HTTP thread, unbounded.
"""

from __future__ import annotations

import io
import json
import shutil
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from vodpipe.cli import cmd_snapshot
from vodpipe.config import DEFAULTS, Config, deep_merge
from vodpipe.jobs import CANCELLED, DONE, JobRunner
from vodpipe.snapshot import SnapshotRequest, SnapshotService
from vodpipe.state import Chunk, Session


def make_config(root: Path) -> Config:
    return Config(deep_merge(DEFAULTS, {
        "paths": {"masters_root": str(root / "m"), "work_root": str(root / "w"),
                  "censor_master_list": str(root / "none.txt")},
        "watcher": {"enabled": False},
        "transcription": {"enabled": False},
        "summary": {"provider": "none"},
    }), root / "config.json")


class GeometryFixture(unittest.TestCase):
    """Placeholder media -- these tests are about arithmetic, not encoding."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="vodpipe-snap-"))
        self.config = make_config(self.tmp)
        self.session_dir = self.tmp / "m" / "chan" / "sess"
        (self.session_dir / "master").mkdir(parents=True)
        (self.session_dir / "live").mkdir(parents=True)

        self.session = Session(session_id="sess", channel="chan",
                               started_at=time.time(),
                               directory=str(self.session_dir), status="complete")
        from vodpipe.util import Tools
        self.tools = Tools(ffmpeg="ffmpeg", ffprobe="ffprobe",
                           streamlink="streamlink", claude=None)
        self.service = SnapshotService(self.config, self.tools)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def add_chunk(self, index, offset, duration, *, master=True, live=False,
                  status="complete"):
        label = f"c{index:03d}"
        chunk = Chunk(index=index, session_id="sess", channel="chan",
                      started_at=time.time(), ts_name=f"chan_{label}.ts",
                      master_name=f"chan_{label}.mp4", duration=float(duration),
                      session_offset=float(offset), status=status)
        if master:
            (self.session_dir / "master" / chunk.master_name).write_bytes(b"x" * 2048)
        if live:
            (self.session_dir / "live" / chunk.ts_name).write_bytes(b"x" * 2048)
        self.session.chunks.append(chunk)
        return chunk


class SourcePreferenceTests(GeometryFixture):
    def test_a_closed_chunk_is_read_from_its_master(self):
        """The .ts is a working copy the pipeline reclaims; the master is not."""
        chunk = self.add_chunk(0, 0, 10, master=True, live=True)
        source, offset, duration = self.service.chunk_span(self.session, chunk)
        self.assertEqual(source.name, chunk.master_name)
        self.assertEqual(offset, 0.0)
        self.assertEqual(duration, 10.0)

    def test_a_closed_chunk_falls_back_to_the_ts_when_the_remux_has_not_run(self):
        chunk = self.add_chunk(0, 0, 10, master=False, live=True,
                               status="remuxing")
        source, _, _ = self.service.chunk_span(self.session, chunk)
        self.assertEqual(source.name, chunk.ts_name)

    def test_a_recording_chunk_is_only_ever_read_from_the_ts(self):
        """An MP4 has no index until it is closed, so a master here is not usable."""
        chunk = self.add_chunk(0, 0, 10, master=True, live=True,
                               status="recording")
        # duration comes from probing, which these placeholder files cannot
        # satisfy -- the source choice is what matters.
        source, _, _ = self.service.chunk_span(self.session, chunk)
        self.assertEqual(source.name, chunk.ts_name)

    def test_a_chunk_with_no_media_at_all_reports_nothing(self):
        chunk = self.add_chunk(0, 0, 10, master=False, live=False,
                               status="failed")
        source, _, _ = self.service.chunk_span(self.session, chunk)
        self.assertIsNone(source)


class CoverageTests(GeometryFixture):
    def test_a_contiguous_range_plans_cleanly(self):
        self.add_chunk(0, 0, 10)
        self.add_chunk(1, 10, 10)
        parts, spans = self.service.plan(self.session, 5.0, 15.0)
        self.assertEqual(spans, ["c000", "c001"])
        self.assertEqual(len(parts), 2)
        # Each piece is expressed in its own file's clock.
        self.assertAlmostEqual(parts[0][1], 5.0)
        self.assertAlmostEqual(parts[0][2], 10.0)
        self.assertAlmostEqual(parts[1][1], 0.0)
        self.assertAlmostEqual(parts[1][2], 5.0)

    def test_a_hole_in_the_middle_is_refused(self):
        """The regression: this silently produced a file that jumped."""
        self.add_chunk(0, 0, 10)
        self.add_chunk(2, 20, 10)          # c001's media never survived
        with self.assertRaises(RuntimeError) as caught:
            self.service.plan(self.session, 5.0, 25.0)
        self.assertIn("no recorded media between", str(caught.exception))

    def test_a_range_running_past_the_recording_is_refused(self):
        self.add_chunk(0, 0, 10)
        with self.assertRaises(RuntimeError) as caught:
            self.service.plan(self.session, 5.0, 60.0)
        self.assertIn("covers only up to", str(caught.exception))

    def test_a_range_touching_no_media_is_refused(self):
        self.add_chunk(0, 0, 10)
        with self.assertRaises(RuntimeError):
            self.service.plan(self.session, 100.0, 120.0)

    def test_a_boundary_sized_gap_is_tolerated(self):
        """Keyframe-aligned chunks never abut to the millisecond."""
        self.add_chunk(0, 0, 10)
        self.add_chunk(1, 10.2, 10)
        parts, _ = self.service.plan(self.session, 5.0, 15.0)
        self.assertEqual(len(parts), 2)

    def test_a_single_chunk_range_needs_no_join(self):
        self.add_chunk(0, 0, 30)
        parts, spans = self.service.plan(self.session, 5.0, 12.0)
        self.assertEqual(len(parts), 1)
        self.assertEqual(spans, ["c000"])


class LeaseTests(unittest.TestCase):
    """A .ts being read by a snapshot must outlive the remux that replaces it."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="vodpipe-lease-"))
        self.config = make_config(self.tmp)
        self.config.masters_root.mkdir(parents=True, exist_ok=True)
        from vodpipe.pipeline import Pipeline
        self.pipeline = Pipeline(self.config)
        self.addCleanup(lambda: [pool.stop(timeout=10, drain=False)
                                 for pool in self.pipeline.pools])
        self.addCleanup(shutil.rmtree, self.tmp, True)

        self.session = Session(session_id="sess", channel="chan",
                               started_at=time.time(),
                               directory=str(self.tmp / "m" / "chan" / "sess"))
        self.chunk = Chunk(index=0, session_id="sess", channel="chan",
                           started_at=time.time(), ts_name="chan_c000.ts")
        self.source = self.tmp / "chan_c000.ts"
        self.source.write_bytes(b"recorded video")

    def test_an_unleased_file_is_removed_immediately(self):
        self.pipeline._reclaim_ts(self.session, self.chunk, self.source)
        self.assertFalse(self.source.exists())

    def test_a_leased_file_survives_until_the_lease_is_released(self):
        with self.pipeline.read_lease([self.source]):
            self.pipeline._reclaim_ts(self.session, self.chunk, self.source)
            self.assertTrue(self.source.exists(),
                            "a snapshot was still reading this")
        # The deletion was deferred, not cancelled.
        self.assertFalse(self.source.exists())

    def test_nested_leases_release_only_on_the_last_one(self):
        with self.pipeline.read_lease([self.source]):
            with self.pipeline.read_lease([self.source]):
                self.pipeline._reclaim_ts(self.session, self.chunk, self.source)
                self.assertTrue(self.source.exists())
            self.assertTrue(self.source.exists(), "one lease is still held")
        self.assertFalse(self.source.exists())

    def test_a_lease_with_no_deletion_pending_leaves_the_file_alone(self):
        with self.pipeline.read_lease([self.source]):
            pass
        self.assertTrue(self.source.exists())

    def test_the_lease_is_released_even_if_the_cut_raises(self):
        with self.assertRaises(ValueError):
            with self.pipeline.read_lease([self.source]):
                self.pipeline._reclaim_ts(self.session, self.chunk, self.source)
                raise ValueError("the cut failed")
        self.assertFalse(self.source.exists())

    def test_keeping_the_ts_is_still_honoured(self):
        self.config.set("recording.keep_ts_after_remux", True)
        with self.pipeline.read_lease([self.source]):
            self.pipeline._reclaim_ts(self.session, self.chunk, self.source)
        self.assertTrue(self.source.exists())

    def test_concurrent_leases_on_different_files_do_not_interfere(self):
        other = self.tmp / "chan_c001.ts"
        other.write_bytes(b"more video")
        with self.pipeline.read_lease([self.source]):
            self.pipeline._reclaim_ts(self.session, self.chunk, other)
            self.assertFalse(other.exists())
            self.assertTrue(self.source.exists())


class SnapshotQueueTests(unittest.TestCase):
    """AUD-025: cutting is queued and bounded, not run on the request thread."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="vodpipe-queue-"))
        self.config = make_config(self.tmp)
        self.config.masters_root.mkdir(parents=True, exist_ok=True)
        from vodpipe.pipeline import Pipeline
        self.pipeline = Pipeline(self.config)
        # Cleanups run last-registered-first, and the order matters here: release
        # the blocked cut, wait for the pools to finish it, and only then delete
        # the tree. Deleting underneath a running worker is a test that fails for
        # reasons that have nothing to do with the code.
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.addCleanup(lambda: [pool.stop(timeout=10, drain=False)
                                 for pool in self.pipeline.pools])

        session_dir = self.tmp / "m" / "chan" / "sess"
        (session_dir / "master").mkdir(parents=True)
        self.session = Session(session_id="sess", channel="chan",
                               started_at=time.time(),
                               directory=str(session_dir), status="complete")
        chunk = Chunk(index=0, session_id="sess", channel="chan",
                      started_at=time.time(), ts_name="chan_c000.ts",
                      master_name="chan_c000.mp4", duration=60.0,
                      status="complete")
        (session_dir / "master" / chunk.master_name).write_bytes(b"x" * 2048)
        self.session.chunks.append(chunk)
        self.pipeline.store.add(self.session)

        # The cut itself is replaced: this is about queueing and caps.
        self.started = threading.Event()
        self.release = threading.Event()

        def slow_create(session, request):
            self.started.set()
            self.release.wait(20)
            from vodpipe.snapshot import SnapshotResult
            return SnapshotResult(path=session.path / "snapshots" / "cut.mp4",
                                  start=request.start or 0.0,
                                  end=request.end or 1.0, spans=["c000"])

        self.pipeline.snapshots.create = slow_create
        self.addCleanup(self.release.set)

    def request(self, start=0.0, end=10.0):
        return SnapshotRequest(session_id="sess", start=start, end=end,
                               transcribe=False)

    def test_queueing_returns_before_the_cut_finishes(self):
        job = self.pipeline.queue_snapshot(self.request())
        self.assertIn(job.status, ("queued", "running"))
        self.assertTrue(self.started.wait(10))
        self.assertFalse(self.release.is_set(), "the cut is still running")

    def test_a_second_cut_of_the_same_session_is_refused(self):
        self.pipeline.queue_snapshot(self.request())
        self.assertTrue(self.started.wait(10))
        with self.assertRaises(RuntimeError) as caught:
            self.pipeline.queue_snapshot(self.request(start=20.0, end=30.0))
        self.assertIn("already being cut", str(caught.exception))

    def test_an_impossible_range_fails_at_the_request_not_in_the_worker(self):
        # Both are answers the caller can act on, and the API maps either to 409.
        with self.assertRaises((RuntimeError, ValueError)):
            self.pipeline.queue_snapshot(self.request(start=500.0, end=600.0))
        self.assertFalse(self.started.is_set(), "nothing should have been queued")

    def test_a_range_with_a_hole_fails_at_the_request(self):
        self.session.chunks.append(Chunk(
            index=2, session_id="sess", channel="chan", started_at=time.time(),
            ts_name="chan_c002.ts", master_name="chan_c002.mp4",
            duration=60.0, session_offset=200.0, status="complete"))
        (self.session.path / "master" / "chan_c002.mp4").write_bytes(b"x" * 2048)
        with self.assertRaises(RuntimeError) as caught:
            self.pipeline.queue_snapshot(self.request(start=10.0, end=220.0))
        self.assertIn("no recorded media between", str(caught.exception))
        self.assertFalse(self.started.is_set())

    def test_an_unknown_session_is_refused_immediately(self):
        with self.assertRaises(RuntimeError):
            self.pipeline.queue_snapshot(
                SnapshotRequest(session_id="nope", start=0.0, end=5.0))

    def test_transcribing_a_snapshot_does_not_hold_a_cut_worker(self):
        """Minutes of ASR on a cut worker would make the next cut queue behind it."""
        self.config.set("transcription.enabled", True)
        self.config.set("secrets.deepgram_api_key", "test-key")
        started = threading.Event()
        release = threading.Event()
        self.addCleanup(release.set)

        def slow_transcribe(path, output, **kwargs):
            started.set()
            release.wait(20)
            return []

        self.pipeline.transcriber.transcribe_file = slow_transcribe

        from vodpipe.snapshot import SnapshotResult
        self.pipeline._after_snapshot(
            self.session,
            SnapshotRequest(session_id="sess", start=0.0, end=5.0,
                            transcribe=True),
            SnapshotResult(path=self.session.path / "snapshots" / "cut.mp4",
                           start=0.0, end=5.0, spans=["c000"]))

        self.assertTrue(started.wait(10))
        self.assertEqual(self.pipeline.media_jobs.active_count(), 1)
        self.assertEqual(self.pipeline.snapshot_jobs.active_count(), 0)

    def test_the_cut_runs_on_the_snapshot_pool(self):
        self.pipeline.queue_snapshot(self.request())
        self.assertTrue(self.started.wait(10))
        self.assertEqual(self.pipeline.snapshot_jobs.active_count(), 1)
        self.assertEqual(self.pipeline.jobs.active_count(), 0,
                         "capture-critical work must not be behind a cut")

    def test_pending_intent_is_durable_before_submit_can_start_worker(self):
        observed = []
        submit = self.pipeline.snapshot_jobs.submit

        def inspect(*args, **kwargs):
            index = self.session.path / "snapshots" / "snapshots.json"
            observed.extend(json.loads(index.read_text(encoding="utf-8")))
            return submit(*args, **kwargs)

        with patch.object(self.pipeline.snapshot_jobs, "submit",
                          side_effect=inspect):
            self.pipeline.queue_snapshot(SnapshotRequest(
                session_id="sess", start=0.0, end=10.0, transcribe=True))

        self.assertEqual(observed[0]["cut_status"], "pending")
        self.assertEqual(observed[0]["transcript_status"], "pending")
        self.assertTrue(observed[0]["transcript_requested"])

    def test_worker_finishing_before_submit_returns_releases_once(self):
        reservation = Mock()
        submit = self.pipeline.snapshot_jobs.submit

        def worker_first(*args, **kwargs):
            job = submit(*args, **kwargs)
            self.assertTrue(self.started.wait(10))
            self.release.set()
            deadline = time.time() + 10
            while job.status not in (DONE, "failed") and time.time() < deadline:
                time.sleep(0.01)
            return job

        with patch.object(self.pipeline.disk_budget, "reserve",
                          return_value=reservation), \
                patch.object(self.pipeline.snapshot_jobs, "submit",
                             side_effect=worker_first):
            job = self.pipeline.queue_snapshot(self.request())

        self.assertEqual(job.status, DONE)
        reservation.release.assert_called_once_with()
        self.assertFalse(self.pipeline._snapshot_reservations)

    def test_submit_shutdown_barrier_keeps_accepted_reservation_owned(self):
        reservation = Mock()
        entered = threading.Event()
        allow_submit = threading.Event()
        submit = self.pipeline.snapshot_jobs.submit
        errors = []

        def blocked_submit(*args, **kwargs):
            entered.set()
            allow_submit.wait(10)
            return submit(*args, **kwargs)

        with patch.object(self.pipeline.disk_budget, "reserve",
                          return_value=reservation), \
                patch.object(self.pipeline.snapshot_jobs, "submit",
                             side_effect=blocked_submit):
            queue_thread = threading.Thread(
                target=lambda: self._capture_error(
                    errors, self.pipeline.queue_snapshot, self.request()))
            queue_thread.start()
            self.assertTrue(entered.wait(10))
            shutdown = threading.Thread(
                target=lambda: self.pipeline.shutdown(job_timeout=10))
            shutdown.start()
            time.sleep(0.1)
            self.assertTrue(shutdown.is_alive(),
                            "shutdown crossed an in-flight admission")
            reservation.release.assert_not_called()

            allow_submit.set()
            queue_thread.join(10)
            self.assertTrue(self.started.wait(10))
            self.release.set()
            shutdown.join(20)

        self.assertFalse(errors)
        self.assertFalse(shutdown.is_alive())
        reservation.release.assert_called_once_with()

    @staticmethod
    def _capture_error(errors, function, *args):
        try:
            function(*args)
        except Exception as exc:
            errors.append(exc)

    def test_refused_submission_releases_once_and_removes_intent(self):
        reservation = Mock()
        with patch.object(self.pipeline.disk_budget, "reserve",
                          return_value=reservation), \
                patch.object(self.pipeline.snapshot_jobs, "submit",
                             return_value=None):
            with self.assertRaisesRegex(RuntimeError, "not being accepted"):
                self.pipeline.queue_snapshot(self.request())

        reservation.release.assert_called_once_with()
        index = self.session.path / "snapshots" / "snapshots.json"
        self.assertEqual(json.loads(index.read_text(encoding="utf-8")), [])

    def test_failed_worker_releases_once_and_persists_cut_error(self):
        reservation = Mock()
        self.pipeline.snapshots.create = Mock(side_effect=RuntimeError("cut broke"))
        with patch.object(self.pipeline.disk_budget, "reserve",
                          return_value=reservation):
            job = self.pipeline.queue_snapshot(self.request())
        deadline = time.time() + 10
        while job.status not in (DONE, "failed") and time.time() < deadline:
            time.sleep(0.01)

        self.assertEqual(job.status, "failed")
        reservation.release.assert_called_once_with()
        entries = json.loads((self.session.path / "snapshots" / "snapshots.json")
                             .read_text(encoding="utf-8"))
        self.assertEqual(entries[0]["cut_status"], "error")
        self.assertIn("cut broke", entries[0]["cut_error"])

    def test_cancelled_submission_releases_once(self):
        self.pipeline.snapshot_jobs.stop(timeout=10, drain=False)
        self.pipeline.snapshot_jobs = JobRunner(workers=1)
        blocker_started = threading.Event()
        blocker_release = threading.Event()
        self.addCleanup(blocker_release.set)
        self.pipeline.snapshot_jobs.submit(
            "block", "block", "test",
            lambda job: (blocker_started.set(), blocker_release.wait(10)))
        self.assertTrue(blocker_started.wait(10))

        reservation = Mock()
        with patch.object(self.pipeline.disk_budget, "reserve",
                          return_value=reservation):
            job = self.pipeline.queue_snapshot(self.request())
        self.pipeline.snapshot_jobs.stop(timeout=0, drain=False)
        self.pipeline._release_cancelled_snapshot_reservations()
        self.pipeline._release_cancelled_snapshot_reservations()

        self.assertEqual(job.status, CANCELLED)
        reservation.release.assert_called_once_with()
        blocker_release.set()
        self.pipeline.snapshot_jobs.stop(timeout=10, drain=False)


class SnapshotCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="vodpipe-snapshot-cli-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.config = make_config(self.tmp)
        self.path = self.tmp / "cut.mp4"
        self.path.write_bytes(b"video")

    def args(self, *, no_transcript=False):
        return SimpleNamespace(
            session_id="sess", last=None, start=0.0, end=5.0,
            precise=False, no_transcript=no_transcript)

    def run_with(self, status, *, no_transcript=False):
        result = SimpleNamespace(path=self.path)
        pipeline = Mock()
        pipeline.snapshot.return_value = result
        pipeline.wait_for_snapshot.return_value = status
        stderr = io.StringIO()
        with patch("vodpipe.cli.Pipeline", return_value=pipeline), \
                redirect_stderr(stderr):
            code = cmd_snapshot(
                self.config, self.args(no_transcript=no_transcript))
        pipeline.shutdown_until_stopped.assert_called_once_with()
        return code, stderr.getvalue(), pipeline

    def test_requested_transcript_failure_is_nonzero_and_diagnostic(self):
        code, diagnostic, _ = self.run_with({
            "cut_status": "done",
            "transcript_status": "error",
            "transcript_error": "provider refused the audio",
        })
        self.assertEqual(code, 1)
        self.assertIn("snapshot transcript failed", diagnostic)
        self.assertIn("provider refused the audio", diagnostic)

    def test_requested_complete_package_returns_zero(self):
        code, diagnostic, _ = self.run_with({
            "cut_status": "done", "transcript_status": "done"})
        self.assertEqual(code, 0)
        self.assertEqual(diagnostic, "")

    def test_requested_transcript_refusal_is_nonzero(self):
        code, diagnostic, _ = self.run_with({
            "cut_status": "done",
            "transcript_status": "skipped",
            "transcript_error": "transcription is unavailable",
        })
        self.assertEqual(code, 1)
        self.assertIn("transcription is unavailable", diagnostic)

    def test_requested_incomplete_transcript_is_nonzero(self):
        code, diagnostic, _ = self.run_with({
            "cut_status": "done",
            "transcript_status": "pending",
        })
        self.assertEqual(code, 1)
        self.assertIn("pending", diagnostic)

    def test_no_transcript_ignores_transcript_state_after_good_cut(self):
        code, diagnostic, pipeline = self.run_with({
            "cut_status": "done",
            "transcript_status": "error",
            "transcript_error": "not requested",
        }, no_transcript=True)
        self.assertEqual(code, 0)
        self.assertEqual(diagnostic, "")
        self.assertFalse(
            pipeline.wait_for_snapshot.call_args.kwargs["require_transcript"])


if __name__ == "__main__":
    unittest.main()
