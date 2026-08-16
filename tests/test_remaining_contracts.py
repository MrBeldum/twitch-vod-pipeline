"""Recovery, ownership, shutdown, disk, and lease contract regressions."""

from __future__ import annotations

import json
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from vodpipe.config import DEFAULTS, Config, deep_merge
from vodpipe.disk import DiskBudget
from vodpipe.exports import MANIFEST_NAME, write_exports
from vodpipe.jobs import ACCEPTING, STOPPED as POOL_STOPPED, Job
from vodpipe.locks import ResourceLock, chunk_lock_path
from vodpipe.pipeline import DRAINING, STOPPED, Pipeline
from vodpipe.snapshot import SnapshotRequest, SnapshotResult, SnapshotService
from vodpipe.state import (
    COMPLETE,
    DONE,
    ERROR,
    PENDING,
    RECORDING,
    REMUXING,
    Chunk,
    Session,
)
from vodpipe.transcript import Word, load_words, save_words
from vodpipe.util import Tools


def make_config(root: Path, **overlay) -> Config:
    base = {
        "paths": {
            "masters_root": str(root / "masters"),
            "work_root": str(root / "work"),
            "censor_master_list": str(root / "none.txt"),
        },
        "recording": {"free_space_floor_gb": 0, "hard_reserve_gb": 0},
        "proxies": {"enabled": False},
        "transcription": {"enabled": False},
        "summary": {"enabled": False, "provider": "none"},
        "watcher": {"enabled": False},
    }
    return Config(deep_merge(DEFAULTS, deep_merge(base, overlay)),
                  root / "config.json")


class PipelineFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="vodpipe-contracts-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.config = make_config(self.tmp)
        self.config.masters_root.mkdir(parents=True)
        self.pipeline = Pipeline(self.config)
        self.addCleanup(self.pipeline.shutdown, job_timeout=10)
        self.directory = self.config.masters_root / "chan" / "sess"
        for name in ("live", "master", "transcripts", "snapshots", "logs"):
            (self.directory / name).mkdir(parents=True, exist_ok=True)
        self.session = self.pipeline.store.add(Session(
            session_id="sess", channel="chan", started_at=1.0,
            directory=str(self.directory), status=COMPLETE))

    def chunk(self, index: int = 0, **changes) -> Chunk:
        values = dict(
            index=index, session_id="sess", channel="chan", started_at=1.0,
            ts_name=f"chan_c{index:03d}.ts",
            master_name=f"chan_c{index:03d}.mp4",
            duration=10.0, status=COMPLETE,
        )
        values.update(changes)
        chunk = Chunk(**values)
        self.pipeline.store.add_chunk(self.session, chunk)
        return chunk

    def recorded_media_jobs(self) -> list[str]:
        """Capture media-pool submissions without running them.

        Proxies and rundowns deliberately do not inherit finalisation ownership
        -- neither may hold the chunk mutation lock for the minutes it takes to
        run -- so a fake ownership group no longer observes them. This records
        what reached the pool instead.
        """
        keys: list[str] = []

        def submit(key, label, kind, work):
            keys.append(key)
            return Job(key, label, kind)

        patcher = patch.object(self.pipeline.media_jobs, "submit",
                               side_effect=submit)
        patcher.start()
        self.addCleanup(patcher.stop)
        return keys


class RecordingOwner:
    def __init__(self, accept: bool = True):
        self.accept = accept
        self.keys: list[str] = []

    def submit(self, pool, key, label, kind, work):
        self.keys.append(key)
        return Job(key, label, kind) if self.accept else None


