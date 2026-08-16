"""Work scheduling and command-line ergonomics (AUD-032, AUD-033, AUD-034).

One FIFO pool served rolling transcription, remuxes, proxy transcodes and
rundowns alike, so a quarter-hour `claude -p` call could sit in front of the
transcript slice for a channel that was still recording. Channel probes ran one
after another, each able to block for a minute, so the tenth channel on a watch
list was checked minutes after the first. And the CLI insisted on tools and
secrets that the command in front of it had no use for.
"""

from __future__ import annotations

import io
import shutil
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from vodpipe.cli import cmd_doctor
from vodpipe.config import DEFAULTS, Config, deep_merge
from vodpipe.exports import write_exports
from vodpipe.state import DONE, Chunk, Session
from vodpipe.transcript import Word
from vodpipe.util import resolve_tools


def make_config(root: Path, **overlay) -> Config:
    base = {
        "paths": {"masters_root": str(root / "m"), "work_root": str(root / "w"),
                  "censor_master_list": str(root / "none.txt")},
        "watcher": {"enabled": False},
        "transcription": {"enabled": False},
        "summary": {"provider": "none"},
    }
    return Config(deep_merge(DEFAULTS, deep_merge(base, overlay)),
                  root / "config.json")


class PipelineFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="vodpipe-sched-"))
        self.config = make_config(self.tmp)
        self.config.masters_root.mkdir(parents=True, exist_ok=True)
        from vodpipe.pipeline import Pipeline
        self.pipeline = Pipeline(self.config)
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.addCleanup(lambda: [pool.stop(timeout=10, drain=False)
                                 for pool in self.pipeline.pools])

        session_dir = self.tmp / "m" / "chan" / "sess"
        session_dir.mkdir(parents=True)
        self.session = Session(session_id="sess", channel="chan",
                               started_at=time.time(),
                               directory=str(session_dir), status="complete")
        self.chunk = Chunk(index=0, session_id="sess", channel="chan",
                           started_at=time.time(), master_name="chan_c000.mp4",
                           duration=60.0, status="complete")
        self.session.chunks.append(self.chunk)
        self.pipeline.store.add(self.session)


class PoolSeparationTests(PipelineFixture):
    def test_rundowns_do_not_occupy_a_capture_critical_worker(self):
        """The regression: a 15-minute summary blocked rolling transcription."""
        self.config.set("summary.provider", "claude-cli")
        self.config.set("summary.min_words", 1)
        output = self.pipeline.transcriber.output_dir(self.session, self.chunk)
        write_exports(
            output, [Word("speech", 0.0, 0.5, 0.9)],
            meta={"chunk": self.chunk.label, "complete": True},
            words_meta={
                "chunk": self.chunk.label, "complete": True,
                "covered_seconds": self.chunk.duration,
                "expected_seconds": self.chunk.duration,
            },
        )
        self.chunk.transcript_status = DONE
        running = threading.Event()
        release = threading.Event()
        self.addCleanup(release.set)

        def slow_summary(session, chunk, generation=None):
            running.set()
            release.wait(20)

        self.pipeline._summarize = slow_summary
        self.pipeline._queue_summary(self.session, self.chunk)
        self.assertTrue(running.wait(10))

        self.assertEqual(self.pipeline.media_jobs.active_count(), 1)
        self.assertEqual(self.pipeline.jobs.active_count(), 0)

    def test_proxies_run_on_the_media_pool(self):
        blocked = threading.Event()
        release = threading.Event()
        self.addCleanup(release.set)

        def slow_proxy(job, session, chunk):
            blocked.set()
            release.wait(20)

        self.pipeline._make_proxy = slow_proxy
        self.pipeline._queue_proxy(self.session, self.chunk)
        self.assertTrue(blocked.wait(10))
        self.assertEqual(self.pipeline.media_jobs.active_count(), 1)
        self.assertEqual(self.pipeline.jobs.active_count(), 0)

    def test_the_dashboard_sees_every_pool_in_one_list(self):
        self.pipeline.jobs.submit("a", "capture work", "transcribe",
                                  lambda job: None)
        self.pipeline.media_jobs.submit("b", "heavy work", "proxy",
                                        lambda job: None)
        self.pipeline.snapshot_jobs.submit("c", "a cut", "snapshot",
                                           lambda job: None)
        self.pipeline.probe_jobs.submit("d", "a live probe", "probe",
                                        lambda job: None)
        deadline = time.time() + 10
        while self.pipeline.active_jobs() and time.time() < deadline:
            time.sleep(0.02)

        labels = {job["label"] for job in self.pipeline.job_snapshot()}
        self.assertEqual(labels, {
            "capture work", "heavy work", "a cut", "a live probe"})

    def test_merged_jobs_are_newest_first(self):
        self.pipeline.jobs.submit("old", "first", "transcribe", lambda job: None)
        time.sleep(0.02)
        self.pipeline.media_jobs.submit("new", "second", "proxy", lambda job: None)
        merged = self.pipeline.job_snapshot()
        self.assertEqual(merged[0]["label"], "second")

    def test_active_jobs_counts_every_pool(self):
        release = threading.Event()
        self.addCleanup(release.set)
        for pool, key in ((self.pipeline.jobs, "a"),
                          (self.pipeline.media_jobs, "b"),
                          (self.pipeline.snapshot_jobs, "c"),
                          (self.pipeline.probe_jobs, "d")):
            pool.submit(key, key, "test", lambda job: release.wait(20))
        deadline = time.time() + 10
        while self.pipeline.active_jobs() < 4 and time.time() < deadline:
            time.sleep(0.02)
        self.assertEqual(self.pipeline.active_jobs(), 4)


