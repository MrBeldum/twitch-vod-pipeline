"""Record means "record when there is something to record".

Pressing Record used to launch streamlink regardless of whether the channel was
broadcasting. streamlink is configured to retry forever -- correct once a stream
exists, so a momentary drop is waited out -- so an offline channel produced a
session that sat at `recording`, holding an empty chunk and a channel lock, for
as long as the application ran. The dashboard reported it as recording, because
as far as the pipeline was concerned it was.

A channel that is not known to be live is now *armed* instead: no process, no
session directory, no lock. The watcher starts the real recording on the pass
that first sees it live.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from vodpipe.config import DEFAULTS, Config, deep_merge
from vodpipe.pipeline import LIVE, OFFLINE, UNKNOWN
from vodpipe.state import STARTING, Session


class _RunningRecorder:
    """Minimal stand-in for a live Recorder."""

    running = True

    def __init__(self, session=None, request_token=""):
        self.stopped: list[str] = []
        self.session = session
        self.request_token = request_token

    def stop(self, reason: str = "") -> None:
        self.stopped.append(reason)
        self.running = False

    def measured_head_position(self) -> float:
        return 0.0


def make_config(root: Path, **overlay) -> Config:
    base = {
        "paths": {"masters_root": str(root / "m"), "work_root": str(root / "w"),
                  "censor_master_list": str(root / "none.txt")},
        # Off, so probes only happen when a test asks for one.
        "watcher": {"enabled": False},
        "transcription": {"enabled": False},
        "summary": {"provider": "none"},
    }
    return Config(deep_merge(DEFAULTS, deep_merge(base, overlay)),
                  root / "config.json")


class ArmingFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="vodpipe-arm-"))
        self.config = make_config(self.tmp)
        self.config.masters_root.mkdir(parents=True, exist_ok=True)
        from vodpipe.pipeline import Pipeline
        self.pipeline = Pipeline(self.config)
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.addCleanup(lambda: [pool.stop(timeout=10, drain=False)
                                 for pool in self.pipeline.pools])

        # No real streamlink and no real recorder anywhere in this file.
        self.started: list[str] = []
        self.confirm_on_start = True
        self.pipeline.start_recording = self.fake_start
        self.live: dict[str, str] = {}
        self.pipeline._probe_live = lambda channel: (
            self.live.get(channel, OFFLINE), "a title")

    def fake_start(self, channel: str, *, request_id: str = "") -> Session:
        """Stands in for the real thing, including registering the recorder.

        Registering matters: `_check_channel` skips a channel whose recorder is
        already running, and a fake that skipped that step would let the same
        channel start twice in tests for reasons production never has.
        """
        self.started.append(channel)
        session_id = f"sess-{channel}-{len(self.started)}"
        session = Session(session_id=session_id,
                          channel=channel, started_at=time.time(),
                          directory=str(self.tmp / "m" / channel / session_id),
                          status=STARTING)
        Path(session.directory).mkdir(parents=True, exist_ok=True)
        self.pipeline.store.add(session)
        self.pipeline._recorders[channel] = _RunningRecorder(session, request_id)
        if request_id and self.confirm_on_start:
            self.pipeline.store.confirm_first_media(session)
            self.pipeline._on_first_media(session, request_id)
        return session

    def set_live(self, channel: str, live: bool, *, age: float = 0.0):
        """Seed the cached live status the request path consults."""
        self.live[channel] = LIVE if live else OFFLINE
        self.pipeline._live_status[channel] = {
            "state": LIVE if live else OFFLINE,
            "live": live, "title": "", "checked_at": time.time() - age}

    def settle(self, timeout: float = 10.0):
        """Let the probe queued by request_recording finish.

        Arming kicks a background probe so a stale `offline` costs seconds rather
        than a full watcher interval. Tests that then flip the channel live have
        to wait for it, or they race it.
        """
        deadline = time.time() + timeout
        while self.pipeline.active_jobs() and time.time() < deadline:
            time.sleep(0.02)


class RequestTests(ArmingFixture):
    def test_an_offline_channel_is_armed_not_started(self):
        """The regression, stated plainly."""
        self.set_live("someone", False)
        outcome = self.pipeline.request_recording("someone")

        self.assertEqual(outcome["state"], "armed")
        self.assertEqual(self.started, [], "nothing should have been launched")
        self.assertTrue(self.pipeline.is_armed("someone"))

    def test_a_live_channel_starts_immediately(self):
        self.set_live("someone", True)
        outcome = self.pipeline.request_recording("someone")

        self.assertEqual(outcome["state"], STARTING)
        self.assertTrue(outcome["session_id"])
        self.assertEqual(self.started, ["someone"])
        self.assertFalse(self.pipeline.is_armed("someone"))

    def test_an_unknown_channel_is_armed_rather_than_assumed_live(self):
        """Never probed, so nothing is known -- arming is the safe reading."""
        outcome = self.pipeline.request_recording("neverseen")
        self.assertEqual(outcome["state"], "armed")
        self.assertEqual(self.started, [])

    def test_a_stale_live_reading_is_not_trusted(self):
        self.set_live("someone", True, age=10_000)
        outcome = self.pipeline.request_recording("someone")
        self.assertEqual(outcome["state"], "armed")

    def test_the_channel_name_is_normalised(self):
        outcome = self.pipeline.request_recording("https://twitch.tv/SomeOne")
        self.assertEqual(outcome["channel"], "someone")
        self.assertTrue(self.pipeline.is_armed("someone"))

    def test_arming_a_channel_that_is_recording_is_refused(self):
        self.set_live("someone", True)
        self.pipeline.request_recording("someone")

        class Running:
            running = True

        self.pipeline._recorders["someone"] = Running()
        with self.assertRaises(RuntimeError) as caught:
            self.pipeline.request_recording("someone")
        self.assertIn("already recording", str(caught.exception))

    def test_a_shutting_down_pipeline_refuses(self):
        self.pipeline._stop.set()
        with self.assertRaises(RuntimeError):
            self.pipeline.request_recording("someone")


class FiringTests(ArmingFixture):
    def test_going_live_starts_the_armed_channel(self):
        self.set_live("someone", False)
        self.pipeline.request_recording("someone")
        self.settle()
        self.assertEqual(self.started, [])

        self.live["someone"] = LIVE
        self.pipeline._check_channel("someone", force=True)

        self.assertEqual(self.started, ["someone"])
        self.assertFalse(self.pipeline.is_armed("someone"),
                         "a fired request should not fire twice")

    def test_staying_offline_starts_nothing(self):
        self.pipeline.request_recording("someone")
        self.settle()
        for _ in range(3):
            self.pipeline._check_channel("someone", force=True)
        self.assertEqual(self.started, [])
        self.assertTrue(self.pipeline.is_armed("someone"))

    def test_arming_beats_auto_record_being_off(self):
        """An explicit instruction outranks a setting chosen earlier."""
        self.pipeline.add_channel("someone")
        self.pipeline.set_channel_setting("someone", "auto_record", False)
        self.pipeline.request_recording("someone")
        self.settle()

        self.live["someone"] = LIVE
        self.pipeline._check_channel("someone", force=True)
        self.assertEqual(self.started, ["someone"])

    def test_auto_record_off_and_unarmed_still_starts_nothing(self):
        self.pipeline.add_channel("someone")
        self.pipeline.set_channel_setting("someone", "auto_record", False)
        self.live["someone"] = LIVE
        self.pipeline._check_channel("someone", force=True)
        self.assertEqual(self.started, [])

    def test_a_failed_start_stays_armed_for_the_next_pass(self):
        """A momentary disk dip should be retried, not silently forgotten."""
        def failing_start(channel, *, request_id=""):
            raise RuntimeError("not enough disk")

        self.pipeline.start_recording = failing_start
        self.pipeline.request_recording("someone")
        self.settle()
        self.live["someone"] = LIVE
        self.pipeline._check_channel("someone", force=True)

        self.assertTrue(self.pipeline.is_armed("someone"))

        self.pipeline.start_recording = self.fake_start
        self.pipeline._check_channel("someone", force=True)
        self.assertEqual(self.started, ["someone"])

    def test_an_armed_channel_is_probed_even_with_the_watcher_off(self):
        self.config.set("watcher.enabled", False)
        self.pipeline.request_recording("someone")
        self.assertIn("someone", self.pipeline._channels_to_probe())

    def test_an_armed_channel_need_not_be_on_the_watch_list(self):
        self.pipeline.request_recording("stranger")
        self.assertNotIn("stranger", self.pipeline.channels())
        self.assertIn("stranger", self.pipeline._channels_to_probe())

    def test_the_watch_list_is_not_probed_when_the_watcher_is_off(self):
        self.pipeline.add_channel("watched")
        self.assertEqual(self.pipeline._channels_to_probe(), [])


class CancellationTests(ArmingFixture):
    def test_stop_cancels_a_pending_request(self):
        self.pipeline.request_recording("someone")
        self.settle()
        self.pipeline.stop_recording("someone")

        self.assertFalse(self.pipeline.is_armed("someone"))
        # The mechanism, not a stand-in for it: a cancelled channel that is not
        # on the watch list drops out of the probe set entirely, so going live
        # never reaches _check_channel at all.
        self.assertNotIn("someone", self.pipeline._channels_to_probe())

    def test_a_cancelled_request_does_not_fire_on_a_watched_channel(self):
        """Watched, auto off: probed, but there is no longer a reason to start."""
        self.pipeline.add_channel("someone")
        self.pipeline.set_channel_setting("someone", "auto_record", False)
        self.pipeline.request_recording("someone")
        self.settle()
        self.pipeline.stop_recording("someone")

        self.live["someone"] = LIVE
        self.pipeline._check_channel("someone", force=True)
        self.assertEqual(self.started, [], "a cancelled request must not fire")

    def test_stop_on_an_idle_channel_still_reports_clearly(self):
        with self.assertRaises(RuntimeError) as caught:
            self.pipeline.stop_recording("someone")
        self.assertIn("not recording", str(caught.exception))

    def test_removing_a_channel_withdraws_its_pending_request(self):
        self.pipeline.add_channel("someone")
        self.pipeline.request_recording("someone")
        self.pipeline.remove_channel("someone")
        self.assertFalse(self.pipeline.is_armed("someone"))

    def test_stopping_a_live_recording_also_withdraws_the_request(self):
        """Otherwise the watcher would helpfully start it again."""
        stopped = []

        class Running:
            running = True

            def stop(self, reason=""):
                stopped.append(reason)

        self.set_live("someone", False)
        self.pipeline.request_recording("someone")
        self.settle()
        self.pipeline._recorders["someone"] = Running()
        self.pipeline.stop_recording("someone")

        self.assertTrue(stopped)
        self.assertFalse(self.pipeline.is_armed("someone"))


class VisibilityTests(ArmingFixture):
    def test_an_armed_channel_is_reported_to_the_dashboard(self):
        self.pipeline.add_channel("someone")
        self.pipeline.request_recording("someone")

        entry = next(item for item in self.pipeline.state_payload()["channels"]
                     if item["name"] == "someone")
        self.assertTrue(entry["armed"])
        self.assertFalse(entry["recording"])

    def test_an_armed_channel_off_the_watch_list_is_still_visible(self):
        """A pending request the user cannot see is one they cannot cancel."""
        self.pipeline.request_recording("stranger")
        names = [item["name"] for item in self.pipeline.state_payload()["channels"]]
        self.assertIn("stranger", names)

    def test_an_ordinary_channel_is_not_marked_armed(self):
        self.pipeline.add_channel("quiet")
        entry = next(item for item in self.pipeline.state_payload()["channels"]
                     if item["name"] == "quiet")
        self.assertFalse(entry["armed"])

    def test_starting_request_identity_is_visible_to_the_dashboard(self):
        self.confirm_on_start = False
        self.set_live("someone", True)
        outcome = self.pipeline.request_recording("someone")

        entry = next(item for item in self.pipeline.state_payload()["channels"]
                     if item["name"] == "someone")

        self.assertTrue(entry["armed"])
        self.assertTrue(entry["recording"])
        self.assertTrue(entry["starting"])
        self.assertEqual(entry["live_state"], LIVE)
        self.assertEqual(entry["request_id"], outcome["request_id"])


class RequestLifecycleBarrierTests(ArmingFixture):
    def test_request_ids_are_unique_and_cancellation_is_durable(self):
        first = self.pipeline.request_recording("someone")
        self.settle()
        self.pipeline.stop_recording("someone")
        second = self.pipeline.request_recording("someone")

        self.assertNotEqual(first["request_id"], second["request_id"])
        result = self.pipeline.request_result(first["request_id"])
        self.assertEqual(result["status"], "cancelled")

    def test_intent_completes_only_after_first_media(self):
        self.confirm_on_start = False
        self.set_live("someone", True)
        outcome = self.pipeline.request_recording("someone")
        session = self.pipeline.store.get(outcome["session_id"])

        self.assertEqual(session.status, STARTING)
        self.assertTrue(self.pipeline.is_armed("someone"))
        self.assertEqual(
            self.pipeline.request_result(outcome["request_id"])["status"],
            STARTING)

        self.pipeline.store.confirm_first_media(session)
        self.pipeline._on_first_media(session, outcome["request_id"])

        self.assertFalse(self.pipeline.is_armed("someone"))
        result = self.pipeline.wait_for_request(outcome["request_id"], timeout=0)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["session_id"], session.session_id)

    def test_cancel_during_forced_probe_invalidates_exact_token(self):
        entered = threading.Event()
        release = threading.Event()
        self.addCleanup(release.set)

        def blocked_probe(channel):
            entered.set()
            release.wait(10)
            return LIVE, "title"

        self.pipeline._probe_live = blocked_probe
        outcome = self.pipeline.request_recording("someone")
        self.assertTrue(entered.wait(5))

        self.assertTrue(self.pipeline.cancel_request(outcome["request_id"]))
        release.set()
        self.settle()

        self.assertEqual(self.started, [])
        self.assertEqual(
            self.pipeline.request_result(outcome["request_id"])["status"],
            "cancelled")

    def test_remove_during_regular_probe_blocks_unarmed_auto_start(self):
        self.config.set("watcher.enabled", True)
        self.pipeline.add_channel("someone")
        entered = threading.Event()
        release = threading.Event()
        self.addCleanup(release.set)

        def blocked_probe(channel):
            entered.set()
            release.wait(10)
            return LIVE, "title"

        self.pipeline._probe_live = blocked_probe
        self.pipeline._check_channels(["someone"])
        self.assertTrue(entered.wait(5))
        self.pipeline.remove_channel("someone")
        release.set()
        self.settle()

        self.assertEqual(self.started, [])

    def test_stale_first_media_callback_cannot_complete_newer_request(self):
        old = self.pipeline.request_recording("someone")
        self.settle()
        self.pipeline.stop_recording("someone")
        newer = self.pipeline.request_recording("someone")
        self.settle()
        stale = Session(
            session_id="stale-session", channel="someone", started_at=time.time(),
            directory=str(self.tmp / "m" / "someone" / "stale-session"),
            status="recording")

        self.pipeline._on_first_media(stale, old["request_id"])

        self.assertTrue(self.pipeline.is_armed("someone"))
        self.assertEqual(
            self.pipeline.request_result(newer["request_id"])["status"],
            "pending")

    def test_cached_live_zero_byte_attempt_rearms_same_request(self):
        self.confirm_on_start = False
        self.set_live("someone", True)
        outcome = self.pipeline.request_recording("someone")
        session = self.pipeline.store.get(outcome["session_id"])
        self.live["someone"] = OFFLINE
        self.pipeline.store.update(
            session, status="failed", ended_at=time.time(),
            error="no video arrived")

        self.pipeline._on_session_ended(session, outcome["request_id"])
        self.settle()

        self.assertTrue(self.pipeline.is_armed("someone"))
        pending = self.pipeline.request_result(outcome["request_id"])
        self.assertEqual(pending["status"], "pending")
        self.assertEqual(pending["session_id"], "")
        self.assertIn("no video", pending["error"])

    def test_unknown_does_not_clear_suppression_but_offline_does(self):
        with self.pipeline._lifecycle:
            self.pipeline._auto_suppressed.add("someone")
            self.pipeline._persist_control_locked()
        states = iter(((UNKNOWN, ""), (OFFLINE, "")))
        self.pipeline._probe_live = lambda channel: next(states)

        self.pipeline._check_channel("someone", force=True)
        self.assertTrue(self.pipeline.is_auto_suppressed("someone"))
        self.pipeline._check_channel("someone", force=True)
        self.assertFalse(self.pipeline.is_auto_suppressed("someone"))

    def test_refused_probe_submission_rolls_back_intent(self):
        with mock.patch.object(self.pipeline.probe_jobs, "submit", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "was not accepted") as caught:
                self.pipeline.request_recording("someone")

        request_id = str(caught.exception).split()[2]
        self.assertFalse(self.pipeline.is_armed("someone"))
        self.assertEqual(
            self.pipeline.request_result(request_id)["status"], "error")

    def test_shutdown_waits_for_request_admission_transition(self):
        entered = threading.Event()
        release = threading.Event()
        self.addCleanup(release.set)
        original = self.pipeline._persist_control_locked

        def paused_persist():
            entered.set()
            release.wait(10)
            original()

        self.pipeline._persist_control_locked = paused_persist
        outcomes = []
        errors = []
        requester = threading.Thread(target=lambda: self._capture_call(
            lambda: self.pipeline.request_recording("someone"), outcomes, errors))
        requester.start()
        self.assertTrue(entered.wait(5))
        stopped = threading.Event()
        shutdown = threading.Thread(target=lambda: (
            self.pipeline.shutdown(job_timeout=10, thread_timeout=10,
                                   recorder_timeout=10), stopped.set()))
        shutdown.start()

        self.assertFalse(stopped.wait(0.1),
                         "shutdown must not split request admission in half")
        release.set()
        requester.join(10)
        shutdown.join(20)

        self.assertEqual(errors, [])
        self.assertEqual(len(outcomes), 1)
        self.assertTrue(stopped.is_set())

    @staticmethod
    def _capture_call(call, outcomes, errors):
        try:
            outcomes.append(call())
        except Exception as exc:
            errors.append(exc)

    def test_probe_runner_stops_as_producer_before_recorders(self):
        observed = []

        class Recorder:
            running = True
            session = None

            def stop(inner, reason=""):
                observed.append(self.pipeline.probe_jobs.accepting)
                inner.running = False

            def join(inner, timeout=None):
                pass

        self.pipeline._recorders["someone"] = Recorder()
        self.pipeline.shutdown(job_timeout=10, thread_timeout=10,
                               recorder_timeout=10)

        self.assertEqual(observed, [False])

    def test_pending_intent_and_suppression_survive_restart(self):
        outcome = self.pipeline.request_recording("someone")
        self.settle()
        with self.pipeline._lifecycle:
            self.pipeline._auto_suppressed.add("other")
            self.pipeline._persist_control_locked()
        self.pipeline.shutdown()

        from vodpipe.pipeline import Pipeline
        restored = Pipeline(self.config)
        self.addCleanup(lambda: [pool.stop(timeout=10, drain=False)
                                 for pool in restored.pools])

        self.assertTrue(restored.is_armed("someone"))
        self.assertTrue(restored.is_auto_suppressed("other"))
        self.assertEqual(
            restored.request_result(outcome["request_id"])["status"], "pending")

    def test_terminal_result_survives_restart(self):
        self.set_live("someone", True)
        outcome = self.pipeline.request_recording("someone")
        self.pipeline._recorders.clear()

        from vodpipe.pipeline import Pipeline
        restored = Pipeline(self.config)
        self.addCleanup(lambda: [pool.stop(timeout=10, drain=False)
                                 for pool in restored.pools])
        result = restored.request_result(outcome["request_id"])

        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["session_id"], outcome["session_id"])

    def test_fast_terminal_session_cli_wait_uses_request_result(self):
        from vodpipe.cli import _wait_for_session

        self.set_live("someone", True)
        outcome = self.pipeline.request_recording("someone")
        session = self.pipeline.store.get(outcome["session_id"])
        self.pipeline.store.update(
            session, status="complete", ended_at=time.time())
        self.pipeline._recorders["someone"].running = False

        started = time.monotonic()
        found = _wait_for_session(
            self.pipeline, outcome["request_id"], "someone")

        self.assertIs(found, session)
        self.assertLess(time.monotonic() - started, 0.2)


class ControlStateValidationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="vodpipe-control-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_invalid_control_state_is_quarantined_and_ignored(self):
        config = make_config(self.tmp)
        config.masters_root.mkdir(parents=True)
        control = config.masters_root / ".vodpipe-control.json"
        control.write_text('{"version": 1, "requests": "not-a-list"}',
                           encoding="utf-8")

        from vodpipe.pipeline import Pipeline
        pipeline = Pipeline(config)
        self.addCleanup(lambda: [pool.stop(timeout=10, drain=False)
                                 for pool in pipeline.pools])

        self.assertEqual(pipeline.armed_channels(), [])
        self.assertFalse(control.exists())
        self.assertEqual(len(list(config.masters_root.glob(
            ".vodpipe-control.invalid-*.json"))), 1)

    def test_control_state_never_persists_configured_secrets(self):
        config = make_config(self.tmp)
        config.set("secrets.twitch_oauth_token", "never-write-this-token")
        config.masters_root.mkdir(parents=True)
        from vodpipe.pipeline import OFFLINE, Pipeline
        pipeline = Pipeline(config)
        pipeline._probe_live = lambda channel: (OFFLINE, "")
        self.addCleanup(lambda: [pool.stop(timeout=10, drain=False)
                                 for pool in pipeline.pools])

        pipeline.request_recording("someone")
        payload = (config.masters_root / ".vodpipe-control.json").read_text(
            encoding="utf-8")

        self.assertNotIn("never-write-this-token", payload)


class ProbeClassificationTests(ArmingFixture):
    def probe(self, result=None, error=None):
        from vodpipe.pipeline import Pipeline
        effect = error if error is not None else result
        with mock.patch("vodpipe.pipeline.run", side_effect=[effect]):
            return Pipeline._probe_live(self.pipeline, "someone")

    def test_timeout_is_unknown(self):
        self.assertEqual(self.probe(error=TimeoutError("blocked"))[0], UNKNOWN)

    def test_malformed_json_is_unknown(self):
        result = subprocess.CompletedProcess([], 0, "not json", "")
        self.assertEqual(self.probe(result=result)[0], UNKNOWN)

    def test_tool_failure_without_offline_evidence_is_unknown(self):
        result = subprocess.CompletedProcess([], 2, "{}", "tool failed")
        self.assertEqual(self.probe(result=result)[0], UNKNOWN)

    def test_empty_stream_map_is_confirmed_offline(self):
        result = subprocess.CompletedProcess([], 0, '{"streams": {}}', "")
        self.assertEqual(self.probe(result=result)[0], OFFLINE)

    def test_nonempty_stream_map_is_live(self):
        result = subprocess.CompletedProcess(
            [], 0, '{"streams": {"best": {}}, "metadata": {"title": "x"}}', "")
        self.assertEqual(self.probe(result=result), (LIVE, "x"))

    def test_configured_proxy_reaches_live_status_probe(self):
        from vodpipe.pipeline import Pipeline
        self.pipeline.config.set("network.proxy", "socks5h://127.0.0.1:1080")
        result = subprocess.CompletedProcess([], 0, '{"streams": {}}', "")
        with mock.patch("vodpipe.pipeline.run", return_value=result) as invoke:
            Pipeline._probe_live(self.pipeline, "someone")

        command = invoke.call_args.args[0]
        self.assertIn("--http-proxy", command)
        self.assertEqual(command[command.index("--http-proxy") + 1],
                         "socks5h://127.0.0.1:1080")


class StartupWatchdogTests(unittest.TestCase):
    """The safety net: a recorder that receives nothing must not claim to record."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="vodpipe-watchdog-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.config = make_config(self.tmp)
        self.live_dir = self.tmp / "live"
        self.live_dir.mkdir(parents=True)

        from vodpipe.recorder import Recorder
        from vodpipe.state import SessionStore
        from vodpipe.util import Tools
        self.recorder = Recorder(
            self.config,
            Tools(ffmpeg="ffmpeg", ffprobe="ffprobe", streamlink="streamlink",
                  claude=None),
            SessionStore(self.config.masters_root), "chan")

    def test_it_waits_before_giving_up(self):
        self.config.set("recording.startup_timeout_seconds", 60)
        self.recorder._started_at = time.time()
        self.assertFalse(self.recorder._startup_expired(self.live_dir))

    def test_it_gives_up_once_the_limit_passes_with_no_video(self):
        self.config.set("recording.startup_timeout_seconds", 30)
        self.recorder._started_at = time.time() - 31
        self.assertTrue(self.recorder._startup_expired(self.live_dir))
        self.assertIn("not actually streaming", self.recorder._stop_reason)

    def test_any_video_at_all_disarms_the_watchdog_permanently(self):
        """Once a stream exists, retrying forever is exactly what we want."""
        self.config.set("recording.startup_timeout_seconds", 30)
        self.recorder._started_at = time.time() - 31
        (self.live_dir / "chan_c000.ts").write_bytes(b"video")

        self.assertFalse(self.recorder._startup_expired(self.live_dir))
        # Even much later, and even if the file is removed afterwards.
        (self.live_dir / "chan_c000.ts").unlink()
        self.recorder._started_at = time.time() - 10_000
        self.assertFalse(self.recorder._startup_expired(self.live_dir))

    def test_an_empty_segment_file_does_not_count_as_video(self):
        self.config.set("recording.startup_timeout_seconds", 30)
        self.recorder._started_at = time.time() - 31
        (self.live_dir / "chan_c000.ts").write_bytes(b"")
        self.assertTrue(self.recorder._startup_expired(self.live_dir))

    def test_it_can_be_switched_off(self):
        self.config.set("recording.startup_timeout_seconds", 0)
        self.recorder._started_at = time.time() - 100_000
        self.assertFalse(self.recorder._startup_expired(self.live_dir))