class RecoveryArtifactTests(PipelineFixture):
    def publish_complete(self, chunk: Chunk) -> Path:
        output = self.pipeline.transcriber.output_dir(self.session, chunk)
        words = [Word("hello", 0.0, 0.5, 0.9)]
        metadata = {
            "channel": self.session.channel,
            "session_id": self.session.session_id,
            "chunk": chunk.label,
            "session_offset": chunk.session_offset,
            "language": "en",
            "asr_identity": {
                "provider": "deepgram", "model": "nova-3", "language": "en",
                "filler_words": True,
            },
            "complete": True,
            "covered_seconds": chunk.duration,
            "expected_seconds": chunk.duration,
        }
        write_exports(
            output, words, language="en",
            meta={
                "channel": self.session.channel,
                "session_id": self.session.session_id,
                "chunk": chunk.label,
                "session_offset": chunk.session_offset,
                "complete": True,
                "source": chunk.master_name,
            },
            words_meta=metadata,
        )
        return output

    def test_valid_master_reconciles_derivatives_independently(self):
        self.config.set("proxies.enabled", True)
        self.config.set("transcription.enabled", True)
        self.config.set("secrets.deepgram_api_key", "key")
        self.config.set("summary.enabled", True)
        self.config.set("summary.provider", "claude-cli")
        self.config.set("summary.min_words", 1)
        chunk = self.chunk(proxy_status=ERROR, transcript_status=ERROR,
                           summary_status=ERROR)
        output = self.pipeline.transcriber.output_dir(self.session, chunk)
        save_words(output / "words.json", [Word("hello", 0.0, 0.5, 0.9)], {
            "complete": True, "covered_seconds": 10.0,
            "expected_seconds": 10.0,
        })
        owner = RecordingOwner()
        media_keys = self.recorded_media_jobs()

        with patch.object(self.pipeline, "_seam_recovery_needed",
                          return_value=False):
            actions = self.pipeline._recover_artifacts(
                self.session, chunk, owner)

        self.assertEqual(chunk.transcript_status, DONE)
        self.assertEqual(chunk.proxy_status, PENDING)
        self.assertEqual(chunk.summary_status, PENDING)
        self.assertIn("proxy:sess:c000", media_keys)
        self.assertIn("recover-post:sess:c000", owner.keys)
        self.assertTrue(any("transcript" in action for action in actions))

    def test_existing_proxy_is_rebuilt_when_full_validation_fails(self):
        self.config.set("proxies.enabled", True)
        chunk = self.chunk(proxy_status=DONE, proxy_name="stale.mp4")
        master = self.directory / "master" / chunk.master_name
        proxy = master.parent / "Proxies" / f"{master.stem}_Proxy.mp4"
        master.write_bytes(b"m" * 2048)
        proxy.parent.mkdir()
        proxy.write_bytes(b"p" * 2048)
        owner = RecordingOwner()
        media_keys = self.recorded_media_jobs()

        with patch("vodpipe.pipeline.validate_proxy",
                   side_effect=RuntimeError("audio stream is short")) as validate:
            actions = self.pipeline._recover_artifacts(
                self.session, chunk, owner)

        validate.assert_called_once_with(
            self.pipeline.tools, master, proxy, height=540)
        self.assertEqual(chunk.proxy_status, PENDING)
        self.assertEqual(chunk.proxy_name, "")
        self.assertIn("proxy:sess:c000", media_keys)
        self.assertIn("will rebuild an invalid proxy", actions)

    def test_refused_transcript_recovery_is_error_not_eternal_pending(self):
        self.config.set("transcription.enabled", True)
        self.config.set("secrets.deepgram_api_key", "key")
        chunk = self.chunk(transcript_status=ERROR)
        owner = RecordingOwner(accept=False)

        self.pipeline._recover_artifacts(self.session, chunk, owner)

        self.assertEqual(chunk.transcript_status, ERROR)
        self.assertIn("could not be queued", chunk.transcript_error)

    def test_previous_complete_generation_is_restored_after_crash(self):
        chunk = self.chunk(transcript_status=ERROR)
        output = self.pipeline.transcriber.output_dir(self.session, chunk)
        stash = output / "words.json.previous"
        save_words(stash, [Word("old", 0.0, 0.4, 0.9)], {
            "complete": True, "covered_seconds": 10.0,
            "expected_seconds": 10.0,
        })
        backup = output / "generation.previous"
        backup.mkdir()
        shutil.copyfile(stash, backup / "words.json")

        actions = self.pipeline._recover_words_stash(self.session, chunk)

        words, meta = load_words(output / "words.json")
        self.assertEqual([word.text for word in words], ["old"])
        self.assertTrue(meta["complete"])
        self.assertFalse(stash.exists())
        self.assertIn("restored previous transcript generation", actions)

    def test_missing_core_exports_are_republished_from_words(self):
        chunk = self.chunk(transcript_status=ERROR)
        output = self.publish_complete(chunk)
        (output / "premiere.json").unlink()
        (output / "transcript.srt").unlink()

        actions = self.pipeline._recover_artifacts(
            self.session, chunk, RecordingOwner())

        self.assertTrue((output / "premiere.json").is_file())
        self.assertTrue((output / "transcript.srt").is_file())
        self.assertIn("repaired transcript export generation", actions)
        self.assertEqual(chunk.transcript_status, DONE)

    def test_corrupt_export_manifest_is_republished_from_words(self):
        chunk = self.chunk(transcript_status=ERROR)
        output = self.publish_complete(chunk)
        (output / MANIFEST_NAME).write_text("{broken", encoding="utf-8")

        actions = self.pipeline._recover_artifacts(
            self.session, chunk, RecordingOwner())

        manifest = json.loads(
            (output / MANIFEST_NAME).read_text(encoding="utf-8"))
        words, meta = load_words(output / "words.json")
        self.assertEqual(manifest["generation"], meta["generation"])
        self.assertEqual(manifest["word_count"], len(words))
        self.assertIn("repaired transcript export generation", actions)

    def test_mismatched_manifest_generation_is_republished_from_words(self):
        chunk = self.chunk(transcript_status=ERROR)
        output = self.publish_complete(chunk)
        path = output / MANIFEST_NAME
        manifest = json.loads(path.read_text(encoding="utf-8"))
        manifest["generation"] = "0" * 16
        path.write_text(json.dumps(manifest), encoding="utf-8")

        actions = self.pipeline._recover_artifacts(
            self.session, chunk, RecordingOwner())

        _, meta = load_words(output / "words.json")
        repaired = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(repaired["generation"], meta["generation"])
        self.assertIn("repaired transcript export generation", actions)

    def test_valid_manifest_allows_correctly_absent_optional_files(self):
        chunk = self.chunk(transcript_status=ERROR)
        output = self.publish_complete(chunk)
        manifest_before = (output / MANIFEST_NAME).read_bytes()

        with patch.object(self.pipeline.transcriber, "republish") as republish:
            actions = self.pipeline._recover_artifacts(
                self.session, chunk, RecordingOwner())

        republish.assert_not_called()
        self.assertEqual((output / MANIFEST_NAME).read_bytes(), manifest_before)
        self.assertNotIn("repaired transcript export generation", actions)

    def test_a_bare_rundown_is_not_adopted_as_a_transcript(self):
        self.config.set("summary.enabled", True)
        self.config.set("summary.provider", "claude-cli")
        chunk = self.chunk(transcript_status=ERROR, summary_status=ERROR)
        output = self.pipeline.transcriber.output_dir(self.session, chunk)
        output.mkdir(parents=True)
        rundown = output / "rundown.md"
        rundown.write_text("# orphan rundown", encoding="utf-8")

        self.pipeline._recover_artifacts(
            self.session, chunk, RecordingOwner())

        self.assertFalse(rundown.exists())
        self.assertNotEqual(chunk.summary_status, DONE)


