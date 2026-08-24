"""Lifecycle: job draining, locking, identity, shutdown (AUD-003, 004, 006, 011, 043).

The theme is that stopping the application must not silently lose work that has
already been recorded, and that two things must never record the same channel.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from vodpipe.config import DEFAULTS, Config, deep_merge
from vodpipe.jobs import ACCEPTING, CANCELLED, DONE, DRAINING, STOPPED, JobRunner
from vodpipe.locks import ChannelBusy, ChannelLock
from vodpipe.recorder import Recorder
from vodpipe.state import (
    COMPLETE, FAILED, RECORDING, STARTING, Chunk, Session, SessionStore,
    new_session_id,
)
from vodpipe.util import Tools


class JobDrainTests(unittest.TestCase):
    """AUD-003: queued finalisation work must not be abandoned at shutdown."""

    def test_queued_work_runs_before_the_pool_stops(self):
        runner = JobRunner(workers=1)
        done = []
        for index in range(5):
            runner.submit(f"job{index}", f"job {index}", "test",
                          lambda job, i=index: (time.sleep(0.05), done.append(i)))
        runner.stop(timeout=30)
        self.assertEqual(sorted(done), [0, 1, 2, 3, 4],
                         "every queued job must run before shutdown completes")

    def test_a_slow_running_job_is_waited_for(self):
        runner = JobRunner(workers=1)
        finished = threading.Event()
        runner.submit("slow", "slow", "test",
                      lambda job: (time.sleep(0.6), finished.set()))
        runner.stop(timeout=30)
        self.assertTrue(finished.is_set())

    def test_submissions_after_stop_are_refused(self):
        runner = JobRunner(workers=1)
        runner.stop(timeout=10)
        self.assertEqual(runner.state, STOPPED)
        self.assertIsNone(runner.submit("late", "late", "test", lambda job: None))

    def test_submissions_are_enqueued_atomically_with_acceptance(self):
        """An accepted job cannot land behind shutdown's sentinel."""
        runner = JobRunner(workers=1)
        real_put = runner._queue.put
        enqueue_entered = threading.Event()
        allow_enqueue = threading.Event()
        stop_done = threading.Event()
        ran = threading.Event()
        submitted = []

        def paused_put(item, *args, **kwargs):
            if item is not None:
                enqueue_entered.set()
                allow_enqueue.wait(5)
            return real_put(item, *args, **kwargs)

        runner._queue.put = paused_put
        submitter = threading.Thread(target=lambda: submitted.append(
            runner.submit("racing", "racing", "test", lambda job: ran.set())))
        submitter.start()
        self.assertTrue(enqueue_entered.wait(5))

        stopper = threading.Thread(
            target=lambda: (runner.stop(timeout=5), stop_done.set()))
        stopper.start()
        try:
            completed_before_enqueue = stop_done.wait(0.1)
        finally:
            allow_enqueue.set()
        submitter.join(5)
        stopper.join(5)

        self.assertFalse(completed_before_enqueue)
        self.assertEqual(len(submitted), 1)
        self.assertIsNotNone(submitted[0])
        self.assertTrue(ran.is_set())
        self.assertEqual(runner.state, STOPPED)

    def test_a_submission_refused_while_draining_returns_none(self):
        runner = JobRunner(workers=1)
        started = threading.Event()
        release = threading.Event()
        runner.submit("running", "running", "test",
                      lambda job: (started.set(), release.wait(5)))
        self.assertTrue(started.wait(5))
        self.assertFalse(runner.drain(timeout=0))
        self.assertEqual(runner.state, DRAINING)
        self.assertIsNone(runner.submit("late", "late", "test", lambda job: None))
        release.set()
        runner.stop(timeout=5)
        self.assertEqual(runner.state, STOPPED)

    def test_state_progresses_and_is_idempotent(self):
        runner = JobRunner(workers=1)
        self.assertEqual(runner.state, ACCEPTING)
        runner.stop(timeout=10)
        runner.stop(timeout=10)          # must not hang or raise
        self.assertEqual(runner.state, STOPPED)

    def test_forced_stop_marks_leftovers_cancelled_not_queued(self):
        """A job that cannot finish in time must say so, not look pending."""
        runner = JobRunner(workers=1)
        release = threading.Event()
        runner.submit("blocker", "blocker", "test",
                      lambda job: release.wait(20))
        for index in range(3):
            runner.submit(f"waiting{index}", "waiting", "test", lambda job: None)

        time.sleep(0.2)
        runner.stop(timeout=0.5)          # deadline expires while blocked
        release.set()
        runner.stop(timeout=5, drain=False)

        states = {job["key"]: job["status"] for job in runner.snapshot()}
        leftovers = [key for key in states if key.startswith("waiting")]
        for key in leftovers:
            self.assertEqual(states[key], CANCELLED, key)

    def test_forced_stop_cancels_before_sentinels_and_stays_draining(self):
        runner = JobRunner(workers=1)
        started = threading.Event()
        release = threading.Event()
        ran = []
        runner.submit("blocker", "blocker", "test",
                      lambda job: (started.set(), release.wait(5)))
        self.assertTrue(started.wait(5))
        runner.submit("waiting", "waiting", "test", lambda job: ran.append(1))

        events = []
        real_get_nowait = runner._queue.get_nowait
        real_put = runner._queue.put

        def observed_get_nowait():
            item = real_get_nowait()
            if item is not None:
                events.append("cancel")
            return item

        def observed_put(item, *args, **kwargs):
            if item is None:
                events.append("sentinel")
            return real_put(item, *args, **kwargs)

        runner._queue.get_nowait = observed_get_nowait
        runner._queue.put = observed_put
        runner.stop(timeout=0.05, drain=False)

        self.assertEqual(events[:2], ["cancel", "sentinel"])
        self.assertEqual(runner.get("waiting").status, CANCELLED)
        self.assertEqual(ran, [])
        self.assertEqual(runner.state, DRAINING)
        self.assertTrue(any(thread.is_alive() for thread in runner._threads))

        release.set()
        runner.stop(timeout=5, drain=False)
        self.assertEqual(runner.state, STOPPED)
        self.assertFalse(any(thread.is_alive() for thread in runner._threads))

    def test_duplicate_keys_are_not_queued_twice(self):
        runner = JobRunner(workers=1)
        calls = []
        gate = threading.Event()
        runner.submit("k", "k", "test", lambda job: (gate.wait(5), calls.append(1)))
        self.assertIsNone(runner.submit("k", "k", "test", lambda job: calls.append(2)))
        gate.set()
        runner.stop(timeout=20)
        self.assertEqual(calls, [1])

    def test_failing_job_does_not_take_the_pool_down(self):
        runner = JobRunner(workers=1)
        after = []
        runner.submit("bad", "bad", "test", lambda job: (_ for _ in ()).throw(
            RuntimeError("boom")))
        runner.submit("good", "good", "test", lambda job: after.append(1))
        runner.stop(timeout=20)
        self.assertEqual(after, [1])
        self.assertEqual(runner.get("bad").status, "failed")
        self.assertEqual(runner.get("good").status, DONE)


