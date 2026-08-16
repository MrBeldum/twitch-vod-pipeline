"""VOD download: URL parsing, command building, state provenance, and an
end-to-end run of a synthetic VOD through the same pipeline as a live recording.

A VOD is fetched by streamlink into the identical ffmpeg segmenter, so the whole
value of these tests is proving the reuse is real: the same chunked masters,
proxies and per-chunk transcript folders come out, and the only difference on
disk is the recorded provenance.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import time
import unittest
from pathlib import Path

from vodpipe import recorder as recorder_module
from vodpipe.channels import InvalidVod, parse_vod, vod_dir_name
from vodpipe.cli import _time_spec
from vodpipe.config import DEFAULTS, Config, deep_merge
from vodpipe.media import format_hls_time, vod_download_command
from vodpipe.pipeline import Pipeline
from vodpipe.state import (
    SOURCE_LIVE,
    SOURCE_VOD,
    Chunk,
    ManifestValidationError,
    Session,
    _validate_manifest,
)
from vodpipe.util import Tools

TOOLS = Tools(ffmpeg="ffmpeg", ffprobe="ffprobe", streamlink="streamlink",
              claude=None)

STREAM_SECONDS = 24
CHUNK_SECONDS = 8


class ParseVodTests(unittest.TestCase):
    def test_canonical_video_url(self):
        vid, url = parse_vod("https://www.twitch.tv/videos/123456789")
        self.assertEqual(vid, "123456789")
        self.assertEqual(url, "https://www.twitch.tv/videos/123456789")

    def test_bare_id(self):
        self.assertEqual(parse_vod("2451234567")[0], "2451234567")

    def test_url_without_scheme(self):
        self.assertEqual(parse_vod("twitch.tv/videos/55")[0], "55")

    def test_legacy_channel_v_form(self):
        self.assertEqual(parse_vod("https://www.twitch.tv/somechan/v/999")[0], "999")

    def test_query_string_is_ignored(self):
        self.assertEqual(parse_vod("https://www.twitch.tv/videos/77?t=1h2m3s")[0], "77")

    def test_leading_zeros_normalised(self):
        self.assertEqual(parse_vod("00042")[0], "42")

    def test_rejects_a_live_channel_url(self):
        with self.assertRaises(InvalidVod):
            parse_vod("https://twitch.tv/somechannel")

    def test_rejects_non_twitch_host(self):
        with self.assertRaises(InvalidVod):
            parse_vod("https://evil.example.com/videos/1")

    def test_rejects_empty(self):
        with self.assertRaises(InvalidVod):
            parse_vod("   ")

    def test_rejects_control_characters(self):
        with self.assertRaises(InvalidVod):
            parse_vod("https://www.twitch.tv/videos/1\x00")


class VodDirNameTests(unittest.TestCase):
    def test_uses_broadcaster_when_valid(self):
        self.assertEqual(vod_dir_name("HasanAbi", "123"), "hasanabi")

    def test_strips_unsafe_characters(self):
        self.assertEqual(vod_dir_name("cool guy!", "123"), "coolguy")

    def test_falls_back_to_vod_id(self):
        self.assertEqual(vod_dir_name("", "123"), "vod_123")
        self.assertEqual(vod_dir_name("!!", "123"), "vod_123")  # too short after strip

    def test_fallback_is_a_valid_channel_name(self):
        from vodpipe.channels import parse_channel
        name = vod_dir_name("", "2451234567")
        self.assertEqual(parse_channel(name), name)


class HlsTimeTests(unittest.TestCase):
    def test_format(self):
        self.assertEqual(format_hls_time(0), "00:00:00.000")
        self.assertEqual(format_hls_time(3661.5), "01:01:01.500")
        self.assertEqual(format_hls_time(-5), "00:00:00.000")

    def test_time_spec_parses_clock_and_seconds(self):
        self.assertEqual(_time_spec("90"), 90.0)
        self.assertEqual(_time_spec("1:30"), 90.0)
        self.assertEqual(_time_spec("1:00:00"), 3600.0)

    def test_time_spec_rejects_garbage(self):
        import argparse
        for bad in ("", "abc", "1:2:3:4", "-5"):
            with self.assertRaises(argparse.ArgumentTypeError):
                _time_spec(bad)


class VodDownloadCommandTests(unittest.TestCase):
    def test_is_not_a_live_command(self):
        cmd = vod_download_command(TOOLS, "https://www.twitch.tv/videos/1", "best")
        # A VOD has no live edge and must not retry forever.
        self.assertNotIn("--hls-live-restart", cmd)
        self.assertNotIn("--twitch-low-latency", cmd)
        joined = " ".join(cmd)
        self.assertNotIn("--retry-max 0", joined)
        self.assertEqual(cmd[-2:], ["https://www.twitch.tv/videos/1", "best"])

    def test_start_and_duration_offsets(self):
        cmd = vod_download_command(
            TOOLS, "https://www.twitch.tv/videos/1", "best",
            start_offset=3661.0, duration=600.0)
        self.assertIn("--hls-start-offset", cmd)
        self.assertEqual(cmd[cmd.index("--hls-start-offset") + 1], "01:01:01.000")
        self.assertIn("--hls-duration", cmd)
        self.assertEqual(cmd[cmd.index("--hls-duration") + 1], "00:10:00.000")

    def test_zero_offsets_are_omitted(self):
        cmd = vod_download_command(
            TOOLS, "https://www.twitch.tv/videos/1", "best",
            start_offset=0.0, duration=0.0)
        self.assertNotIn("--hls-start-offset", cmd)
        self.assertNotIn("--hls-duration", cmd)


class SourceProvenanceStateTests(unittest.TestCase):
    def _session(self, **kw):
        return Session(session_id="c_2026-01-01_000000_abcdef", channel="c",
                       started_at=1.0, directory="/x", **kw)

    def test_defaults_to_live(self):
        self.assertEqual(self._session().source_kind, SOURCE_LIVE)

    def test_round_trips_vod_fields(self):
        session = self._session(source_kind=SOURCE_VOD,
                                source_url="https://www.twitch.tv/videos/9")
        restored = Session.from_dict(session.to_dict())
        self.assertEqual(restored.source_kind, SOURCE_VOD)
        self.assertEqual(restored.source_url, "https://www.twitch.tv/videos/9")

    def test_legacy_manifest_without_source_reads_as_live(self):
        payload = self._session().to_dict()
        del payload["source_kind"]
        del payload["source_url"]
        self.assertEqual(Session.from_dict(payload).source_kind, SOURCE_LIVE)

    def test_manifest_validation_accepts_known_kinds(self):
        root = Path(tempfile.mkdtemp(prefix="vodpipe-vod-state-"))
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        session_dir = root / "somechan" / "somechan_2026-01-01_000000_abcdef"
        session_dir.mkdir(parents=True)
        manifest = session_dir / "session.json"
        session = Session(
            session_id="somechan_2026-01-01_000000_abcdef", channel="somechan",
            started_at=1.0, directory=str(session_dir.resolve()), status="complete",
            source_kind=SOURCE_VOD, source_url="https://www.twitch.tv/videos/9")
        _validate_manifest(session.to_dict(), root, manifest)   # must not raise

    def test_manifest_validation_rejects_unknown_kind(self):
        root = Path(tempfile.mkdtemp(prefix="vodpipe-vod-state-"))
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        session_dir = root / "somechan" / "somechan_2026-01-01_000000_abcdef"
        session_dir.mkdir(parents=True)
        manifest = session_dir / "session.json"
        payload = Session(
            session_id="somechan_2026-01-01_000000_abcdef", channel="somechan",
            started_at=1.0, directory=str(session_dir.resolve()),
            status="complete").to_dict()
        payload["source_kind"] = "bogus"
        with self.assertRaises(ManifestValidationError):
            _validate_manifest(payload, root, manifest)


def fake_stream_command(tools, url, quality, **options):
    """Stand-in for streamlink/vod_download_command: real-time MPEG-TS on stdout."""
    return [
        tools.ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin",
        "-re",
        "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30",
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
    (root / "censor.txt").write_text("damn\n", encoding="utf-8")
    return Config(data, root / "config.json")


class VodPipelineEndToEndTests(unittest.TestCase):
    """A synthetic VOD driven through the identical live-recording pipeline."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="vodpipe-vod-e2e-"))
        cls.config = make_config(cls.tmp)

        cls._real_vod_cmd = recorder_module.vod_download_command
        recorder_module.vod_download_command = fake_stream_command

        cls.pipeline = Pipeline(cls.config)
        # Never touch the network: return a fixed broadcaster and title.
        cls.pipeline._probe_vod_metadata = lambda url: ("teststreamer", "My VOD")
        cls.pipeline.start()
        cls.session = cls.pipeline.download_vod(
            "https://www.twitch.tv/videos/246810")

        deadline = time.time() + STREAM_SECONDS + 45
        while cls.session.status in ("starting", "recording") and time.time() < deadline:
            time.sleep(1)
        deadline = time.time() + 180
        while cls.pipeline.active_jobs() and time.time() < deadline:
            time.sleep(1)

    @classmethod
    def tearDownClass(cls):
        recorder_module.vod_download_command = cls._real_vod_cmd
        try:
            cls.pipeline.shutdown()
        finally:
            shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_download_completed(self):
        self.assertEqual(self.session.status, "complete", self.session.error)

    def test_recorded_as_a_vod(self):
        self.assertEqual(self.session.source_kind, SOURCE_VOD)
        self.assertEqual(self.session.source_url,
                         "https://www.twitch.tv/videos/246810")

    def test_channel_came_from_metadata(self):
        self.assertEqual(self.session.channel, "teststreamer")
        self.assertEqual(self.session.title, "My VOD")

    def test_split_into_multiple_chunks(self):
        self.assertGreaterEqual(len(self.session.chunks), 2)

    def test_every_chunk_produced_a_master(self):
        for chunk in self.session.chunks:
            master = self.session.path / "master" / chunk.master_name
            self.assertTrue(master.exists(), f"{chunk.label} master missing")
            self.assertEqual(chunk.status, "complete", str(chunk.errors))

    def test_proxies_were_generated(self):
        for chunk in self.session.chunks:
            master = self.session.path / "master" / chunk.master_name
            proxy = master.parent / "Proxies" / f"{master.stem}_Proxy.mp4"
            self.assertTrue(proxy.exists(), f"{chunk.label} proxy missing")

    def test_session_json_round_trips_source_fields(self):
        payload = json.loads((self.session.path / "session.json")
                             .read_text(encoding="utf-8"))
        restored = Session.from_dict(payload)
        self.assertEqual(restored.source_kind, SOURCE_VOD)
        self.assertEqual(restored.source_url, self.session.source_url)

    def test_store_recovers_the_vod_session(self):
        from vodpipe.state import SessionStore
        store = SessionStore(self.config.masters_root)
        store.load_from_disk()
        restored = store.get(self.session.session_id)
        self.assertIsNotNone(restored)
        self.assertEqual(restored.source_kind, SOURCE_VOD)

    def test_a_second_download_of_the_same_vod_is_refused_while_running(self):
        # A completed download does not block a re-run, but a running one must.
        recorder_module.vod_download_command = fake_stream_command
        try:
            running = self.pipeline.download_vod(
                "https://www.twitch.tv/videos/999001")
            with self.assertRaises(RuntimeError):
                self.pipeline.download_vod("https://www.twitch.tv/videos/999001")
        finally:
            try:
                self.pipeline.stop_vod(running.session_id)
            except RuntimeError:
                pass


