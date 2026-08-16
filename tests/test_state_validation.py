"""Total validation and quarantine for persisted session manifests."""

from __future__ import annotations

import copy
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from vodpipe.state import MANIFEST_VERSION, Chunk, Session, SessionStore


class ManifestValidationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="vodpipe-state-validation-"))
        self.root = self.tmp / "masters"
        self.session_id = "chan_2026-01-01_000000_abc123"
        self.session_dir = self.root / "chan" / self.session_id
        self.session_dir.mkdir(parents=True)
        (self.session_dir / "master").mkdir()
        self.media = self.session_dir / "master" / "chan_c000.mp4"
        self.media.write_bytes(b"media must survive quarantine")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def payload(self) -> dict:
        session = Session(
            session_id=self.session_id,
            channel="chan",
            started_at=100.0,
            ended_at=110.0,
            directory=str(self.session_dir.resolve()),
            status="complete",
            title="A stream",
            quality_selected="1080p60",
            quality_available=["720p60", "1080p60"],
        )
        session.ad_events.append({
            "kind": "break",
            "detail": "advertisement break",
            "at_wall": 103.0,
            "approx_session_seconds": 3.0,
        })
        session.chunks.append(Chunk(
            index=0,
            session_id=self.session_id,
            channel="chan",
            started_at=100.0,
            ended_at=105.0,
            master_name="chan_c000.mp4",
            duration=5.0,
            size_bytes=25,
            status="complete",
            proxy_status="done",
            transcript_status="done",
            summary_status="skipped",
            word_count=4,
            width=1920,
            height=1080,
        ))
        return session.to_dict()

    def write(self, payload) -> Path:
        path = self.session_dir / "session.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def assert_quarantined(self, original: Path, store: SessionStore) -> dict:
        self.assertIsNone(store.get(self.session_id))
        self.assertFalse(original.exists())
        self.assertTrue(self.media.exists(), "validation must leave media untouched")
        diagnostics = store.diagnostics()
        self.assertGreaterEqual(len(diagnostics), 1)
        diagnostic = diagnostics[-1]
        quarantined = Path(diagnostic["quarantine_path"])
        self.assertTrue(quarantined.exists())
        self.assertTrue(Path(diagnostic["diagnostic_path"]).exists())
        return diagnostic

    def test_malformed_json_is_quarantined_and_reported(self):
        path = self.session_dir / "session.json"
        original = b'{"session_id": "broken"'
        path.write_bytes(original)

        store = SessionStore(self.root)
        store.load_from_disk()

        diagnostic = self.assert_quarantined(path, store)
        self.assertIn("malformed JSON", diagnostic["error"])
        self.assertEqual(Path(diagnostic["quarantine_path"]).read_bytes(), original)

    def test_valid_json_with_wrong_types_is_quarantined(self):
        mutations = {
            "session id": lambda data: data.update(session_id=[]),
            "timestamp": lambda data: data.update(started_at="yesterday"),
            "chunks": lambda data: data.update(chunks={}),
            "artifact error": lambda data: data["chunks"][0].update(proxy_error=[]),
            "artifact state": lambda data: data["chunks"][0].update(proxy_status=7),
            "artifact name": lambda data: data["chunks"][0].update(
                master_name="../../outside.mp4"),
            "artifact error map": lambda data: data["chunks"][0].update(errors=[]),
            "quality list": lambda data: data.update(quality_available={}),
            "ad event list": lambda data: data.update(ad_events={}),
            "ad event object": lambda data: data.update(ad_events=[[]]),
            "ad event number": lambda data: data["ad_events"][0].update(
                at_wall="now"),
        }
        for label, mutate in mutations.items():
            with self.subTest(label=label):
                data = self.payload()
                mutate(data)
                path = self.write(data)
                store = SessionStore(self.root)
                store.load_from_disk()
                self.assert_quarantined(path, store)

    def test_unhashable_session_id_is_diagnosed_not_raised(self):
        data = self.payload()
        data["session_id"] = ["not", "hashable"]
        path = self.write(data)

        store = SessionStore(self.root)
        store.load_from_disk()

        diagnostic = self.assert_quarantined(path, store)
        self.assertIn("session_id must be text", diagnostic["error"])

    def test_absolute_and_traversal_channels_are_rejected_without_coercion(self):
        for channel in ("C:/outside", "../../outside", "Twitch.TV/Chan", "CHAN"):
            with self.subTest(channel=channel):
                data = self.payload()
                data["channel"] = channel
                data["chunks"][0]["channel"] = channel
                path = self.write(data)
                store = SessionStore(self.root)
                store.load_from_disk()
                self.assert_quarantined(path, store)

    def test_outside_and_traversing_directories_are_rejected(self):
        directories = (
            str((self.tmp / "outside").resolve()),
            str(self.session_dir / ".." / self.session_id),
            "../../outside",
        )
        for directory in directories:
            with self.subTest(directory=directory):
                data = self.payload()
                data["directory"] = directory
                path = self.write(data)
                store = SessionStore(self.root)
                store.load_from_disk()
                self.assert_quarantined(path, store)

    def test_unsafe_ids_and_directory_mismatches_are_rejected(self):
        for session_id in ("../escape", "C:/escape", "bad*glob"):
            with self.subTest(session_id=session_id):
                data = self.payload()
                data["session_id"] = session_id
                data["chunks"][0]["session_id"] = session_id
                path = self.write(data)
                store = SessionStore(self.root)
                store.load_from_disk()
                self.assert_quarantined(path, store)

    def test_duplicate_or_negative_chunk_indexes_are_rejected(self):
        for indexes in ((0, 0), (0, -1)):
            with self.subTest(indexes=indexes):
                data = self.payload()
                second = copy.deepcopy(data["chunks"][0])
                data["chunks"].append(second)
                for chunk, index in zip(data["chunks"], indexes):
                    chunk["index"] = index
                    chunk["label"] = f"c{index:03d}"
                path = self.write(data)
                store = SessionStore(self.root)
                store.load_from_disk()
                self.assert_quarantined(path, store)

    def test_invalid_statuses_and_numerics_are_rejected(self):
        mutations = {
            "session status": lambda data: data.update(status="mystery"),
            "chunk status": lambda data: data["chunks"][0].update(status="mystery"),
            "artifact status": lambda data: data["chunks"][0].update(
                transcript_status="expired"),
            "negative duration": lambda data: data["chunks"][0].update(duration=-1),
            "boolean count": lambda data: data["chunks"][0].update(word_count=True),
            "infinite timestamp": lambda data: data.update(ended_at=float("inf")),
        }
        for label, mutate in mutations.items():
            with self.subTest(label=label):
                data = self.payload()
                mutate(data)
                path = self.write(data)
                store = SessionStore(self.root)
                store.load_from_disk()
                self.assert_quarantined(path, store)

    def test_unknown_manifest_version_is_rejected(self):
        data = self.payload()
        data["version"] = MANIFEST_VERSION + 1
        path = self.write(data)

        store = SessionStore(self.root)
        store.load_from_disk()

        diagnostic = self.assert_quarantined(path, store)
        self.assertIn("unsupported manifest version", diagnostic["error"])

    def test_quarantine_diagnostic_is_available_on_a_later_start(self):
        data = self.payload()
        data["status"] = "invalid"
        path = self.write(data)
        first = SessionStore(self.root)
        first.load_from_disk()
        first_diagnostic = self.assert_quarantined(path, first)

        later = SessionStore(self.root)
        later.load_from_disk()

        self.assertEqual(len(later.manifest_diagnostics), 1)
        self.assertEqual(later.manifest_diagnostics[0]["quarantine_path"],
                         first_diagnostic["quarantine_path"])

    def test_valid_legacy_manifest_loads_and_round_trips_to_v1(self):
        data = self.payload()
        self.assertEqual(data.pop("version"), MANIFEST_VERSION)
        path = self.write(data)

        store = SessionStore(self.root)
        store.load_from_disk()

        restored = store.get(self.session_id)
        self.assertIsNotNone(restored)
        self.assertEqual(restored.channel, "chan")
        self.assertEqual(restored.chunks[0].master_name, "chan_c000.mp4")
        self.assertEqual(restored.ad_events[0]["kind"], "break")
        self.assertEqual(store.diagnostics(), [])
        self.assertNotIn("version", json.loads(path.read_text(encoding="utf-8")),
                         "loading a valid live manifest remains read-only")

        store.flush(restored)
        rewritten = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(rewritten["version"], MANIFEST_VERSION)

    def test_loading_a_valid_manifest_twice_is_idempotent(self):
        self.write(self.payload())
        store = SessionStore(self.root)

        store.load_from_disk()
        store.load_from_disk()

        self.assertEqual(len(store.all()), 1)
        self.assertEqual(store.diagnostics(), [])

    def test_starting_is_additive_to_the_existing_manifest_version(self):
        data = self.payload()
        data["status"] = "starting"
        data["chunks"][0]["status"] = "starting"
        self.write(data)
        store = SessionStore(self.root)

        store.load_from_disk()

        restored = store.get(self.session_id)
        self.assertEqual(restored.status, "starting")
        self.assertEqual(restored.chunks[0].status, "starting")
        self.assertEqual(data["version"], MANIFEST_VERSION)
        self.assertEqual(store.diagnostics(), [])


if __name__ == "__main__":
    unittest.main()