class RecoveryTimelineTests(PipelineFixture):
    def test_orphans_are_measured_then_offsets_are_recomputed(self):
        first = self.chunk(duration=99.0, session_offset=50.0)
        (self.directory / "live" / first.ts_name).write_bytes(b"x")
        safe = self.directory / "live" / "chan_c001.ts"
        production = self.directory / "live" / "chan_sess_c002.ts"
        unsafe = self.directory / "live" / "other_c003.ts"
        for path in (safe, production, unsafe):
            path.write_bytes(b"x")
        durations = {
            first.ts_name: 5.0,
            safe.name: 7.0,
            production.name: 11.0,
            unsafe.name: 13.0,
        }

        with patch("vodpipe.pipeline.live_duration",
                   side_effect=lambda tools, path, **kwargs: durations[path.name]):
            self.pipeline._scan_session_media(self.session)

        chunks = sorted(self.session.chunks, key=lambda item: item.index)
        self.assertEqual([chunk.index for chunk in chunks], [0, 1, 2])
        self.assertEqual([chunk.duration for chunk in chunks], [5.0, 7.0, 11.0])
        self.assertEqual([chunk.session_offset for chunk in chunks], [0.0, 5.0, 12.0])
        self.assertNotIn(unsafe.name, {chunk.ts_name for chunk in chunks})
        self.assertTrue(unsafe.exists(), "noncanonical media must remain untouched")

    def test_unreadable_ts_falls_back_to_master_and_corrects_stale_duration(self):
        first = self.chunk(duration=99.0, session_offset=50.0)
        second = self.chunk(index=1, duration=88.0, session_offset=150.0)
        for chunk in (first, second):
            (self.directory / "live" / chunk.ts_name).write_bytes(b"ts")
            (self.directory / "master" / chunk.master_name).write_bytes(b"mp4")
        measured = {
            ("live", first.ts_name): 0.0,
            ("master", first.master_name): 8.0,
            ("live", second.ts_name): 0.0,
            ("master", second.master_name): 4.0,
        }
        seen = []

        def duration(tools, path, **kwargs):
            seen.append((path.parent.name, path.name))
            return measured[(path.parent.name, path.name)]

        with patch("vodpipe.pipeline.live_duration", side_effect=duration):
            self.pipeline._scan_session_media(self.session)

        self.assertEqual([first.duration, second.duration], [8.0, 4.0])
        self.assertEqual([first.session_offset, second.session_offset], [0.0, 8.0])
        self.assertEqual(set(seen), set(measured))

    def test_existing_master_validation_receives_actual_differently_named_ts(self):
        chunk = self.chunk(
            ts_name="chan_sess_c000.ts", master_name="chan_c000.mp4",
            status=COMPLETE)
        source = self.directory / "live" / chunk.ts_name
        master = self.directory / "master" / chunk.master_name
        source.write_bytes(b"source" * 400)
        master.write_bytes(b"master" * 400)

        with patch("vodpipe.pipeline.validate_master") as validate, \
                patch("vodpipe.pipeline.video_dimensions", return_value=(320, 180)), \
                patch.object(self.pipeline, "_recover_artifacts", return_value=[]):
            self.pipeline._recover_chunk(
                self.session, chunk, RecordingOwner())

        validate.assert_called_once_with(
            self.pipeline.tools, master, chunk.duration, source=source)

    def test_complete_chunk_with_ts_and_no_master_is_requeued(self):
        chunk = self.chunk(status=COMPLETE)
        (self.directory / "live" / chunk.ts_name).write_bytes(b"video")
        owner = RecordingOwner()

        action = self.pipeline._recover_chunk(self.session, chunk, owner)

        self.assertEqual(chunk.status, REMUXING)
        self.assertIn("finalize:sess:c000", owner.keys)
        self.assertIn("re-queued finalisation", action)