class ProbeConcurrencyTests(PipelineFixture):
    def test_channels_are_probed_in_parallel(self):
        """Sequentially, the last channel on a long list is checked far too late."""
        seen: list[str] = []
        guard = threading.Lock()

        def slow_check(channel: str) -> None:
            time.sleep(0.4)
            with guard:
                seen.append(channel)

        self.pipeline._check_channel = slow_check
        channels = ["a", "b", "c", "d"]

        started = time.time()
        self.pipeline._check_channels(channels)
        deadline = time.time() + 10
        while self.pipeline.probe_jobs.active_count() and time.time() < deadline:
            time.sleep(0.02)
        elapsed = time.time() - started

        self.assertEqual(sorted(seen), channels)
        self.assertLess(elapsed, 1.2,
                        "four 0.4s probes run together should not take 1.6s")

    def test_one_failing_probe_does_not_stop_the_others(self):
        seen: list[str] = []

        def check(channel: str) -> None:
            if channel == "b":
                raise RuntimeError("streamlink fell over")
            seen.append(channel)

        self.pipeline._check_channel = check
        self.pipeline._check_channels(["a", "b", "c"])
        deadline = time.time() + 10
        while self.pipeline.probe_jobs.active_count() and time.time() < deadline:
            time.sleep(0.02)
        self.assertEqual(sorted(seen), ["a", "c"])

    def test_an_empty_watch_list_is_harmless(self):
        self.pipeline._check_channels([])

    def test_shutdown_stops_further_probes(self):
        seen: list[str] = []
        self.pipeline._check_channel = seen.append
        self.pipeline._stop.set()
        self.pipeline._check_channels(["a", "b"])
        self.assertEqual(seen, [])

    def test_three_blocked_probes_cannot_delay_capture_work(self):
        entered = threading.Barrier(4)
        release = threading.Event()
        self.addCleanup(release.set)
        for channel in ("a", "b", "c"):
            self.pipeline.probe_jobs.submit(
                f"blocked:{channel}", channel, "probe",
                lambda job: (entered.wait(10), release.wait(20)))
        entered.wait(10)

        captured = threading.Event()
        self.pipeline.jobs.submit(
            "capture-now", "capture", "transcribe", lambda job: captured.set())

        self.assertTrue(captured.wait(2),
                        "probe workers must never occupy capture-critical slots")


class ToolResolutionTests(unittest.TestCase):
    """AUD-034: only insist on the tools the command actually uses."""

    def test_transcribing_a_local_file_does_not_need_streamlink(self):
        tools = resolve_tools({"streamlink": r"C:\nothing\here.exe"},
                              need=("ffmpeg", "ffprobe"))
        self.assertTrue(tools.ffmpeg)
        self.assertEqual(tools.streamlink, "")

    def test_recording_still_insists_on_streamlink(self):
        with self.assertRaises(RuntimeError) as caught:
            resolve_tools({"streamlink": r"C:\nothing\here.exe"})
        self.assertIn("streamlink", str(caught.exception))

    def test_the_default_is_everything(self):
        tools = resolve_tools()
        self.assertTrue(tools.ffmpeg and tools.ffprobe and tools.streamlink)


class DoctorTests(unittest.TestCase):
    """AUD-034: doctor judges the configuration that is switched on."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="vodpipe-doc-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def run_doctor(self, **overlay):
        config = make_config(self.tmp, **overlay)
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = cmd_doctor(config)
        return code, buffer.getvalue()

    def test_no_deepgram_key_is_fine_when_transcription_is_off(self):
        """The regression: doctor failed a perfectly working configuration."""
        code, output = self.run_doctor(
            transcription={"enabled": False},
            recording={"free_space_floor_gb": 0})
        self.assertEqual(code, 0, output)
        self.assertIn("Ready.", output)

    def test_a_missing_deepgram_key_still_fails_when_transcription_is_on(self):
        code, output = self.run_doctor(
            transcription={"enabled": True},
            recording={"free_space_floor_gb": 0})
        self.assertEqual(code, 1)
        self.assertIn("MISSING", output)

    def test_an_enabled_api_summariser_without_a_key_is_reported(self):
        """And this used to pass silently, for a setup that cannot work."""
        code, output = self.run_doctor(
            transcription={"enabled": False},
            summary={"enabled": True, "provider": "anthropic-api"},
            recording={"free_space_floor_gb": 0})
        self.assertEqual(code, 1)
        self.assertIn("anthropic_api_key", output)

    def test_an_api_summariser_with_a_key_passes(self):
        code, output = self.run_doctor(
            transcription={"enabled": False},
            summary={"enabled": True, "provider": "anthropic-api"},
            secrets={"anthropic_api_key": "sk-test"},
            recording={"free_space_floor_gb": 0})
        self.assertEqual(code, 0, output)

    def test_features_are_reported(self):
        _, output = self.run_doctor(recording={"free_space_floor_gb": 0})
        self.assertIn("Features", output)
        self.assertIn("transcription off", output)

    def test_a_disabled_summariser_does_not_demand_the_claude_cli(self):
        code, output = self.run_doctor(
            transcription={"enabled": False},
            summary={"enabled": False},
            tools={"claude": r"C:\nothing\here.exe"},
            recording={"free_space_floor_gb": 0})
        self.assertEqual(code, 0, output)
        self.assertIn("absent", output)


if __name__ == "__main__":
    unittest.main()
