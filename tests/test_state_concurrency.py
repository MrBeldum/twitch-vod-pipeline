"""Cross-process consistency for session manifests and recording intent."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from vodpipe.config import DEFAULTS, Config, deep_merge
from vodpipe.pipeline import Pipeline
from vodpipe.state import (
    COMPLETE,
    DONE,
    RECORDING,
    RUNNING,
    STARTING,
    Chunk,
    ControlStateStore,
    Session,
    SessionStore,
)


def make_config(root: Path) -> Config:
    data = deep_merge(DEFAULTS, {
        "paths": {
            "masters_root": str(root / "masters"),
            "work_root": str(root / "work"),
            "censor_master_list": str(root / "none.txt"),
        },
        "watcher": {"enabled": False},
        "transcription": {"enabled": False},
        "summary": {"provider": "none"},
    })
    return Config(data, root / "config.json")


class SessionMergeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="vodpipe-state-merge-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.root = self.tmp / "masters"
        self.session_id = "chan_2026-01-01_000000_abc123"
        self.directory = self.root / "chan" / self.session_id
        self.directory.mkdir(parents=True)
        self.initial = Session(
            session_id=self.session_id,
            channel="chan",
            started_at=100.0,
            directory=str(self.directory),
            status=RECORDING,
        )
        self.initial.chunks.append(Chunk(
            index=0,
            session_id=self.session_id,
            channel="chan",
            started_at=100.0,
            status=RECORDING,
        ))
        SessionStore(self.root).add(self.initial)

    def stores(self):
        first = SessionStore(self.root)
        second = SessionStore(self.root)
        first.load_from_disk()
        second.load_from_disk()
        return first, second

    def disk(self) -> dict:
        return json.loads((self.directory / "session.json").read_text(
            encoding="utf-8"))

    def test_peer_chunk_addition_and_stale_chunk_update_both_survive(self):
        first, second = self.stores()
        first_session = first.get(self.session_id)
        second_session = second.get(self.session_id)
        original_second_chunk = second_session.chunk(0)

        first.add_chunk(first_session, Chunk(
            index=2,
            session_id=self.session_id,
            channel="chan",
            started_at=300.0,
            status=COMPLETE,
        ))
        second.update_chunk(
            second_session, original_second_chunk,
            transcript_status=DONE, word_count=42)

        chunks = {item["index"]: item for item in self.disk()["chunks"]}
        self.assertEqual(set(chunks), {0, 2})
        self.assertEqual(chunks[0]["transcript_status"], DONE)
        self.assertEqual(chunks[0]["word_count"], 42)
        self.assertIs(second_session.chunk(0), original_second_chunk)
        self.assertIsNotNone(second_session.chunk(2))

    def test_unmodified_stale_fields_cannot_regress_terminal_peer_state(self):
        first, second = self.stores()
        first_session = first.get(self.session_id)
        second_session = second.get(self.session_id)
        first_chunk = first_session.chunk(0)
        second_chunk = second_session.chunk(0)

        first.update(
            first_session, status=COMPLETE, ended_at=500.0,
            error="capture ended")
        first.update_chunk(
            first_session, first_chunk,
            proxy_status=DONE, proxy_name="chan_c000_Proxy.mp4",
            transcript_status=DONE, word_count=12)
        second.update_chunk(second_session, second_chunk, duration=120.0)

        payload = self.disk()
        chunk = payload["chunks"][0]
        self.assertEqual(payload["status"], COMPLETE)
        self.assertEqual(payload["ended_at"], 500.0)
        self.assertEqual(payload["error"], "capture ended")
        self.assertEqual(chunk["proxy_status"], DONE)
        self.assertEqual(chunk["proxy_name"], "chan_c000_Proxy.mp4")
        self.assertEqual(chunk["transcript_status"], DONE)
        self.assertEqual(chunk["word_count"], 12)
        self.assertEqual(chunk["duration"], 120.0)
        self.assertEqual(second_session.status, COMPLETE)
        self.assertEqual(second_session.ended_at, 500.0)

    def test_stale_startup_and_artifact_transitions_cannot_reopen_peer_outcomes(self):
        seed = SessionStore(self.root)
        seed.load_from_disk()
        seed_session = seed.get(self.session_id)
        seed.update(seed_session, status=STARTING)
        seed.update_chunk(seed_session, seed_session.chunk(0), status=STARTING)
        first, second = self.stores()
        first_session = first.get(self.session_id)
        second_session = second.get(self.session_id)

        first.update(first_session, status=COMPLETE, ended_at=500.0)
        first.update_chunk(
            first_session, first_session.chunk(0), status=COMPLETE,
            transcript_status=DONE, word_count=12)
        second_session.chunk(0).transcript_status = RUNNING
        second.confirm_first_media(second_session)

        payload = self.disk()
        chunk = payload["chunks"][0]
        self.assertEqual(payload["status"], COMPLETE)
        self.assertEqual(payload["ended_at"], 500.0)
        self.assertEqual(chunk["status"], COMPLETE)
        self.assertEqual(chunk["transcript_status"], DONE)
        self.assertEqual(chunk["word_count"], 12)

    def test_peer_ad_events_are_merged_and_objects_are_synchronized(self):
        first, second = self.stores()
        first_session = first.get(self.session_id)
        second_session = second.get(self.session_id)

        first.add_ad_event(first_session, "break", "first", 101.0, 1.0)
        second.add_ad_event(second_session, "preroll", "second", 102.0, 2.0)

        self.assertEqual(
            [event["detail"] for event in self.disk()["ad_events"]],
            ["first", "second"],
        )
        self.assertEqual(
            [event["detail"] for event in second_session.ad_events],
            ["first", "second"],
        )

    def test_subprocess_barrier_preserves_added_c002_and_updated_c000(self):
        ready_add = self.tmp / "ready-add"
        ready_update = self.tmp / "ready-update"
        go = self.tmp / "go"
        script = r'''
import sys, time
from pathlib import Path
from vodpipe.state import COMPLETE, DONE, Chunk, SessionStore

root, session_id, mode, ready, go = sys.argv[1:]
store = SessionStore(Path(root))
store.load_from_disk()
session = store.get(session_id)
Path(ready).write_text("ready", encoding="utf-8")
while not Path(go).exists():
    time.sleep(0.01)
if mode == "add":
    store.add_chunk(session, Chunk(
        index=2, session_id=session_id, channel="chan",
        started_at=300.0, status=COMPLETE))
else:
    store.update_chunk(
        session, session.chunk(0), transcript_status=DONE, word_count=77)
'''
        processes = [
            subprocess.Popen([
                sys.executable, "-c", script, str(self.root), self.session_id,
                "add", str(ready_add), str(go),
            ], cwd=Path(__file__).resolve().parents[1]),
            subprocess.Popen([
                sys.executable, "-c", script, str(self.root), self.session_id,
                "update", str(ready_update), str(go),
            ], cwd=Path(__file__).resolve().parents[1]),
        ]
        deadline = time.time() + 20
        while (not ready_add.exists() or not ready_update.exists()) \
                and time.time() < deadline:
            time.sleep(0.02)
        self.assertTrue(ready_add.exists() and ready_update.exists())
        go.write_text("go", encoding="utf-8")
        for process in processes:
            self.assertEqual(process.wait(timeout=30), 0)

        chunks = {item["index"]: item for item in self.disk()["chunks"]}
        self.assertEqual(set(chunks), {0, 2})
        self.assertEqual(chunks[0]["transcript_status"], DONE)
        self.assertEqual(chunks[0]["word_count"], 77)


class ControlMergeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="vodpipe-control-merge-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.config = make_config(self.tmp)
        self.config.masters_root.mkdir(parents=True)

    @staticmethod
    def request(request_id: str, channel: str, when: float) -> dict:
        return {
            "request_id": request_id,
            "channel": channel,
            "requested_at": when,
            "attempt_session_id": "",
            "last_error": "",
        }

    def test_two_loaded_stores_merge_requests_and_suppression_deltas(self):
        first = ControlStateStore(self.config.masters_root)
        second = ControlStateStore(self.config.masters_root)
        first_payload = first.load()
        second_payload = second.load()
        first_payload["requests"].append(self.request("a" * 16, "alpha", 1.0))
        first_payload["auto_suppressed"].append("alpha")
        first.save(first_payload)
        second_payload["requests"].append(self.request("b" * 16, "beta", 2.0))
        second_payload["auto_suppressed"].append("beta")
        second.save(second_payload)

        self.assertEqual(
            {item["channel"] for item in second_payload["requests"]},
            {"alpha", "beta"},
        )
        self.assertEqual(set(second_payload["auto_suppressed"]),
                         {"alpha", "beta"})

        first_payload["requests"] = []
        first_payload["results"] = [{
            "request_id": "a" * 16,
            "channel": "alpha",
            "status": "cancelled",
            "session_id": "",
            "error": "cancelled",
            "completed_at": 3.0,
        }]
        first_payload["auto_suppressed"] = []
        first.save(first_payload)

        durable = second.load()
        self.assertEqual([item["channel"] for item in durable["requests"]],
                         ["beta"])
        self.assertEqual([item["channel"] for item in durable["results"]],
                         ["alpha"])
        self.assertEqual(durable["auto_suppressed"], ["beta"])

    def test_two_preloaded_pipelines_preserve_and_complete_independent_requests(self):
        first = Pipeline(self.config)
        second = Pipeline(self.config)
        self.addCleanup(first.shutdown, job_timeout=5)
        self.addCleanup(second.shutdown, job_timeout=5)
        accepted = object()
        with mock.patch.object(first, "_submit_forced_probe_locked",
                               return_value=accepted), \
                mock.patch.object(second, "_submit_forced_probe_locked",
                                  return_value=accepted):
            alpha = first.request_recording("alpha")
            beta = second.request_recording("beta")
            self.assertTrue(first.cancel_request(alpha["request_id"]))
            self.assertTrue(second.cancel_request(beta["request_id"]))

        durable = ControlStateStore(self.config.masters_root).load()
        self.assertEqual(durable["requests"], [])
        self.assertEqual(
            {item["request_id"] for item in durable["results"]},
            {alpha["request_id"], beta["request_id"]},
        )
        self.assertEqual(
            {item["status"] for item in durable["results"]}, {"cancelled"})


class ExternalOwnershipGuardTests(unittest.TestCase):
    def test_manual_artifact_mutations_conflict_before_touching_state(self):
        root = Path(tempfile.mkdtemp(prefix="vodpipe-owner-guard-"))
        self.addCleanup(shutil.rmtree, root, True)
        pipeline = Pipeline(make_config(root))
        self.addCleanup(pipeline.shutdown, job_timeout=5)
        session = Session(
            session_id="chan_2026-01-01_000000_abc123",
            channel="chan",
            started_at=1.0,
            directory=str(root / "masters" / "chan" /
                          "chan_2026-01-01_000000_abc123"),
            status=RECORDING,
        )
        chunk = Chunk(
            index=0, session_id=session.session_id, channel="chan",
            started_at=1.0, status=COMPLETE,
        )
        session.chunks.append(chunk)
        pipeline._externally_owned_sessions.add(session.session_id)

        for operation in (pipeline.retranscribe, pipeline.resummarize):
            with self.subTest(operation=operation.__name__), \
                    self.assertRaisesRegex(RuntimeError, "owned by another pipeline"):
                operation(session, chunk)


if __name__ == "__main__":
    unittest.main()