class QuarantinedManifestRecoveryTests(unittest.TestCase):
    def test_canonical_ts_is_adopted_without_trusting_invalid_manifest(self):
        root = Path(tempfile.mkdtemp(prefix="vodpipe-quarantine-recovery-"))
        self.addCleanup(shutil.rmtree, root, True)
        config = make_config(root)
        directory = config.masters_root / "chan" / "sess"
        live = directory / "live"
        live.mkdir(parents=True)
        (live / "chan_c000.ts").write_bytes(b"recorded-media")
        (directory / "session.json").write_text("{broken", encoding="utf-8")

        pipeline = Pipeline(config)
        self.addCleanup(pipeline.shutdown, job_timeout=10)
        self.assertIsNone(pipeline.store.get("sess"))
        with patch("vodpipe.pipeline.live_duration", return_value=5.0), \
                patch.object(pipeline, "_recover_chunk", return_value=""):
            actions = pipeline.recover()

        session = pipeline.store.get("sess")
        self.assertIsNotNone(session)
        self.assertEqual([chunk.ts_name for chunk in session.chunks],
                         ["chan_c000.ts"])
        self.assertTrue(any("quarantining an invalid manifest" in action
                            for action in actions))

    def test_canonical_master_only_media_is_adopted_for_recovery(self):
        root = Path(tempfile.mkdtemp(prefix="vodpipe-quarantine-master-"))
        self.addCleanup(shutil.rmtree, root, True)
        config = make_config(root)
        directory = config.masters_root / "chan" / "sess"
        master = directory / "master"
        master.mkdir(parents=True)
        path = master / "chan_sess_c000.mp4"
        path.write_bytes(b"recorded-master")
        (directory / "session.json").write_text("{broken", encoding="utf-8")

        pipeline = Pipeline(config)
        self.addCleanup(pipeline.shutdown, job_timeout=10)
        with patch("vodpipe.pipeline.live_duration", return_value=5.0), \
                patch.object(pipeline, "_recover_chunk", return_value=""):
            pipeline.recover()

        session = pipeline.store.get("sess")
        self.assertIsNotNone(session)
        self.assertEqual(len(session.chunks), 1)
        self.assertEqual(session.chunks[0].master_name, path.name)
        self.assertEqual(session.chunks[0].ts_name, "")
        self.assertEqual(session.chunks[0].duration, 5.0)