def recorder_config(root: Path) -> Config:
    return Config(deep_merge(DEFAULTS, {
        "paths": {
            "masters_root": str(root / "masters"),
            "work_root": str(root / "work"),
            "censor_master_list": str(root / "none.txt"),
        },
        "recording": {
            "free_space_floor_gb": 0,
            "hard_reserve_gb": 0,
            "ffmpeg_grace_seconds": 0,
        },
    }), root / "config.json")


class RecorderLifecycleTests(unittest.TestCase):
    """A failed recorder operation must never retain its lock or children."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="vodpipe-rec-life-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.config = recorder_config(self.tmp)
        self.store = SessionStore(self.config.masters_root)

    def recorder(self) -> Recorder:
        return Recorder(
            self.config,
            Tools(ffmpeg="ffmpeg", ffprobe="ffprobe",
                  streamlink="streamlink", claude=None),
            self.store, "chan")

    def assert_channel_released(self, recorder: Recorder) -> None:
        self.assertIsNone(recorder._lock)
        ChannelLock(self.config.masters_root, "chan").acquire().release()

    def test_store_add_failure_releases_the_channel(self):
        recorder = self.recorder()
        with mock.patch.object(self.store, "add",
                               side_effect=RuntimeError("start persistence failed")):
            with self.assertRaisesRegex(RuntimeError, "start persistence failed"):
                recorder.start()
        self.assert_channel_released(recorder)

    def test_first_media_atomically_transitions_starting_manifest(self):
        callbacks = []
        recorder = Recorder(
            self.config,
            Tools(ffmpeg="ffmpeg", ffprobe="ffprobe",
                  streamlink="streamlink", claude=None),
            self.store, "chan", request_token="request-token-1234",
            on_first_media=lambda session, token: callbacks.append(
                (session.session_id, token)))
        with mock.patch("vodpipe.recorder.threading.Thread.start", return_value=None):
            session = recorder.start()
        self.addCleanup(recorder._release_lock)
        chunk = recorder._open_chunk(0, 0.0)
        media = session.path / "live" / chunk.ts_name
        media.write_bytes(b"media")

        self.assertEqual(session.status, STARTING)
        self.assertEqual(chunk.status, STARTING)
        self.assertFalse(recorder._startup_expired(session.path / "live"))

        persisted = json.loads((session.path / "session.json").read_text(
            encoding="utf-8"))
        self.assertEqual(session.status, RECORDING)
        self.assertEqual(chunk.status, RECORDING)
        self.assertEqual(persisted["status"], RECORDING)
        self.assertEqual(persisted["chunks"][0]["status"], RECORDING)
        self.assertEqual(callbacks, [(session.session_id, "request-token-1234")])

    def test_an_unopened_successor_row_does_not_land_at_offset_zero(self):
        recorder = self.recorder()
        with mock.patch("vodpipe.recorder.threading.Thread.start", return_value=None):
            session = recorder.start()
        self.addCleanup(recorder._release_lock)
        first = recorder._open_chunk(0, 0.0)
        self.store.update_chunk(session, first, duration=7200.0, status="complete")
        (session.path / "live" / f"{session.channel}_{session.session_id}_c001.ts"
         ).write_bytes(b"media")

        recorder._handle_segment_row(1, "c001.ts", 7200.0, 7300.0)

        successor = session.chunk(1)
        self.assertIsNotNone(successor)
        self.assertAlmostEqual(successor.session_offset, 7200.0)
        self.assertAlmostEqual(successor.duration, 100.0)

    def test_first_media_state_retries_and_callback_failure_is_nonfatal(self):
        callback_calls = []

        def broken_callback(session, token):
            callback_calls.append((session.session_id, token))
            raise RuntimeError("control store unavailable")

        recorder = Recorder(
            self.config,
            Tools(ffmpeg="ffmpeg", ffprobe="ffprobe",
                  streamlink="streamlink", claude=None),
            self.store, "chan", request_token="request-token-1234",
            on_first_media=broken_callback)
        with mock.patch("vodpipe.recorder.threading.Thread.start", return_value=None):
            session = recorder.start()
        self.addCleanup(recorder._release_lock)
        chunk = recorder._open_chunk(0, 0.0)
        (session.path / "live" / chunk.ts_name).write_bytes(b"media")

        real_confirm = self.store.confirm_first_media
        attempts = 0

        def fail_once(target):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise OSError("manifest temporarily unavailable")
            real_confirm(target)

        with mock.patch.object(self.store, "confirm_first_media",
                               side_effect=fail_once):
            self.assertTrue(recorder._observe_first_media(session.path / "live"))
            self.assertGreater(recorder._first_bytes_at, 0)
            self.assertEqual(session.status, STARTING)
            self.assertFalse(recorder._stop.is_set())

            # A later tick retries only the manifest transition. The control
            # callback is one-shot even when it fails.
            self.assertTrue(recorder._observe_first_media(session.path / "live"))

        self.assertEqual(attempts, 2)
        self.assertEqual(session.status, RECORDING)
        self.assertEqual(chunk.status, RECORDING)
        self.assertEqual(callback_calls,
                         [(session.session_id, "request-token-1234")])
        self.assertIn("first-media state update failed", recorder._stderr_tail)
        self.assertIn("first-media callback failed", recorder._stderr_tail)
        self.assertEqual(recorder._fatal_detail(), "")

    def test_zero_byte_startup_finishes_failed_not_complete(self):
        recorder = self.recorder()
        directory = self.tmp / "masters" / "chan" / "empty-start"
        (directory / "live").mkdir(parents=True)
        session = self.store.add(Session(
            session_id="empty-start", channel="chan", started_at=time.time(),
            directory=str(directory), status=STARTING))
        chunk = self.store.add_chunk(session, Chunk(
            index=0, session_id=session.session_id, channel="chan",
            started_at=time.time(), ts_name="chan_c000.ts", status=STARTING))
        recorder.session = session
        recorder._stop_reason = "no video arrived within 1s"

        with mock.patch("vodpipe.recorder.time.sleep", return_value=None):
            recorder._finish("")

        self.assertEqual(session.status, FAILED)
        self.assertIn("no video", session.error)
        self.assertEqual(chunk.status, FAILED)
        self.assertIn("no data", chunk.master_error)

    def test_quality_refusal_is_a_fatal_startup_error(self):
        self.config.set("recording.min_height", 1080)
        self.config.set("recording.on_low_quality", "refuse")
        recorder = self.recorder()
        directory = self.tmp / "masters" / "chan" / "quality-start"
        directory.mkdir(parents=True)
        recorder.session = self.store.add(Session(
            session_id="quality-start", channel="chan", started_at=time.time(),
            directory=str(directory), status=STARTING))

        recorder._note_quality(
            "[cli][info] Opening stream: 720p60 (hls)")

        self.assertTrue(recorder._stop.is_set())
        self.assertIn("refusing to record", recorder._fatal_detail())

    def test_recorder_thread_start_failure_is_persisted_and_releases(self):
        recorder = self.recorder()
        with mock.patch("vodpipe.recorder.threading.Thread.start",
                        side_effect=RuntimeError("thread failed")):
            with self.assertRaisesRegex(RuntimeError, "thread failed"):
                recorder.start()
        self.assertEqual(recorder.session.status, FAILED)
        self.assertIn("thread failed", recorder.session.error)
        self.assert_channel_released(recorder)

    def test_command_construction_failure_finishes_and_releases(self):
        recorder = self.recorder()
        with mock.patch("vodpipe.recorder.streamlink_command",
                        side_effect=RuntimeError("command failed")), \
                mock.patch("vodpipe.recorder.time.sleep", return_value=None):
            session = recorder.start()
            recorder.join(5)
        self.assertFalse(recorder.running)
        self.assertEqual(session.status, FAILED)
        self.assertIn("command failed", session.error)
        self.assert_channel_released(recorder)

    def test_segment_list_setup_failure_finishes_and_releases(self):
        recorder = self.recorder()
        real_write_text = Path.write_text

        def fail_segment_list(path, *args, **kwargs):
            if path.name == "segments.csv":
                raise OSError("segment list failed")
            return real_write_text(path, *args, **kwargs)

        with mock.patch.object(Path, "write_text", new=fail_segment_list), \
                mock.patch("vodpipe.recorder.time.sleep", return_value=None):
            session = recorder.start()
            recorder.join(5)
        self.assertFalse(recorder.running)
        self.assertEqual(session.status, FAILED)
        self.assertIn("segment list failed", session.error)
        self.assert_channel_released(recorder)

    def test_ffmpeg_spawn_failure_reaps_streamlink_and_closes_pipes(self):
        recorder = self.recorder()

        class Process:
            def __init__(self):
                self.returncode = None
                self.stdout = io.BytesIO()
                self.stderr = io.BytesIO()
                self.stdin = None
                self.killed = False
                self.waited = False

            def poll(self):
                return self.returncode

            def kill(self):
                self.killed = True
                self.returncode = -9

            def wait(self, timeout=None):
                self.waited = True
                return self.returncode

        producer = Process()
        with mock.patch("vodpipe.recorder.popen",
                        side_effect=[producer, RuntimeError("ffmpeg spawn failed")]), \
                mock.patch("vodpipe.recorder.time.sleep", return_value=None):
            session = recorder.start()
            recorder.join(5)

        self.assertEqual(session.status, FAILED)
        self.assertTrue(producer.killed)
        self.assertTrue(producer.waited)
        self.assertTrue(producer.stdout.closed)
        self.assertTrue(producer.stderr.closed)
        self.assert_channel_released(recorder)

    def test_finish_persistence_failure_still_releases_the_channel(self):
        recorder = self.recorder()
        root = self.config.masters_root
        root.mkdir(parents=True, exist_ok=True)
        recorder._lock = ChannelLock(root, "chan").acquire()
        directory = root / "chan" / "session"
        directory.mkdir(parents=True)
        recorder.session = self.store.add(Session(
            session_id="session", channel="chan", started_at=time.time(),
            directory=str(directory)))
        recorder._first_bytes_at = time.time()
        callbacks = []
        recorder.on_session_ended = lambda session: callbacks.append(session.status)

        with mock.patch.object(recorder, "_finalize_remaining_chunks"), \
                mock.patch("vodpipe.recorder.time.sleep", return_value=None), \
                mock.patch.object(self.store, "update",
                                  side_effect=RuntimeError("finish persistence failed")):
            recorder._finish("")

        self.assertEqual(recorder.session.status, FAILED)
        self.assertIn("could not persist final session state", recorder.session.error)
        persisted = json.loads((directory / "session.json").read_text(
            encoding="utf-8"))
        self.assertEqual(persisted["status"], FAILED)
        self.assertIn("finish persistence failed", persisted["error"])
        self.assertEqual(callbacks, [FAILED])
        self.assert_channel_released(recorder)

    def test_final_reconciliation_failure_still_persists_and_calls_back(self):
        recorder = self.recorder()
        root = self.config.masters_root
        root.mkdir(parents=True, exist_ok=True)
        recorder._lock = ChannelLock(root, "chan").acquire()
        directory = root / "chan" / "reconcile"
        directory.mkdir(parents=True)
        session = self.store.add(Session(
            session_id="reconcile", channel="chan", started_at=time.time(),
            directory=str(directory), status=RECORDING))
        recorder.session = session
        recorder._first_bytes_at = time.time()
        callbacks = []

        def ended(target):
            # The callback runs after release, so a successor can claim channel.
            ChannelLock(root, "chan").acquire().release()
            callbacks.append((target.status, target.error))

        recorder.on_session_ended = ended
        with mock.patch.object(
                recorder, "_finalize_remaining_chunks",
                side_effect=RuntimeError("reconciliation exploded")), \
                mock.patch("vodpipe.recorder.time.sleep", return_value=None):
            recorder._finish("")

        self.assertEqual(session.status, FAILED)
        self.assertGreater(session.ended_at, 0)
        self.assertIn("final chunk reconciliation failed", session.error)
        self.assertIn("reconciliation exploded", session.error)
        self.assertEqual(callbacks, [(FAILED, session.error)])
        self.assert_channel_released(recorder)

    def test_clean_user_and_shutdown_stops_complete_without_an_error(self):
        for reason in ("stopped by user", "stopped: shutting down"):
            with self.subTest(reason=reason):
                recorder = self._session_recorder()
                recorder._first_bytes_at = time.time()
                recorder._stop_reason = reason
                with mock.patch.object(recorder, "_finalize_remaining_chunks"), \
                        mock.patch("vodpipe.recorder.time.sleep", return_value=None):
                    recorder._finish("")
                self.assertEqual(recorder.session.status, COMPLETE)
                self.assertEqual(recorder.session.error, "")

    def test_graceful_shutdown_stops_producer_before_waiting_for_ffmpeg(self):
        recorder = self.recorder()
        events = []

        class Process:
            def __init__(self, name):
                self.name = name
                self.returncode = None

            def poll(self):
                return self.returncode

            def terminate(self):
                events.append(f"terminate-{self.name}")

            def wait(self, timeout=None):
                events.append(f"wait-{self.name}")
                self.returncode = 0
                return 0

            def kill(self):
                events.append(f"kill-{self.name}")
                self.returncode = -9

        recorder._streamlink = Process("streamlink")
        recorder._ffmpeg = Process("ffmpeg")
        self.assertEqual(recorder._shutdown_processes(), "")
        self.assertEqual(events, [
            "terminate-streamlink", "wait-ffmpeg", "wait-streamlink"])

    def _session_recorder(self) -> Recorder:
        recorder = self.recorder()
        directory = self.tmp / "masters" / "chan" / "session"
        (directory / "logs").mkdir(parents=True, exist_ok=True)
        recorder.session = self.store.add(Session(
            session_id="session", channel="chan", started_at=time.time(),
            directory=str(directory)))
        return recorder

    def test_both_stderr_pumps_keep_draining_after_log_write_failure(self):
        class TrackingStream(io.BytesIO):
            def close(self):
                self.closed_by_pump = True

        class BrokenSink:
            def write(self, _text):
                raise OSError("log disk failed")

            def flush(self):
                pass

            def close(self):
                pass

        payload = b"first\nAuthorization=OAuth secret-value\nlast\n"
        for source in ("streamlink", "ffmpeg"):
            with self.subTest(source=source):
                recorder = self._session_recorder()
                live = recorder.session.path / "live"
                live.mkdir(exist_ok=True)
                (live / "chan_session_c000.ts").write_bytes(b"media")
                recorder._first_bytes_at = time.time()
                stderr = TrackingStream(payload)

                class Process:
                    pass

                process = Process()
                process.stderr = stderr
                setattr(recorder, f"_{source}", process)
                log_name = f"{source}.log"
                real_open = Path.open

                def fail_log(path, *args, **kwargs):
                    if path.name == log_name:
                        return BrokenSink()
                    return real_open(path, *args, **kwargs)

                with mock.patch.object(Path, "open", new=fail_log):
                    getattr(recorder, f"_pump_{source}_log")()

                self.assertEqual(stderr.tell(), len(payload),
                                 "a failed sink must not stop pipe drainage")
                self.assertIn(f"{source} stderr pump could not write log",
                              recorder._stderr_tail)
                self.assertEqual(recorder._fatal_detail(), "")
                self.assertFalse(recorder._stop.is_set())
                self.assertNotIn("secret-value", recorder._stderr_tail)
                self.assertIn("<redacted>", recorder._stderr_tail)
                recorder._append_stderr(source, "x" * 20_000)
                self.assertLessEqual(len(recorder._stderr_tail), 16 * 1024)

    def test_streamlink_pump_drains_after_quality_state_failure(self):
        recorder = self._session_recorder()
        live = recorder.session.path / "live"
        live.mkdir(exist_ok=True)
        (live / "chan_session_c000.ts").write_bytes(b"media")
        recorder._first_bytes_at = time.time()
        payload = (b"[cli][info] Opening stream: 1080p60 (hls)\n"
                   b"line after failed state update\n")

        class TrackingStream(io.BytesIO):
            def close(self):
                self.closed_by_pump = True

        class Process:
            stderr = TrackingStream(payload)

        recorder._streamlink = Process()
        with mock.patch.object(self.store, "update",
                               side_effect=OSError("manifest write failed")):
            recorder._pump_streamlink_log()

        self.assertEqual(recorder._streamlink.stderr.tell(), len(payload))
        self.assertIn("line after failed state update", recorder._stderr_tail)
        self.assertIn("quality metadata update failed", recorder._stderr_tail)
        self.assertEqual(recorder._fatal_detail(), "")
        self.assertFalse(recorder._stop.is_set())

    def test_ad_metadata_failure_is_diagnostic_only(self):
        recorder = self._session_recorder()
        with mock.patch.object(
                self.store, "add_ad_event",
                side_effect=OSError("ad manifest unavailable")):
            recorder._note_ad_event("Detected advertisement break of 30 seconds")

        self.assertFalse(recorder._stop.is_set())
        self.assertEqual(recorder._fatal_detail(), "")
        self.assertIn("ad metadata update failed", recorder._stderr_tail)
        self.assertIn("ad manifest unavailable", recorder._stderr_tail)

    def test_quality_store_failure_does_not_stop_capture_and_children_are_reaped(self):
        recorder = self.recorder()
        callbacks = []
        recorder.on_session_ended = lambda session: callbacks.append(session.status)
        processes = []

        class Process:
            def __init__(self, stderr=b"", returncode=0):
                self.returncode = returncode
                self.stdout = io.BytesIO()
                self.stderr = io.BytesIO(stderr)
                self.stdin = None
                self.terminated = False
                self.waited = False

            def poll(self):
                return self.returncode

            def terminate(self):
                self.terminated = True
                self.returncode = 0

            def wait(self, timeout=None):
                self.waited = True
                if self.returncode is None:
                    self.returncode = 0
                return self.returncode

            def kill(self):
                self.returncode = -9

        streamlink = Process(
            b"[cli][info] Opening stream: 1080p60 (hls)\n"
            b"drained after failure\n")
        ffmpeg = Process()
        processes.extend((streamlink, ffmpeg))

        real_update = self.store.update

        def fail_quality(session, **changes):
            if "quality_selected" in changes:
                raise OSError("manifest unavailable")
            return real_update(session, **changes)

        def spawn(_cmd, **_kwargs):
            process = processes.pop(0)
            if process is ffmpeg:
                session = recorder.session
                assert session is not None
                name = f"chan_{session.session_id}_c000.ts"
                (session.path / "live" / name).write_bytes(b"media")
                recorder._first_bytes_at = time.time()
            return process

        with mock.patch("vodpipe.recorder.popen", side_effect=spawn), \
                mock.patch.object(self.store, "update", side_effect=fail_quality), \
                mock.patch.object(recorder, "_finalize_remaining_chunks"), \
                mock.patch("vodpipe.recorder.time.sleep", return_value=None):
            session = recorder.start()
            recorder.join(5)

        self.assertFalse(recorder.running)
        self.assertEqual(session.status, COMPLETE)
        self.assertEqual(session.error, "")
        self.assertEqual(callbacks, [COMPLETE])
        self.assertIn("drained after failure", recorder._stderr_tail)
        self.assertIn("quality metadata update failed", recorder._stderr_tail)
        self.assertEqual(recorder._fatal_detail(), "")
        for process in (streamlink, ffmpeg):
            self.assertTrue(process.waited)
            self.assertIsNotNone(process.returncode)
        self.assert_channel_released(recorder)

    def test_stderr_read_failure_stops_reaps_and_fails_the_attempt(self):
        recorder = self.recorder()
        callbacks = []
        recorder.on_session_ended = lambda session: callbacks.append(session.status)
        processes = []

        class BrokenRead(io.BytesIO):
            def readline(self, *args, **kwargs):
                raise OSError("stderr pipe broke")

        class Process:
            def __init__(self, stderr):
                self.returncode = None
                self.stdout = io.BytesIO()
                self.stderr = stderr
                self.stdin = None
                self.terminated = False
                self.waited = False

            def poll(self):
                return self.returncode

            def terminate(self):
                self.terminated = True
                self.returncode = 0

            def wait(self, timeout=None):
                self.waited = True
                if self.returncode is None:
                    self.returncode = 0
                return self.returncode

            def kill(self):
                self.returncode = -9

        streamlink = Process(BrokenRead())
        ffmpeg = Process(io.BytesIO())
        processes.extend((streamlink, ffmpeg))

        def spawn(_cmd, **_kwargs):
            process = processes.pop(0)
            if process is ffmpeg:
                session = recorder.session
                assert session is not None
                name = f"chan_{session.session_id}_c000.ts"
                (session.path / "live" / name).write_bytes(b"media")
                recorder._first_bytes_at = time.time()
            return process

        with mock.patch("vodpipe.recorder.popen", side_effect=spawn), \
                mock.patch.object(recorder, "_finalize_remaining_chunks"), \
                mock.patch("vodpipe.recorder.time.sleep", return_value=None):
            session = recorder.start()
            recorder.join(5)

        self.assertFalse(recorder.running)
        self.assertEqual(session.status, FAILED)
        self.assertIn("could not read stderr", session.error)
        self.assertIn("stderr pipe broke", session.error)
        self.assertEqual(callbacks, [FAILED])
        for process in (streamlink, ffmpeg):
            self.assertTrue(process.waited)
            self.assertIsNotNone(process.returncode)
        self.assertTrue(streamlink.terminated)
        self.assert_channel_released(recorder)

    def test_established_media_stagnation_is_fatal(self):
        recorder = self._session_recorder()
        live = recorder.session.path / "live"
        live.mkdir()
        segment = live / "chan_session_c000.ts"
        segment.write_bytes(b"media")
        recorder._first_bytes_at = time.time()

        with mock.patch("vodpipe.recorder.time.monotonic", return_value=100.0):
            self.assertFalse(recorder._stagnation_expired(live))
        with mock.patch("vodpipe.recorder.time.monotonic", return_value=401.0):
            self.assertTrue(recorder._stagnation_expired(live))

        self.assertTrue(recorder._stop.is_set())
        self.assertIn("write head did not advance", recorder._fatal_detail())

    def test_segment_rollover_resets_stagnation_watchdog(self):
        recorder = self._session_recorder()
        live = recorder.session.path / "live"
        live.mkdir()
        (live / "chan_session_c000.ts").write_bytes(b"first segment")
        recorder._first_bytes_at = time.time()

        with mock.patch("vodpipe.recorder.time.monotonic", return_value=100.0):
            self.assertFalse(recorder._stagnation_expired(live))
        (live / "chan_session_c001.ts").write_bytes(b"next")
        with mock.patch("vodpipe.recorder.time.monotonic", return_value=399.0):
            self.assertFalse(recorder._stagnation_expired(live))
        with mock.patch("vodpipe.recorder.time.monotonic", return_value=500.0):
            self.assertFalse(recorder._stagnation_expired(live))

        self.assertFalse(recorder._stop.is_set())


class ChannelLockTests(unittest.TestCase):
    """AUD-011: one recorder per channel, across processes as well as threads."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="vodpipe-lock-"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_second_acquire_is_refused(self):
        first = ChannelLock(self.tmp, "somechannel").acquire()
        try:
            with self.assertRaises(ChannelBusy):
                ChannelLock(self.tmp, "somechannel").acquire()
        finally:
            first.release()

    def test_release_allows_reacquisition(self):
        ChannelLock(self.tmp, "chan").acquire().release()
        second = ChannelLock(self.tmp, "chan").acquire()
        second.release()

    def test_different_channels_do_not_block_each_other(self):
        a = ChannelLock(self.tmp, "one").acquire()
        b = ChannelLock(self.tmp, "two").acquire()
        a.release()
        b.release()

    def test_an_abandoned_lock_file_does_not_block(self):
        """A crash must not lock a channel out forever.

        Ownership is a kernel byte-range lock on an open descriptor, so a file
        left behind by a dead process holds nothing. The pid inside it is
        diagnostics and is deliberately not consulted -- pids get reused, and the
        old design's `os.kill(pid, 0)` probe would call a recycled pid "alive"
        and lock the channel out permanently.
        """
        directory = self.tmp / ".locks"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "ghost.lock").write_text(
            '{"pid": 999999998, "channel": "ghost", "acquired_at": 0}',
            encoding="utf-8")
        ChannelLock(self.tmp, "ghost").acquire().release()

    def test_a_lock_file_naming_a_live_pid_still_does_not_block(self):
        """Contents never confer ownership -- only a held descriptor does.

        This file names *this* running process, which the old pid-probing
        implementation treated as a live holder. Nothing holds a kernel lock on
        it, so the channel is genuinely free and must be acquirable.
        """
        directory = self.tmp / ".locks"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "mine.lock").write_text(
            f'{{"pid": {os.getpid()}, "channel": "mine", "acquired_at": 0}}',
            encoding="utf-8")
        ChannelLock(self.tmp, "mine").acquire().release()

    def test_corrupt_lock_file_is_reclaimed(self):
        directory = self.tmp / ".locks"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "junk.lock").write_text("not json", encoding="utf-8")
        ChannelLock(self.tmp, "junk").acquire().release()

    def test_release_cannot_remove_a_successors_lock(self):
        """`release()` used to unlink by path, taking the next holder's lock."""
        first = ChannelLock(self.tmp, "chan").acquire()
        first.release()
        second = ChannelLock(self.tmp, "chan").acquire()
        try:
            # The already-released first lock must be inert.
            first.release()
            with self.assertRaises(ChannelBusy):
                ChannelLock(self.tmp, "chan").acquire()
        finally:
            second.release()