class ExitClassificationTests(unittest.TestCase):
    """Stopping a recording is not a failure.

    Found by recording a real broadcast and stopping it: terminating streamlink
    closes the pipe under ffmpeg mid-packet, so ffmpeg exits AVERROR_INVALIDDATA
    (3199971767 unsigned on Windows). That was folded into the session error, so
    every hand-stopped recording -- and every `--minutes` run -- was marked
    `failed` while holding complete, validated masters.
    """

    # What ffmpeg actually returned in that recording.
    TRUNCATED_INPUT = 3199971767

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="vodpipe-exit-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        from vodpipe.recorder import Recorder
        from vodpipe.state import SessionStore
        from vodpipe.util import Tools
        config = make_config(self.tmp)
        self.recorder = Recorder(
            config,
            Tools(ffmpeg="ffmpeg", ffprobe="ffprobe", streamlink="streamlink",
                  claude=None),
            SessionStore(config.masters_root), "chan")

    def children(self, streamlink_code, ffmpeg_code):
        class Proc:
            def __init__(self, code):
                self.returncode = code

        self.recorder._streamlink = Proc(streamlink_code)
        self.recorder._ffmpeg = Proc(ffmpeg_code)

    def test_a_stopped_recording_is_not_a_failure(self):
        self.recorder._stop.set()
        self.children(1, self.TRUNCATED_INPUT)
        self.assertEqual(self.recorder._classify_exit(), "")

    def test_a_clean_end_is_not_a_failure(self):
        self.children(0, 0)
        self.assertEqual(self.recorder._classify_exit(), "")

    def test_an_unexpected_ffmpeg_death_is_still_reported(self):
        """Nobody asked it to stop, so this one matters."""
        self.children(0, self.TRUNCATED_INPUT)
        self.assertIn("ffmpeg exited", self.recorder._classify_exit())

    def test_an_unexpected_streamlink_death_is_still_reported(self):
        self.children(1, 0)
        self.assertIn("streamlink exited", self.recorder._classify_exit())

    def test_a_stopped_recording_ignores_streamlink_too(self):
        self.recorder._stop.set()
        self.children(130, 0)
        self.assertEqual(self.recorder._classify_exit(), "")


if __name__ == "__main__":
    unittest.main()