class OwnershipAndLeaseTests(PipelineFixture):
    def test_peer_recovery_cannot_claim_a_held_chunk(self):
        chunk = self.chunk()
        master = self.directory / "master" / chunk.master_name
        master.write_bytes(b"x" * 2048)
        lock = ResourceLock(chunk_lock_path(self.directory, chunk.label)).acquire()
        try:
            with patch("vodpipe.pipeline.validate_master"), \
                    patch("vodpipe.pipeline.video_dimensions", return_value=(1, 1)):
                actions = self.pipeline.recover()
        finally:
            lock.release()
        self.assertTrue(any("another process owns finalisation" in item
                            for item in actions), actions)

    def test_cross_pipeline_read_lease_defers_delete_until_release(self):
        chunk = self.chunk()
        source = self.directory / "live" / chunk.ts_name
        source.write_bytes(b"video")
        peer = Pipeline(self.config)
        self.addCleanup(peer.shutdown, job_timeout=10)

        with self.pipeline.read_lease([source]):
            peer._reclaim_ts(self.session, chunk, source)
            self.assertTrue(source.exists())
            peer._drain_deferred_deletes()
            self.assertTrue(source.exists())
        peer._drain_deferred_deletes()
        self.assertFalse(source.exists())

    def test_recovery_reclaims_duplicate_ts_only_after_lease(self):
        chunk = self.chunk(status=COMPLETE)
        source = self.directory / "live" / chunk.ts_name
        master = self.directory / "master" / chunk.master_name
        source.write_bytes(b"working copy")
        master.write_bytes(b"x" * 2048)
        owner = RecordingOwner()

        with self.pipeline.read_lease([source]), \
                patch("vodpipe.pipeline.validate_master"), \
                patch("vodpipe.pipeline.video_dimensions", return_value=(1, 1)), \
                patch.object(self.pipeline, "_recover_artifacts", return_value=[]):
            self.pipeline._recover_chunk(self.session, chunk, owner)
            self.assertTrue(source.exists())
        self.assertFalse(source.exists())

    def test_absent_candidate_paths_are_leased(self):
        chunk = self.chunk(status=RECORDING)
        service = self.pipeline.snapshots
        candidates = service.readable_sources(self.session)
        self.assertIn(self.directory / "live" / chunk.ts_name, candidates)
        self.assertIn(self.directory / "master" / chunk.master_name, candidates)

    def test_tick_skips_externally_owned_live_session(self):
        self.session.status = RECORDING
        chunk = self.chunk(status=RECORDING)
        self.pipeline._externally_owned_sessions.add(self.session.session_id)
        with patch.object(self.pipeline.jobs, "submit") as submit, \
                patch.object(self.pipeline, "_sweep_proxies"):
            self.pipeline._tick()
        submit.assert_not_called()


class DiskBudgetTests(unittest.TestCase):
    def test_concurrent_reservations_are_accounted_atomically(self):
        root = Path(tempfile.mkdtemp(prefix="vodpipe-budget-"))
        self.addCleanup(shutil.rmtree, root, True)
        budget = DiskBudget(root, lambda: 40)
        with patch("vodpipe.disk.free_bytes", return_value=150):
            first = budget.reserve(60, "first")
            self.addCleanup(first.release)
            with self.assertRaisesRegex(RuntimeError, "already reserved"):
                budget.reserve(60, "second")
            first.release()
            budget.reserve(60, "after release").release()

    def test_live_reservation_metadata_is_readable_on_windows(self):
        root = Path(tempfile.mkdtemp(prefix="vodpipe-budget-live-"))
        self.addCleanup(shutil.rmtree, root, True)
        first_budget = DiskBudget(root, lambda: 40)
        second_budget = DiskBudget(root, lambda: 40)
        with patch("vodpipe.disk.free_bytes", return_value=200):
            first = first_budget.reserve(60, "first")
            self.addCleanup(first.release)
            second = second_budget.reserve(60, "second")
            second.release()


