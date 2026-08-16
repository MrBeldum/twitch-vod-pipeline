"""End-to-end tests against synthetic media.

No Twitch, no network, no API key. A local ffmpeg process stands in for streamlink
and writes MPEG-TS to stdout in real time, which is byte-for-byte the same thing
the recorder consumes in production. Everything after that -- segmenting, remuxing,
proxying, snapshotting, slicing audio -- is the real code path.

These take about a minute; they are the slow suite on purpose.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import time
import unittest
from pathlib import Path

from vodpipe import recorder as recorder_module
from vodpipe.config import Config, deep_merge, DEFAULTS
from vodpipe.media import extract_audio_slice
from vodpipe.pipeline import Pipeline
from vodpipe.snapshot import SnapshotRequest
from vodpipe.state import Chunk, Session
from vodpipe.transcribe import RollingTranscriber
from vodpipe.transcript import Word
from vodpipe.util import media_duration, resolve_tools

STREAM_SECONDS = 26
CHUNK_SECONDS = 8


def fake_stream_command(tools, url, quality, **options):
    """Stand-in for streamlink: a real-time MPEG-TS test pattern on stdout.

    Takes its keyword options loosely on purpose. This stands in for a real
    signature, and pinning every keyword here meant adding one to the recorder
    broke ten unrelated tests with a TypeError from inside a worker thread.
    """
    return [
        tools.ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin",
        "-re",
        "-f", "lavfi", "-i", f"testsrc2=size=640x360:rate=30",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100",
        "-t", str(STREAM_SECONDS),
        "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
        "-g", "30", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "96k",
        "-f", "mpegts", "pipe:1",
    ]


def make_config(root: Path) -> Config:
    data = deep_merge(DEFAULTS, {
        "paths": {
            "masters_root": str(root / "masters"),
            "work_root": str(root / "work"),
            "censor_master_list": str(root / "censor.txt"),
        },
        "recording": {
            "chunk_seconds": CHUNK_SECONDS,
            "free_space_floor_gb": 0.001,
            "hard_reserve_gb": 0.0005,
        },
        "proxies": {"enabled": True, "height": 180, "quality": 30},
        "transcription": {"enabled": False},
        "summary": {"enabled": False},
        "watcher": {"enabled": False},
        "dashboard": {"open_browser": False},
        "channels": [],
    })
    (root / "censor.txt").write_text("damn\nporch monkey\n", encoding="utf-8")
    config = Config(data, root / "config.json")
    return config


class RecordingPipelineTests(unittest.TestCase):
    """One synthetic broadcast, driven all the way through the pipeline."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="vodpipe-test-"))
        cls.config = make_config(cls.tmp)

        cls._real_command = recorder_module.streamlink_command
        recorder_module.streamlink_command = fake_stream_command

        cls.pipeline = Pipeline(cls.config)
        cls.pipeline.start()
        cls.session = cls.pipeline.start_recording("testchannel")

        # Let the synthetic stream run to its natural end.
        deadline = time.time() + STREAM_SECONDS + 45
        while cls.session.status in ("starting", "recording") and time.time() < deadline:
            time.sleep(1)

        # Then let remux and proxy jobs drain. Every pool, not just the
        # capture-critical one -- proxies and rundowns run on their own now, and
        # waiting on the wrong queue would let the assertions race the encode.
        deadline = time.time() + 180
        while cls.pipeline.active_jobs() and time.time() < deadline:
            time.sleep(1)

    @classmethod
    def tearDownClass(cls):
        recorder_module.streamlink_command = cls._real_command
        try:
            cls.pipeline.shutdown()
        finally:
            shutil.rmtree(cls.tmp, ignore_errors=True)

    # -- recorder ----------------------------------------------------------

    def test_session_completed(self):
        self.assertEqual(self.session.status, "complete", self.session.error)

    def test_stream_was_split_into_multiple_chunks(self):
        self.assertGreaterEqual(len(self.session.chunks), 2)

    def test_chunk_offsets_are_contiguous(self):
        chunks = sorted(self.session.chunks, key=lambda c: c.index)
        expected = 0.0
        for chunk in chunks:
            self.assertAlmostEqual(chunk.session_offset, expected, places=1)
            expected += chunk.duration

    def test_total_duration_matches_the_broadcast(self):
        total = sum(chunk.duration for chunk in self.session.chunks)
        self.assertAlmostEqual(total, STREAM_SECONDS, delta=3.0)

    def test_every_chunk_produced_a_master(self):
        for chunk in self.session.chunks:
            master = self.session.path / "master" / chunk.master_name
            self.assertTrue(master.exists(), f"{chunk.label} master missing")
            self.assertGreater(master.stat().st_size, 1000)
            self.assertEqual(chunk.status, "complete", str(chunk.errors))

    def test_ts_working_copies_are_reclaimed_after_remux(self):
        for chunk in self.session.chunks:
            self.assertFalse((self.session.path / "live" / chunk.ts_name).exists(),
                             f"{chunk.ts_name} should have been removed")

    def test_master_duration_matches_the_recorded_chunk(self):
        tools = resolve_tools()
        for chunk in self.session.chunks:
            master = self.session.path / "master" / chunk.master_name
            self.assertAlmostEqual(media_duration(tools.ffprobe, master),
                                   chunk.duration, delta=1.0)

    def test_chunk_boundaries_land_on_keyframes(self):
        """A stream-copied master must begin with a keyframe or Premiere shows garbage."""
        tools = resolve_tools()
        from vodpipe.util import run
        for chunk in self.session.chunks:
            master = self.session.path / "master" / chunk.master_name
            result = run([
                tools.ffprobe, "-v", "error", "-select_streams", "v:0",
                "-show_entries", "frame=key_frame", "-read_intervals", "%+#1",
                "-of", "csv=p=0", str(master),
            ], timeout=60)
            first = result.stdout.strip().splitlines()[0].strip(",")
            self.assertEqual(first, "1", f"{chunk.label} does not start on a keyframe")

    # -- proxies -----------------------------------------------------------

    def test_proxies_were_generated_with_adobe_naming(self):
        for chunk in self.session.chunks:
            master = self.session.path / "master" / chunk.master_name
            proxy = master.parent / "Proxies" / f"{master.stem}_Proxy.mp4"
            self.assertTrue(proxy.exists(), f"{chunk.label} proxy missing")
            self.assertEqual(chunk.proxy_status, "done")

    def test_proxy_is_smaller_and_lower_resolution(self):
        tools = resolve_tools()
        from vodpipe.util import video_info
        chunk = self.session.chunks[0]
        master = self.session.path / "master" / chunk.master_name
        proxy = master.parent / "Proxies" / f"{master.stem}_Proxy.mp4"
        self.assertLess(proxy.stat().st_size, master.stat().st_size)
        self.assertEqual(video_info(tools.ffprobe, proxy)["height"], 180)

    # -- state -------------------------------------------------------------

    def test_session_json_round_trips(self):
        payload = json.loads((self.session.path / "session.json")
                             .read_text(encoding="utf-8"))
        restored = Session.from_dict(payload)
        self.assertEqual(restored.session_id, self.session.session_id)
        self.assertEqual(len(restored.chunks), len(self.session.chunks))

    def test_session_index_was_written(self):
        index = (self.session.path / "index.md").read_text(encoding="utf-8")
        self.assertIn("testchannel", index)
        self.assertIn(self.session.chunks[0].label, index)

    def test_store_recovers_sessions_from_disk(self):
        from vodpipe.state import SessionStore
        store = SessionStore(self.config.masters_root)
        store.load_from_disk()
        self.assertIsNotNone(store.get(self.session.session_id))

    # -- snapshots ---------------------------------------------------------

    def test_snapshot_of_a_range_inside_one_chunk(self):
        result = self.pipeline.snapshot(SnapshotRequest(
            session_id=self.session.session_id,
            start=1.0, end=5.0, transcribe=False, name="inner",
        ))
        self.assertTrue(result.path.exists())
        tools = resolve_tools()
        self.assertAlmostEqual(media_duration(tools.ffprobe, result.path), 4.0,
                               delta=2.5)
        self.assertEqual(len(result.spans), 1)

    def test_snapshot_spanning_a_chunk_boundary_is_joined(self):
        """The whole point of the early cut: a range that crosses chunk files."""
        boundary = self.session.chunks[0].duration
        result = self.pipeline.snapshot(SnapshotRequest(
            session_id=self.session.session_id,
            start=max(0.0, boundary - 3), end=boundary + 3,
            transcribe=False, name="seam",
        ))
        self.assertTrue(result.path.exists())
        self.assertEqual(len(result.spans), 2, "expected the cut to span two chunks")
        tools = resolve_tools()
        self.assertAlmostEqual(media_duration(tools.ffprobe, result.path), 6.0,
                               delta=3.0)

    def test_snapshot_last_minutes(self):
        result = self.pipeline.snapshot(SnapshotRequest(
            session_id=self.session.session_id,
            last_minutes=0.15, transcribe=False, name="tail",
        ))
        self.assertTrue(result.path.exists())
        self.assertGreater(result.end, result.start)

    def test_snapshot_index_records_the_cut(self):
        # Self-contained: test methods run in alphabetical order, so this cannot
        # rely on snapshots taken by its siblings.
        result = self.pipeline.snapshot(SnapshotRequest(
            session_id=self.session.session_id,
            start=0.5, end=3.5, transcribe=False, name="indexed",
        ))
        entries = json.loads((self.session.path / "snapshots" / "snapshots.json")
                             .read_text(encoding="utf-8"))
        newest = entries[-1]
        self.assertEqual(newest["file"], result.path.name)
        self.assertIn("clock", newest)
        # `requested_duration` is what was asked for; `duration` is what the file
        # actually holds. They differ: a copy-mode cut can only begin on a
        # keyframe, so it carries up to one GOP of lead-in. Reporting the request
        # as the result is what hid short exports (AUD-017).
        self.assertAlmostEqual(newest["requested_duration"], 3.0, delta=0.01)
        self.assertGreaterEqual(newest["duration"], 3.0)
        self.assertLess(newest["duration"], 3.0 + 2.0)
        probed = media_duration(resolve_tools().ffprobe, result.path)
        self.assertAlmostEqual(newest["duration"], probed, delta=0.05)

    def test_snapshot_rejects_an_inverted_range(self):
        with self.assertRaises(Exception):
            self.pipeline.snapshot(SnapshotRequest(
                session_id=self.session.session_id, start=10.0, end=5.0,
                transcribe=False))

    def test_masters_are_untouched_by_snapshots(self):
        """Non-destructive is the whole contract of the early cut."""
        for chunk in self.session.chunks:
            master = self.session.path / "master" / chunk.master_name
            self.assertAlmostEqual(master.stat().st_size, chunk.size_bytes,
                                   delta=max(4096, chunk.size_bytes * 0.02))

    # -- audio -------------------------------------------------------------

    def test_audio_slices_come_out_at_the_requested_length(self):
        tools = resolve_tools()
        chunk = self.session.chunks[0]
        master = self.session.path / "master" / chunk.master_name
        out = self.tmp / "slice.flac"
        extract_audio_slice(tools, master, out, 1.0, 4.0)
        self.assertTrue(out.exists())
        self.assertAlmostEqual(media_duration(tools.ffprobe, out), 4.0, delta=0.4)


