"""Crash recovery and disk safety (AUD-007, AUD-012).

The defect these cover: a crash left a perfectly good .ts on disk with no master,
and every restart merely relabelled the session in memory while the file sat there
forever. Recovery has to actually finish the work, and doing it twice must be a
no-op.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import time
import unittest
from pathlib import Path

from vodpipe.config import DEFAULTS, Config, deep_merge
from vodpipe.pipeline import Pipeline
from vodpipe.state import Chunk, Session, SessionStore
from vodpipe.util import resolve_tools, run


def make_config(root: Path) -> Config:
    data = deep_merge(DEFAULTS, {
        "paths": {"masters_root": str(root / "masters"),
                  "work_root": str(root / "work"),
                  "censor_master_list": str(root / "none.txt")},
        "recording": {"free_space_floor_gb": 0.001, "hard_reserve_gb": 0.0005},
        "proxies": {"enabled": False},
        "transcription": {"enabled": False},
        "summary": {"enabled": False},
        "watcher": {"enabled": False},
        "dashboard": {"open_browser": False},
    })
    return Config(data, root / "config.json")


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="vodpipe-recover-"))
        self.config = make_config(self.tmp)
        self.tools = resolve_tools()
        self.session_dir = (self.config.masters_root / "chan" /
                            "chan_2026-01-01_000000_abc123")
        for sub in ("live", "master", "transcripts", "snapshots", "logs"):
            (self.session_dir / sub).mkdir(parents=True)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write_ts(self, name: str, seconds: int = 4) -> Path:
        path = self.session_dir / "live" / name
        run([self.tools.ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
             "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=30",
             "-f", "lavfi", "-i", "sine=frequency=440",
             "-t", str(seconds), "-c:v", "libx264", "-preset", "ultrafast",
             "-g", "30", "-c:a", "aac", "-f", "mpegts", str(path)],
            check=True, timeout=180)
        return path

    def write_state(self, chunk_status: str, session_status: str = "recording",
                    duration: float = 4.0) -> None:
        session = Session(session_id=self.session_dir.name, channel="chan",
                          started_at=time.time(),
                          directory=str(self.session_dir), status=session_status)
        session.chunks.append(Chunk(
            index=0, session_id=session.session_id, channel="chan",
            started_at=time.time(), ts_name="chan_c000.ts",
            master_name="chan_c000.mp4", duration=duration, status=chunk_status))
        (self.session_dir / "session.json").write_text(
            json.dumps(session.to_dict()), encoding="utf-8")

    def drain(self, pipeline: Pipeline) -> None:
        deadline = time.time() + 180
        while pipeline.jobs.active_count() and time.time() < deadline:
            time.sleep(0.5)

    # -- persistence -------------------------------------------------------

    def test_loading_does_not_touch_disk(self):
        """AUD2-002: loading used to rewrite a session that might still be live.

        `load_from_disk()` assumed any `recording` session had lost its owner and
        persisted `interrupted` immediately. Opening a CLI snapshot while the
        dashboard was recording therefore relabelled the dashboard's live
        session, and could go on to remux and reclaim a `.ts` still being
        appended to. Deciding a session is abandoned needs the channel lock, so
        it belongs to recovery, not to loading.
        """
        self.write_ts("chan_c000.ts")
        self.write_state(chunk_status="recording", session_status="recording")

        store = SessionStore(self.config.masters_root)
        store.load_from_disk()

        on_disk = json.loads((self.session_dir / "session.json")
                             .read_text(encoding="utf-8"))
        self.assertEqual(on_disk["status"], "recording",
                         "loading must not rewrite state it does not own")
        self.assertEqual(store.get(self.session_dir.name).status, "recording")

    def test_interrupted_state_is_written_back_during_recovery(self):
        """Relabelling only in memory made every restart repeat the same work."""
        self.write_ts("chan_c000.ts")
        self.write_state(chunk_status="recording", session_status="recording")

        pipeline = Pipeline(self.config)
        pipeline.recover()

        on_disk = json.loads((self.session_dir / "session.json")
                             .read_text(encoding="utf-8"))
        self.assertEqual(on_disk["status"], "interrupted",
                         "the correction must be persisted, not just in memory")

    def test_a_session_another_process_owns_is_left_alone(self):
        """The channel lock is what separates "crashed" from "live elsewhere"."""
        from vodpipe.locks import ChannelLock

        self.write_ts("chan_c000.ts")
        self.write_state(chunk_status="recording", session_status="recording")

        # Stand in for the other process still holding the channel.
        holder = ChannelLock(self.config.masters_root, "chan").acquire()
        try:
            pipeline = Pipeline(self.config)
            actions = pipeline.recover()
        finally:
            holder.release()

        on_disk = json.loads((self.session_dir / "session.json")
                             .read_text(encoding="utf-8"))
        self.assertEqual(on_disk["status"], "recording",
                         "recovery rewrote a session another process owns")
        self.assertTrue(any("another process" in line for line in actions),
                        actions)
        # And the recording it is still writing must survive untouched.
        self.assertTrue((self.session_dir / "live" / "chan_c000.ts").exists())

    def test_running_artifacts_are_reset_during_recovery(self):
        self.write_ts("chan_c000.ts")
        session = Session(session_id=self.session_dir.name, channel="chan",
                          started_at=time.time(),
                          directory=str(self.session_dir), status="complete")
        session.chunks.append(Chunk(
            index=0, session_id=session.session_id, channel="chan",
            started_at=time.time(), ts_name="chan_c000.ts",
            master_name="chan_c000.mp4", duration=4.0, status="complete",
            proxy_status="running", transcript_status="running"))
        (self.session_dir / "session.json").write_text(
            json.dumps(session.to_dict()), encoding="utf-8")

        pipeline = Pipeline(self.config)
        pipeline.recover()
        chunk = pipeline.store.get(self.session_dir.name).chunks[0]
        # Their workers are gone; nothing is running.
        self.assertEqual(chunk.proxy_status, "pending")
        self.assertEqual(chunk.transcript_status, "pending")

    # -- doing the work ----------------------------------------------------

    def test_interrupted_recording_is_remuxed_on_restart(self):
        self.write_ts("chan_c000.ts")
        self.write_state(chunk_status="recording", session_status="recording")

        pipeline = Pipeline(self.config)
        try:
            pipeline.start()
            self.drain(pipeline)
            master = self.session_dir / "master" / "chan_c000.mp4"
            self.assertTrue(master.exists(),
                            "recovered media must actually get a master")
            self.assertGreater(master.stat().st_size, 1000)
        finally:
            pipeline.shutdown()

    def test_chunk_stuck_at_remuxing_is_retried(self):
        self.write_ts("chan_c000.ts")
        self.write_state(chunk_status="remuxing", session_status="complete")

        pipeline = Pipeline(self.config)
        try:
            pipeline.start()
            self.drain(pipeline)
            self.assertTrue((self.session_dir / "master" / "chan_c000.mp4").exists())
        finally:
            pipeline.shutdown()

    def test_second_restart_does_no_further_work(self):
        self.write_ts("chan_c000.ts")
        self.write_state(chunk_status="recording", session_status="recording")

        first = Pipeline(self.config)
        try:
            first.start()
            self.drain(first)
        finally:
            first.shutdown()

        second = Pipeline(self.config)
        try:
            actions = second.recover()
            self.assertEqual(actions, [], f"second restart must be a no-op: {actions}")
        finally:
            second.shutdown()

    def test_stale_partial_output_is_removed(self):
        self.write_ts("chan_c000.ts")
        self.write_state(chunk_status="complete", session_status="complete")
        partial = self.session_dir / "master" / "chan_c000.partial.mp4"
        partial.write_bytes(b"garbage")

        pipeline = Pipeline(self.config)
        try:
            pipeline.recover()
            self.assertFalse(partial.exists(),
                             "a partial remux is never a valid master")
        finally:
            pipeline.shutdown()

    def test_corrupt_master_is_discarded_and_rebuilt(self):
        self.write_ts("chan_c000.ts")
        self.write_state(chunk_status="complete", session_status="complete")
        (self.session_dir / "master" / "chan_c000.mp4").write_bytes(b"\x00" * 4096)

        pipeline = Pipeline(self.config)
        try:
            pipeline.start()
            self.drain(pipeline)
            master = self.session_dir / "master" / "chan_c000.mp4"
            self.assertGreater(master.stat().st_size, 1000)
        finally:
            pipeline.shutdown()

    def test_valid_master_is_adopted_without_rework(self):
        source = self.write_ts("chan_c000.ts")
        master = self.session_dir / "master" / "chan_c000.mp4"
        run([self.tools.ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
             "-i", str(source), "-c", "copy", str(master)], check=True, timeout=120)
        self.write_state(chunk_status="interrupted", session_status="interrupted")
        before = master.stat().st_mtime

        pipeline = Pipeline(self.config)
        try:
            pipeline.recover()
            self.assertEqual(master.stat().st_mtime, before,
                             "a valid master must not be rebuilt")
            chunk = pipeline.store.get(self.session_dir.name).chunks[0]
            self.assertEqual(chunk.status, "complete")
        finally:
            pipeline.shutdown()

    def test_interruption_with_no_media_is_marked_failed(self):
        self.write_state(chunk_status="recording", session_status="recording")

        pipeline = Pipeline(self.config)
        try:
            pipeline.recover()
            chunk = pipeline.store.get(self.session_dir.name).chunks[0]
            self.assertEqual(chunk.status, "failed")
            self.assertIn("no media", chunk.master_error)
        finally:
            pipeline.shutdown()

    def test_interrupted_starting_state_without_media_is_failed_visibly(self):
        self.write_state(chunk_status="starting", session_status="starting")

        pipeline = Pipeline(self.config)
        try:
            pipeline.recover()
            session = pipeline.store.get(self.session_dir.name)
            self.assertEqual(session.status, "failed")
            self.assertIn("before any media", session.error)
            self.assertEqual(session.chunks[0].status, "failed")
            self.assertIn("no media", session.chunks[0].master_error)
        finally:
            pipeline.shutdown()

    def test_interrupted_starting_state_with_media_is_recovered(self):
        self.write_ts("chan_c000.ts")
        self.write_state(chunk_status="starting", session_status="starting")

        pipeline = Pipeline(self.config)
        try:
            pipeline.start()
            self.drain(pipeline)
            session = pipeline.store.get(self.session_dir.name)
            self.assertEqual(session.status, "interrupted")
            self.assertTrue(
                (self.session_dir / "master" / "chan_c000.mp4").exists())
        finally:
            pipeline.shutdown()

    def test_a_live_session_is_left_alone(self):
        """Recovery must not touch a session that is being recorded.

        A live `Recorder` holds the channel lock for the whole session, which is
        what marks the session as owned. Byte-range locks are per descriptor, so
        this holds whether the owner is another process or this one.
        """
        from vodpipe.locks import ChannelLock

        pipeline = Pipeline(self.config)
        held = ChannelLock(self.config.masters_root, "chan").acquire()
        try:
            session = Session(session_id=self.session_dir.name, channel="chan",
                              started_at=time.time(),
                              directory=str(self.session_dir), status="recording")
            pipeline.store.add(session)
            actions = " ".join(pipeline.recover())
            self.assertIn("another process", actions)
            self.assertEqual(
                pipeline.store.get(self.session_dir.name).status, "recording")
        finally:
            held.release()
            pipeline.shutdown()


class DiskGuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="vodpipe-disk-"))
        self.config = make_config(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_post_processing_is_refused_below_the_reserve(self):
        """A proxy must never be the thing that fills the drive."""
        self.config.set("recording.hard_reserve_gb", 1_000_000)
        pipeline = Pipeline(self.config)
        try:
            with self.assertRaises(RuntimeError) as caught:
                pipeline._require_space(1024, "a test job")
            self.assertIn("not enough disk", str(caught.exception))
        finally:
            pipeline.shutdown()

    def test_normal_space_passes(self):
        pipeline = Pipeline(self.config)
        try:
            pipeline._require_space(1024, "a test job")
        finally:
            pipeline.shutdown()


if __name__ == "__main__":
    unittest.main()