class ProxyDiskAdmissionTests(PipelineFixture):
    def test_quality_zero_reserves_uncompressed_peak_not_master_fraction(self):
        self.config.set("proxies.enabled", True)
        self.config.set("proxies.quality", 0)
        chunk = self.chunk()
        master = self.directory / "master" / chunk.master_name
        master.write_bytes(b"m" * (10 * 1024 * 1024))
        probe = {
            "format": {"duration": "60.0"},
            "streams": [
                {"index": 0, "codec_type": "video", "codec_name": "h264",
                 "width": 1920, "height": 1080, "avg_frame_rate": "60/1",
                 "duration": "60.0"},
                {"index": 1, "codec_type": "audio", "codec_name": "aac",
                 "channels": 2, "channel_layout": "stereo",
                 "duration": "60.0"},
            ],
        }
        reservation = Mock()
        self.pipeline.disk_budget.reserve = Mock(return_value=reservation)

        def publish(tools, source, destination, **kwargs):
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(b"proxy")

        with patch("vodpipe.media.ffprobe_json", return_value=probe), \
                patch("vodpipe.pipeline.probe_encoder", return_value="libx264"), \
                patch("vodpipe.pipeline.make_proxy", side_effect=publish):
            self.pipeline._make_proxy_inner(
                Job("proxy", "proxy", "proxy"), self.session, chunk)

        needed = self.pipeline.disk_budget.reserve.call_args.args[0]
        self.assertGreater(needed, master.stat().st_size // 5)
        self.assertGreater(needed, master.stat().st_size)
        reservation.release.assert_called_once()


class SnapshotContractTests(PipelineFixture):
    def drain_pool(self, pool) -> None:
        deadline = time.time() + 10
        while pool.active_count() and time.time() < deadline:
            time.sleep(0.01)
        self.assertEqual(pool.active_count(), 0)

    def complete_snapshot_publication(self):
        source = self.directory / "snapshots" / "cut.mp4"
        source.write_bytes(b"video")
        output = source.parent / "cut_transcript"
        words = [Word("uh", 0.0, 0.5, 0.9)]
        metadata = {
            "source": source.name,
            "language": "en",
            "complete": True,
            "covered_seconds": 5.0,
            "expected_seconds": 5.0,
        }
        write_exports(
            output, words, language="en",
            meta={"source": source.name, "complete": True},
            words_meta=metadata)
        result = SnapshotResult(
            source, 0.0, 5.0, ["c000"], transcript_dir=output,
            actual_duration=5.0, transcript_status=DONE)
        self.pipeline._record_snapshot(
            self.session, result, transcript_requested=True)
        return source, output

    def test_transcript_submission_failure_is_persisted_and_retryable(self):
        self.config.set("transcription.enabled", True)
        self.config.set("secrets.deepgram_api_key", "key")
        source = self.directory / "snapshots" / "cut.mp4"
        source.write_bytes(b"video")
        result = SnapshotResult(source, 0.0, 5.0, ["c000"], actual_duration=5.0)
        request = SnapshotRequest("sess", start=0.0, end=5.0, transcribe=True)

        with patch.object(self.pipeline.media_jobs, "submit", return_value=None):
            self.pipeline._after_snapshot(self.session, request, result)
        entries = json.loads((source.parent / "snapshots.json").read_text())
        self.assertEqual(entries[0]["transcript_status"], ERROR)
        self.assertIn("retried", entries[0]["transcript_error"])

        with patch.object(self.pipeline, "_queue_snapshot_transcript",
                          return_value=Job("k", "label", "transcribe")):
            actions = self.pipeline._recover_snapshot_tasks(self.session)
        entries = json.loads((source.parent / "snapshots.json").read_text())
        self.assertEqual(entries[0]["transcript_status"], PENDING)
        self.assertTrue(actions)

    def test_recovery_repairs_complete_but_corrupt_publication(self):
        source, output = self.complete_snapshot_publication()
        (output / "premiere.json").write_text("{broken", encoding="utf-8")
        (output / "transcript.srt").unlink()

        with patch.object(self.pipeline.transcriber, "transcribe_file") as asr:
            actions = self.pipeline._recover_snapshot_tasks(self.session)
            self.drain_pool(self.pipeline.media_jobs)

        entries = json.loads((source.parent / "snapshots.json").read_text())
        self.assertEqual(entries[0]["transcript_status"], DONE)
        json.loads((output / "premiere.json").read_text(encoding="utf-8"))
        self.assertTrue((output / "transcript.srt").is_file())
        asr.assert_not_called()
        self.assertTrue(any("transcript repair" in action for action in actions))

    def test_failed_publication_repair_is_error_then_retries(self):
        source, output = self.complete_snapshot_publication()
        (output / "premiere.json").unlink()

        with patch("vodpipe.pipeline.write_exports",
                   side_effect=RuntimeError("repair broke")):
            self.pipeline._recover_snapshot_tasks(self.session)
            self.drain_pool(self.pipeline.media_jobs)
        entries = json.loads((source.parent / "snapshots.json").read_text())
        self.assertEqual(entries[0]["transcript_status"], ERROR)
        self.assertIn("repair broke", entries[0]["transcript_error"])

        self.pipeline._recover_snapshot_tasks(self.session)
        self.drain_pool(self.pipeline.media_jobs)
        entries = json.loads((source.parent / "snapshots.json").read_text())
        self.assertEqual(entries[0]["transcript_status"], DONE)
        self.assertTrue((output / "premiere.json").is_file())

    def test_wait_rejects_done_index_with_incomplete_publication(self):
        source, output = self.complete_snapshot_publication()
        (output / "premiere.json").unlink()

        status = self.pipeline.wait_for_snapshot(
            source, require_transcript=True, timeout=1)

        self.assertEqual(status["transcript_status"], ERROR)
        self.assertIn("incomplete", status["transcript_error"])
        entries = json.loads((source.parent / "snapshots.json").read_text())
        self.assertEqual(entries[0]["transcript_status"], ERROR)

    def test_pending_cut_intent_is_requeued_and_completed(self):
        chunk = self.chunk(duration=10.0)
        (self.directory / "master" / chunk.master_name).write_bytes(b"x" * 2048)
        request = SnapshotRequest(
            "sess", start=0.0, end=5.0, transcribe=False)
        key = "snapshot:sess:0-5000:recovery"
        self.pipeline._record_snapshot_intent(self.session, request, key)

        def create(session, frozen):
            path = self.directory / "snapshots" / "recovered.mp4"
            path.write_bytes(b"video")
            return SnapshotResult(
                path, 0.0, 5.0, ["c000"], actual_duration=5.0)

        with patch.object(self.pipeline.snapshots, "create", side_effect=create):
            actions = self.pipeline._recover_snapshot_tasks(self.session)
            self.drain_pool(self.pipeline.snapshot_jobs)

        entries = json.loads((self.directory / "snapshots" / "snapshots.json")
                             .read_text(encoding="utf-8"))
        self.assertEqual(entries[0]["file"], "recovered.mp4")
        self.assertEqual(entries[0]["cut_status"], DONE)
        self.assertTrue(any("pending snapshot" in action for action in actions))

    def test_material_shortfall_is_quarantined_and_fails(self):
        chunk = self.chunk(duration=10.0)
        master = self.directory / "master" / chunk.master_name
        master.write_bytes(b"x" * 2048)
        service = SnapshotService(
            self.config,
            Tools("ffmpeg", "ffprobe", "streamlink", None))

        def fake_cut(tools, parts, destination, **kwargs):
            destination.write_bytes(b"x" * 2048)

        with patch("vodpipe.snapshot.cut_and_join", side_effect=fake_cut), \
                patch("vodpipe.snapshot.validate_media_coverage",
                      return_value=3.0):
            with self.assertRaisesRegex(RuntimeError, "partial output retained"):
                service.create(
                    self.session,
                    SnapshotRequest("sess", start=0.0, end=10.0,
                                    transcribe=False))
        self.assertFalse(any(path.name == "cut.mp4"
                             for path in master.parent.glob("*.mp4")))
        partials = list((self.directory / "snapshots").glob(
            "*.partial-shortfall.mp4"))
        self.assertEqual(len(partials), 1)


class FakePool:
    def __init__(self, name: str, events: list[str]):
        self.name = name
        self.events = events
        self.state = ACCEPTING

    @property
    def accepting(self):
        return self.state == ACCEPTING

    def stop(self, timeout=0, **kwargs):
        self.events.append(self.name)
        self.state = POOL_STOPPED

    def get(self, key):
        return None


class ShutdownAndStartupTests(PipelineFixture):
    def test_shutdown_drains_capture_then_snapshots_then_media(self):
        events: list[str] = []
        for pool in self.pipeline.pools:
            pool.stop(timeout=10, drain=False)
        self.pipeline.jobs = FakePool("capture", events)
        self.pipeline.snapshot_jobs = FakePool("snapshot", events)
        self.pipeline.media_jobs = FakePool("media", events)
        self.pipeline.probe_jobs = FakePool("probe", events)

        self.pipeline.shutdown(job_timeout=1)

        self.assertEqual(events, ["probe", "capture", "snapshot", "media"])
        self.assertEqual(self.pipeline.lifecycle_state, STOPPED)

    def test_recorder_timeout_leaves_pipeline_draining(self):
        class Recorder:
            channel = "chan"
            running = True

            def __init__(self, session):
                self.session = session

            def stop(self, reason):
                pass

            def join(self, timeout=None):
                pass

        events: list[str] = []
        self.pipeline.jobs = FakePool("capture", events)
        self.pipeline.snapshot_jobs = FakePool("snapshot", events)
        self.pipeline.media_jobs = FakePool("media", events)
        self.pipeline._recorders["chan"] = Recorder(self.session)

        self.pipeline.shutdown(recorder_timeout=0, job_timeout=0)

        self.assertEqual(self.pipeline.lifecycle_state, DRAINING)
        self.assertIn("recorder threads", self.pipeline._lifecycle_error)
        self.assertEqual(events, [], "consumers stopped while a producer was alive")

    def test_worker_timeout_does_not_report_stopped(self):
        started = threading.Event()
        release = threading.Event()
        self.pipeline.jobs.submit(
            "block", "block", "test",
            lambda job: (started.set(), release.wait(10)))
        self.assertTrue(started.wait(5))

        self.pipeline.shutdown(job_timeout=0.01)

        self.assertEqual(self.pipeline.lifecycle_state, DRAINING)
        self.assertIn("capture", self.pipeline._lifecycle_error)
        release.set()
        self.pipeline.shutdown(job_timeout=5)
        self.assertEqual(self.pipeline.lifecycle_state, STOPPED)

    def test_failed_thread_launch_rolls_start_back_cleanly(self):
        original = threading.Thread.start

        def fail_watcher(thread):
            if thread.name == "watcher":
                raise RuntimeError("watcher launch failed")
            return original(thread)

        with patch("vodpipe.pipeline.threading.Thread.start", new=fail_watcher):
            with self.assertRaisesRegex(RuntimeError, "watcher launch failed"):
                self.pipeline.start()

        self.assertEqual(self.pipeline.lifecycle_state, STOPPED)
        self.assertFalse(self.pipeline._threads)
        self.assertFalse(self.pipeline._stop.is_set())

    def test_failed_watcher_launch_waits_for_recovery_then_retries_cleanly(self):
        recovery_started = threading.Event()
        recovery_release = threading.Event()
        recovery_finished = threading.Event()
        watcher_failed = threading.Event()
        errors: list[Exception] = []
        old_pools = self.pipeline.pools

        def recover():
            self.pipeline.jobs.submit(
                "blocked-recovery", "blocked recovery", "recover",
                lambda job: (
                    recovery_started.set(),
                    recovery_release.wait(10),
                    recovery_finished.set(),
                ))
            self.assertTrue(recovery_started.wait(5))
            return ["queued blocked recovery"]

        original = threading.Thread.start

        def fail_watcher(thread):
            if thread.name == "watcher":
                watcher_failed.set()
                raise RuntimeError("watcher launch failed")
            return original(thread)

        def launch():
            try:
                self.pipeline.start()
            except Exception as exc:
                errors.append(exc)

        with patch.object(self.pipeline, "recover", side_effect=recover), \
                patch("vodpipe.pipeline.threading.Thread.start", new=fail_watcher):
            starter = threading.Thread(target=launch)
            starter.start()
            self.assertTrue(watcher_failed.wait(5))
            self.assertEqual(self.pipeline.lifecycle_state, DRAINING)
            self.assertTrue(starter.is_alive(),
                            "start returned while recovery still owned state")
            self.assertFalse(recovery_finished.is_set())
            recovery_release.set()
            starter.join(10)

        self.assertFalse(starter.is_alive())
        self.assertTrue(recovery_finished.is_set())
        self.assertEqual(len(errors), 1)
        self.assertIn("watcher launch failed", str(errors[0]))
        self.assertEqual(self.pipeline.lifecycle_state, STOPPED)
        for old, fresh in zip(old_pools, self.pipeline.pools):
            self.assertEqual(old.state, POOL_STOPPED)
            self.assertIsNot(old, fresh)
            self.assertTrue(fresh.accepting)

        with patch.object(self.pipeline, "recover", return_value=[]):
            self.pipeline.start()
        self.assertEqual(self.pipeline.lifecycle_state, "running")

    def test_derived_budgets_cover_probe_and_configured_operations(self):
        self.pipeline.config.set("watcher.probe_timeout_seconds", 180)
        self.pipeline.config.set("recording.ffmpeg_grace_seconds", 300)
        self.pipeline.config.set(
            "transcription.request_timeout_seconds", 7200)
        self.pipeline.config.set("summary.timeout_seconds", 7200)

        self.assertGreaterEqual(
            self.pipeline._producer_shutdown_timeout(), 210)
        self.assertGreaterEqual(
            self.pipeline._recorder_shutdown_timeout(), 300)
        self.assertGreaterEqual(
            self.pipeline._job_shutdown_timeout(), 7_260)
        self.assertGreaterEqual(
            self.pipeline._shutdown_ultimate_timeout(),
            self.pipeline._job_shutdown_timeout())

    def test_second_interrupt_logs_recoverable_work_and_propagates(self):
        self.pipeline._state = DRAINING
        with patch.object(
                self.pipeline, "shutdown", side_effect=KeyboardInterrupt), \
                self.assertLogs("vodpipe", level="WARNING") as captured:
            with self.assertRaises(KeyboardInterrupt):
                self.pipeline.shutdown_until_stopped(progress_interval=0.1)

        self.assertEqual(self.pipeline.lifecycle_state, DRAINING)
        self.assertIn("recoverable work", "\n".join(captured.output))


if __name__ == "__main__":
    unittest.main()