class VodTranscriptionReuseTests(unittest.TestCase):
    """A VOD session is transcribed by the same source-agnostic transcriber."""

    def test_transcriber_advances_a_vod_chunk(self):
        from vodpipe.state import SessionStore
        from vodpipe.transcribe import RollingTranscriber
        from vodpipe.transcript import Word
        from vodpipe.util import media_duration, resolve_tools

        tmp = Path(tempfile.mkdtemp(prefix="vodpipe-vod-asr-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        config = make_config(tmp)
        config.set("transcription.enabled", True)
        config.set("transcription.slice_seconds", 10)
        config.set("transcription.min_slice_seconds", 5)
        tools = resolve_tools()

        session_dir = tmp / "masters" / "vod_5" / "vod_5_session"
        (session_dir / "master").mkdir(parents=True)
        (session_dir / "transcripts").mkdir(parents=True)
        media = session_dir / "master" / "vod_5_c000.mp4"
        from vodpipe.util import run
        run([tools.ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
             "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=30",
             "-f", "lavfi", "-i", "sine=frequency=440",
             "-t", "20", "-c:v", "libx264", "-preset", "ultrafast",
             "-c:a", "aac", "-f", "mp4", str(media)], check=True)

        store = SessionStore(config.masters_root)
        session = Session(session_id="vod_5_session", channel="vod_5",
                          started_at=time.time(), directory=str(session_dir),
                          status="complete", source_kind=SOURCE_VOD,
                          source_url="https://www.twitch.tv/videos/5")
        chunk = Chunk(index=0, session_id="vod_5_session", channel="vod_5",
                      started_at=time.time(), ts_name="vod_5_c000.ts",
                      master_name="vod_5_c000.mp4", duration=20.0,
                      status="complete")
        session.chunks.append(chunk)
        store.add(session)

        transcriber = RollingTranscriber(config, tools, store)

        class Stub:
            def transcribe(self, audio, *, expected_duration=None):
                duration = media_duration(tools.ffprobe, audio)
                return [Word(f"w{i}", i + 0.05, 0.5, 0.9)
                        for i in range(max(1, int(duration)))]

        transcriber.provider_override = Stub()
        transcriber.finalize(session, chunk)
        self.assertEqual(chunk.transcript_status, "done")
        self.assertTrue((session_dir / "transcripts" / "c000" / "premiere.json").exists())


if __name__ == "__main__":
    unittest.main()