class CrossProcessChannelLockTests(unittest.TestCase):
    """AUD2-001: exclusion has to hold across processes, not just threads.

    Threads share a descriptor table, so a thread-level test cannot distinguish
    a real kernel lock from an in-process one. These use a real child process.
    """

    HOLDER = (
        "import sys, time, pathlib\n"
        "sys.path.insert(0, {repo!r})\n"
        "from vodpipe.locks import ChannelLock\n"
        "ChannelLock(pathlib.Path({root!r}), 'chan').acquire()\n"
        "print('HELD', flush=True)\n"
        "time.sleep(60)\n"
    )

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="vodpipe-xlock-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.proc = None

    def start_holder(self):
        repo = str(Path(__file__).resolve().parent.parent)
        script = self.tmp / "holder.py"
        script.write_text(self.HOLDER.format(repo=repo, root=str(self.tmp)),
                          encoding="utf-8")
        self.proc = subprocess.Popen(
            [sys.executable, str(script)], stdout=subprocess.PIPE, text=True)
        self.addCleanup(self._kill)
        ready = self.proc.stdout.readline().strip()
        self.assertEqual(ready, "HELD", "the holder process did not start")

    def _kill(self):
        if not self.proc:
            return
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(timeout=10)
        if self.proc.stdout:
            self.proc.stdout.close()

    def test_another_process_cannot_take_a_held_channel(self):
        self.start_holder()
        with self.assertRaises(ChannelBusy):
            ChannelLock(self.tmp, "chan").acquire()

    def test_a_different_channel_is_unaffected(self):
        self.start_holder()
        ChannelLock(self.tmp, "other").acquire().release()

    def test_killing_the_holder_frees_the_channel(self):
        """The OS releases the lock on exit, so staleness needs no detection."""
        self.start_holder()
        self.proc.kill()
        self.proc.wait(timeout=10)
        deadline = time.time() + 10
        while True:
            try:
                ChannelLock(self.tmp, "chan").acquire().release()
                return
            except ChannelBusy:
                if time.time() > deadline:
                    self.fail("a killed holder locked the channel out")
                time.sleep(0.1)

    def test_context_manager_releases(self):
        with ChannelLock(self.tmp, "ctx"):
            pass
        ChannelLock(self.tmp, "ctx").acquire().release()

    def test_concurrent_acquires_yield_exactly_one_winner(self):
        winners, losers = [], []
        barrier = threading.Barrier(8)

        def attempt():
            barrier.wait()
            try:
                lock = ChannelLock(self.tmp, "race").acquire()
            except ChannelBusy:
                losers.append(1)
            else:
                winners.append(lock)

        threads = [threading.Thread(target=attempt) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)

        self.assertEqual(len(winners), 1, "exactly one thread may hold the channel")
        self.assertEqual(len(losers), 7)
        winners[0].release()


class SessionIdentityTests(unittest.TestCase):
    """AUD-011: one-second resolution ids collided on a fast stop/restart."""

    def test_ids_differ_within_the_same_second(self):
        moment = time.time()
        ids = {new_session_id("chan", moment) for _ in range(200)}
        self.assertEqual(len(ids), 200)

    def test_id_keeps_the_channel_and_a_readable_timestamp(self):
        value = new_session_id("somechannel", time.time())
        self.assertTrue(value.startswith("somechannel_"))
        self.assertRegex(value, r"^somechannel_\d{4}-\d{2}-\d{2}_\d{6}_[0-9a-f]{6}$")

    def test_id_is_filename_safe(self):
        for character in '<>:"/\\|?*':
            self.assertNotIn(character, new_session_id("chan"))


if __name__ == "__main__":
    unittest.main()