class StubProvider:
    """Returns one word per second of audio, so timings are checkable."""

    def __init__(self):
        self.calls = []

    def transcribe(self, audio: Path):
        from vodpipe.util import run, resolve_tools
        tools = resolve_tools()
        duration = media_duration(tools.ffprobe, audio)
        self.calls.append(round(duration, 2))
        count = max(1, int(duration))
        return [Word(f"w{index}", index + 0.05, 0.5, 0.9) for index in range(count)]


class RollingTranscriptionTests(unittest.TestCase):
    """The rolling transcriber against real media, with a stubbed ASR engine."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="vodpipe-asr-"))
        cls.config = make_config(cls.tmp)
        cls.config.set("transcription.enabled", True)
        cls.config.set("transcription.slice_seconds", 10)
        cls.config.set("transcription.min_slice_seconds", 5)
        cls.config.set("transcription.overlap_seconds", 2)
        cls.tools = resolve_tools()

        # A finished 30s "chunk" on disk.
        cls.session_dir = cls.tmp / "masters" / "stub" / "stub_session"
        (cls.session_dir / "master").mkdir(parents=True)
        (cls.session_dir / "transcripts").mkdir(parents=True)
        cls.media = cls.session_dir / "master" / "stub_c000.mp4"
        from vodpipe.util import run
        run([cls.tools.ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
             "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=30",
             "-f", "lavfi", "-i", "sine=frequency=440",
             "-t", "30", "-c:v", "libx264", "-preset", "ultrafast",
             "-c:a", "aac", "-f", "mp4", str(cls.media)], check=True)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _fixture(self):
        from vodpipe.state import SessionStore
        store = SessionStore(self.config.masters_root)
        session = Session(session_id="stub_session", channel="stub",
                          started_at=time.time(), directory=str(self.session_dir),
                          status="complete")
        chunk = Chunk(index=0, session_id="stub_session", channel="stub",
                      started_at=time.time(), ts_name="stub_c000.ts",
                      master_name="stub_c000.mp4", duration=30.0, status="complete")
        session.chunks.append(chunk)
        store.add(session)

        transcriber = RollingTranscriber(self.config, self.tools, store)
        provider = StubProvider()
        transcriber.provider_override = provider
        return session, chunk, transcriber, provider

    def test_advances_in_slices_until_the_chunk_is_covered(self):
        session, chunk, transcriber, provider = self._fixture()
        for _ in range(6):
            transcriber.advance(session, chunk)
            if chunk.transcribed_through >= 29.0:
                break
        self.assertGreaterEqual(chunk.transcribed_through, 29.0)
        self.assertGreaterEqual(len(provider.calls), 3)

    def test_exports_land_in_the_chunk_folder(self):
        session, chunk, transcriber, _ = self._fixture()
        transcriber.finalize(session, chunk)
        directory = self.session_dir / "transcripts" / "c000"
        for name in ("words.json", "premiere.json", "transcript.srt", "transcript.txt"):
            self.assertTrue((directory / name).exists(), name)
        payload = json.loads((directory / "premiere.json").read_text(encoding="utf-8"))
        self.assertTrue(payload["segments"])

    def test_word_times_are_offset_into_chunk_time(self):
        session, chunk, transcriber, _ = self._fixture()
        transcriber.advance(session, chunk)          # 0-10s
        transcriber.advance(session, chunk)          # 8-20s, overlapping
        words, _ = __import__("vodpipe.transcript", fromlist=["load_words"]).load_words(
            transcriber.words_path(session, chunk))
        self.assertGreater(max(word.start for word in words), 10.0,
                           "second slice should carry times past its own start")
        starts = [word.start for word in words]
        self.assertEqual(starts, sorted(starts), "words must stay in order")

    def test_nothing_new_is_a_no_op(self):
        session, chunk, transcriber, provider = self._fixture()
        chunk.transcribed_through = 30.0
        result = transcriber.advance(session, chunk)
        self.assertEqual(result.status, "idle")
        self.assertEqual(provider.calls, [])


if __name__ == "__main__":
    unittest.main()
