"""Regressions for findings in AUDIT-2026-08-14.md.

Each test names the finding it closes and reproduces the original defect, so a
future change that reintroduces one fails here rather than in a recording.

A note on that audit: its "Verification baseline" section is not reliable on this
machine. It reports `_process_alive(os.getpid())` returning `False` on Windows,
`tests.test_lifecycle.ChannelLockTests.test_live_process_lock_is_respected`
failing, and a suite with one failure. All three were re-checked here on Python
3.14.6 and none reproduce -- the call returns `True`, that test passes, and the
suite was green before any of this work. `ProcessLivenessTests` below pins that
down. The individual findings were therefore each verified against the code
before being fixed, rather than taken on trust; the ones covered here were real.
"""

from __future__ import annotations

import os
import unittest

from vodpipe.channels import InvalidChannel, parse_channel
from vodpipe.cli import SUBCOMMANDS, build_parser


class ChannelAddressTests(unittest.TestCase):
    """AUD2-037: shorthand URLs were matched by substring, not parsed."""

    def test_mixed_case_shorthand_no_longer_crashes(self):
        """`"twitch.tv/" in value.lower()` matched, then split on the literal.

        The split ran against the original mixed-case string, which does not
        contain the lowercase needle, so `[1]` raised IndexError -- an unhandled
        500 from the dashboard rather than a validation message.
        """
        self.assertEqual(parse_channel("Twitch.TV/SomeOne"), "someone")

    def test_every_case_form_reaches_the_same_channel(self):
        for form in ("twitch.tv/SomeOne", "TWITCH.TV/someone",
                     "https://Twitch.tv/SomeOne", "https://www.twitch.tv/SOMEONE",
                     "m.twitch.tv/someone", "@someone", "someone", "SomeOne"):
            self.assertEqual(parse_channel(form), "someone", form)

    def test_lookalike_hosts_are_refused(self):
        """`evil-twitch.tv/someone` contains "twitch.tv/" and was accepted."""
        for spoof in ("evil-twitch.tv/someone", "nottwitch.tv/someone",
                      "twitch.tv.evil.com/someone", "evil.com/twitch.tv/someone",
                      "https://evil-twitch.tv/someone"):
            with self.assertRaises(InvalidChannel, msg=spoof):
                parse_channel(spoof)

    def test_userinfo_cannot_disguise_the_host(self):
        with self.assertRaises(InvalidChannel):
            parse_channel("https://twitch.tv@evil.com/someone")

    def test_query_strings_are_stripped(self):
        self.assertEqual(
            parse_channel("https://twitch.tv/someone?referrer=raid"), "someone")

    def test_extra_path_components_are_refused(self):
        for form in ("twitch.tv/someone/videos", "https://twitch.tv/",
                     "https://twitch.tv"):
            with self.assertRaises(InvalidChannel, msg=form):
                parse_channel(form)

    def test_non_text_is_refused(self):
        for value in (None, 5, True, ["someone"], {"name": "someone"}):
            with self.assertRaises(InvalidChannel):
                parse_channel(value)

    def test_traversal_and_devices_are_still_refused(self):
        for bad in ("../evil", "C:\\evil", "con", "nul", "a"):
            with self.assertRaises(InvalidChannel, msg=bad):
                parse_channel(bad)


class CliDefaultCommandTests(unittest.TestCase):
    """AUD2-040: `vodpipe --config PATH` with no subcommand crashed."""

    def normalise(self, argv):
        # Mirrors main()'s pre-parse step.
        from vodpipe.cli import _has_subcommand
        if not _has_subcommand(argv):
            argv = list(argv) + ["app"]
        return build_parser().parse_args(argv)

    def test_config_flag_alone_still_reaches_the_app(self):
        """PATH is a non-flag argument, which is what defeated the old test."""
        args = self.normalise(["--config", "C:/tmp/x.json"])
        self.assertEqual(args.command, "app")
        # cmd_app() reads both of these; their absence was the crash.
        self.assertTrue(hasattr(args, "port"))
        self.assertTrue(hasattr(args, "no_window"))

    def test_bare_invocation_still_reaches_the_app(self):
        args = self.normalise([])
        self.assertEqual(args.command, "app")
        self.assertTrue(hasattr(args, "port"))

    def test_an_explicit_subcommand_is_not_overridden(self):
        self.assertEqual(self.normalise(["doctor"]).command, "doctor")
        self.assertEqual(
            self.normalise(["--config", "C:/tmp/x.json", "doctor"]).command,
            "doctor")

    def test_subcommand_list_matches_the_parser(self):
        """Keeps SUBCOMMANDS honest if a command is ever added."""
        parser = build_parser()
        actions = [action for action in parser._subparsers._group_actions
                   if hasattr(action, "choices")]
        self.assertEqual(set(actions[0].choices), set(SUBCOMMANDS))


class MasterIsNeverDeletedWithoutAReplacementTests(unittest.TestCase):
    """AUD2-003: a transient probe failure destroyed the only copy of a VOD.

    Recovery unlinked a master the moment validation raised, and only afterwards
    discovered there was no .ts left to rebuild from. `validate_master()` raises
    both for genuine corruption and for an ffprobe timeout, and those are
    indistinguishable at that point -- so a hiccup deleted a finished recording.
    """

    def setUp(self):
        import shutil
        import tempfile
        from pathlib import Path
        from vodpipe.config import DEFAULTS, Config, deep_merge
        from vodpipe.pipeline import Pipeline
        from vodpipe.state import Chunk, Session

        self.tmp = Path(tempfile.mkdtemp(prefix="vodpipe-master-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.pipeline = Pipeline(Config(deep_merge(DEFAULTS, {
            "paths": {"masters_root": str(self.tmp)},
            "watcher": {"enabled": False},
        })))
        directory = self.tmp / "chan" / "s"
        (directory / "master").mkdir(parents=True)
        (directory / "live").mkdir(parents=True)
        self.session = self.pipeline.store.add(Session(
            session_id="s", channel="chan", started_at=0.0,
            directory=str(directory)))
        self.chunk = Chunk(index=0, session_id="s", channel="chan",
                           started_at=0.0, ts_name="c000.ts",
                           master_name="c000.mp4", duration=120.0)
        self.session.chunks.append(self.chunk)
        self.master = directory / "master" / "c000.mp4"
        self.master.write_bytes(b"x" * 4096)

        import vodpipe.pipeline as module
        self.module = module
        self.original = module.validate_master
        module.validate_master = self._fail
        self.addCleanup(setattr, module, "validate_master", self.original)

    def _fail(self, *args, **kwargs):
        raise RuntimeError("ffprobe timed out")

    def test_master_survives_when_there_is_no_ts_to_rebuild_from(self):
        self.pipeline._remux(self.session, self.chunk)
        self.assertTrue(self.master.exists(),
                        "the only copy of this chunk was deleted")
        self.assertIn("could not be rebuilt", self.chunk.master_error)

    def test_a_failed_rebuild_loses_neither_copy(self):
        """With a .ts present the old master stays put while the rebuild is tried.

        remux_to_mp4() stages a `.partial` and only replaces the master once the
        replacement validates, so nothing needs to be unlinked up front -- and
        unlinking up front is exactly what could leave neither file.
        """
        source = self.session.path / "live" / "c000.ts"
        source.write_bytes(b"y" * 4096)

        def failing_remux(*args, **kwargs):
            raise RuntimeError("remux failed")

        original = self.module.remux_to_mp4
        self.module.remux_to_mp4 = failing_remux
        self.addCleanup(setattr, self.module, "remux_to_mp4", original)

        self.pipeline._remux(self.session, self.chunk)
        self.assertTrue(self.master.exists(), "the suspect master was destroyed")
        self.assertTrue(source.exists(), "the .ts must survive a failed remux")


class StopSuppressesAutoRecordTests(unittest.TestCase):
    """AUD2-063: Stop on a live watched channel was undone by the next probe."""

    def setUp(self):
        import shutil
        import tempfile
        from vodpipe.config import DEFAULTS, Config, deep_merge
        from vodpipe.pipeline import LIVE, OFFLINE, Pipeline

        self.tmp = tempfile.mkdtemp(prefix="vodpipe-stop-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        config = Config(deep_merge(DEFAULTS, {
            "paths": {"masters_root": self.tmp},
            "channels": ["chan"],
            "watcher": {"enabled": False},
        }))
        self.pipeline = Pipeline(config)
        self.started = []
        # Stand in for a live probe and a real recorder launch.
        self.pipeline._probe_live = (
            lambda channel, live_state=LIVE, offline_state=OFFLINE:
                (live_state if self.live else offline_state, "a title"))
        self.pipeline.start_recording = (
            lambda channel, request_id="": self._start(channel, request_id))
        self.live = True

    def _start(self, channel, request_id=""):
        from pathlib import Path
        from vodpipe.state import Session
        self.started.append(channel)
        session = Session(session_id="s", channel=channel, started_at=0.0,
                          directory=str(Path(self.tmp) / channel / "s"))

        class Started:
            running = True

        recorder = Started()
        recorder.session = session
        recorder.request_token = request_id
        self.pipeline._recorders[channel] = recorder
        return session

    def force(self):
        self.pipeline._check_channel("chan", force=True)

    def test_auto_record_starts_a_live_watched_channel(self):
        self.force()
        self.assertEqual(self.started, ["chan"])

    def test_stop_prevents_an_immediate_restart(self):
        self.force()
        self.assertEqual(len(self.started), 1)

        # A running recorder, so stop_recording() takes the stop branch.
        class Running:
            running = True
            stopped = False

            def stop(self, reason=""):
                Running.stopped = True

        self.pipeline._recorders["chan"] = Running()
        self.pipeline.stop_recording("chan")
        self.pipeline._recorders.pop("chan")

        self.assertTrue(self.pipeline.is_auto_suppressed("chan"))
        for _ in range(5):
            self.force()
        self.assertEqual(len(self.started), 1, "Stop was undone by the watcher")

    def test_going_offline_restores_auto_record(self):
        self.pipeline._auto_suppressed.add("chan")
        self.live = False
        self.force()
        self.assertFalse(self.pipeline.is_auto_suppressed("chan"))
        self.live = True
        self.force()
        self.assertEqual(self.started, ["chan"])

    def test_an_explicit_record_overrides_the_suppression(self):
        self.pipeline._auto_suppressed.add("chan")
        self.pipeline._live_status["chan"] = {"live": True,
                                              "checked_at": 9e9}
        self.pipeline.request_recording("chan")
        self.assertFalse(self.pipeline.is_auto_suppressed("chan"))
        self.assertEqual(self.started, ["chan"])

    def test_suppression_is_keyed_canonically(self):
        self.pipeline._auto_suppressed.add("chan")
        self.assertTrue(self.pipeline.is_auto_suppressed("https://twitch.tv/CHAN"))


class ChannelMethodsParseTests(unittest.TestCase):
    """AUD2-048: the CLI armed `someone` then looked up the raw URL."""

    def setUp(self):
        import shutil
        import tempfile
        from vodpipe.config import DEFAULTS, Config, deep_merge
        from vodpipe.pipeline import OFFLINE, Pipeline

        self.tmp = tempfile.mkdtemp(prefix="vodpipe-parse-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.pipeline = Pipeline(Config(deep_merge(DEFAULTS, {
            "paths": {"masters_root": self.tmp},
            "watcher": {"enabled": False},
        })))
        self.pipeline._probe_live = (
            lambda channel, offline_state=OFFLINE: (offline_state, ""))

    def test_is_armed_and_disarm_accept_any_form(self):
        self.pipeline.request_recording("someone")
        self.assertTrue(self.pipeline.is_armed("https://twitch.tv/SomeOne"))
        self.assertTrue(self.pipeline.is_armed("SOMEONE"))
        self.assertTrue(self.pipeline.disarm("twitch.tv/SomeOne"))
        self.assertFalse(self.pipeline.is_armed("someone"))

    def test_stop_recording_accepts_any_form(self):
        self.pipeline.request_recording("someone")
        self.pipeline.stop_recording("https://twitch.tv/SomeOne")
        self.assertFalse(self.pipeline.is_armed("someone"))


class DeepgramEnvelopeTests(unittest.TestCase):
    """AUD2-007: a malformed HTTP 200 was indistinguishable from silence.

    The caller trusts an empty word list: it advances its coverage cursor past
    the audio and can retire the previous exports. So an error-shaped 200 or a
    changed schema published real speech as having contained none.
    """

    def parse(self, payload):
        from vodpipe.asr import parse_deepgram
        return parse_deepgram(payload)

    def good(self, words):
        return {"results": {"channels": [{"alternatives": [{"words": words}]}]}}

    def test_explicit_empty_words_is_still_valid_silence(self):
        self.assertEqual(self.parse(self.good([])), [])

    def test_a_normal_response_still_parses(self):
        words = self.parse(self.good([
            {"word": "hello", "punctuated_word": "Hello,", "start": 1.0,
             "end": 1.4, "confidence": 0.98},
        ]))
        self.assertEqual(len(words), 1)
        self.assertEqual(words[0].text, "Hello,")
        self.assertAlmostEqual(words[0].start, 1.0)
        self.assertAlmostEqual(words[0].duration, 0.4)

    def test_missing_envelope_levels_are_errors_not_silence(self):
        from vodpipe.asr import TranscriptionError
        for payload in (
            {},
            {"results": {}},
            {"results": {"channels": []}},
            {"results": {"channels": [{}]}},
            {"results": {"channels": [{"alternatives": []}]}},
        ):
            with self.assertRaises(TranscriptionError, msg=repr(payload)):
                self.parse(payload)

    def test_transcript_without_word_timings_is_not_silence(self):
        from vodpipe.asr import TranscriptionError
        with self.assertRaises(TranscriptionError):
            self.parse({"results": {"channels": [
                {"alternatives": [{"transcript": "plenty of speech here"}]}]}})

    def test_error_shaped_success_bodies_are_rejected(self):
        from vodpipe.asr import TranscriptionError
        for payload in ({"err_code": "INVALID_AUTH", "err_msg": "nope"},
                        {"error": "something went wrong"},
                        {"error": {"code": "bad_request"}},
                        {"error": 17},
                        {"message": 0}):
            with self.assertRaises(TranscriptionError, msg=repr(payload)):
                self.parse(payload)

    def test_null_and_empty_error_fields_do_not_hide_a_valid_response(self):
        payload = self.good([])
        payload.update(error=None, err_msg="", message={})
        self.assertEqual(self.parse(payload), [])

    def test_nonsensical_timings_are_rejected(self):
        from vodpipe.asr import TranscriptionError
        for entry in ({"word": "a", "start": -1.0, "end": 2.0},
                      {"word": "a", "start": 5.0, "end": 2.0},
                      {"word": "a", "start": float("inf"), "end": 2.0},
                      {"word": "a", "start": "abc", "end": 2.0}):
            with self.assertRaises(TranscriptionError, msg=repr(entry)):
                self.parse(self.good([entry]))

    def test_blank_words_are_rejected_not_silently_dropped(self):
        from vodpipe.asr import TranscriptionError
        with self.assertRaises(TranscriptionError):
            self.parse(self.good([
                {"word": "   ", "start": 0.0, "end": 0.1,
                 "confidence": 0.9},
            ]))


class CorruptWordsFileTests(unittest.TestCase):
    """AUD2-008: a corrupt words.json read as "nothing transcribed yet"."""

    def setUp(self):
        import shutil
        import tempfile
        from pathlib import Path
        self.tmp = Path(tempfile.mkdtemp(prefix="vodpipe-words-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.path = self.tmp / "words.json"

    def test_a_missing_file_is_still_an_empty_start(self):
        from vodpipe.transcript import load_words
        self.assertEqual(load_words(self.path), ([], {}))

    def test_truncated_json_raises_instead_of_resetting(self):
        from vodpipe.transcript import CorruptWordsFile, load_words
        self.path.write_text('{"words": [{"text": "hel', encoding="utf-8")
        with self.assertRaises(CorruptWordsFile):
            load_words(self.path)

    def test_a_non_object_payload_raises(self):
        from vodpipe.transcript import CorruptWordsFile, load_words
        self.path.write_text("[1, 2, 3]", encoding="utf-8")
        with self.assertRaises(CorruptWordsFile):
            load_words(self.path)

    def test_a_valid_file_round_trips(self):
        from vodpipe.transcript import Word, load_words, save_words
        save_words(self.path, [Word("hi", 0.0, 0.3, 0.9)],
                   {"covered_seconds": 12.0, "expected_seconds": 12.0,
                    "complete": True})
        words, meta = load_words(self.path)
        self.assertEqual([word.text for word in words], ["hi"])
        self.assertEqual(meta["covered_seconds"], 12.0)


class ConfigStringTests(unittest.TestCase):
    """AUD2-055 / AUD2-038: strings that cannot survive being stored."""

    def validator(self):
        from vodpipe.schema import SCHEMA
        return SCHEMA["recording.quality"]

    def test_lone_surrogates_are_refused(self):
        from vodpipe.config import ConfigError
        check = self.validator()
        with self.assertRaises(ConfigError):
            check("\ud800", "recording.quality")

    def test_control_characters_are_refused(self):
        from vodpipe.config import ConfigError
        check = self.validator()
        for bad in ("\x00", "a\x01b", "tab\x7f"):
            with self.assertRaises(ConfigError, msg=repr(bad)):
                check(bad, "recording.quality")

    def test_ordinary_text_still_passes(self):
        check = self.validator()
        self.assertEqual(check("1080p60,best", "recording.quality"), "1080p60,best")
        # Non-ASCII is fine; it is only surrogates and controls that are not.
        self.assertEqual(check("최고", "recording.quality"), "최고")

    def test_a_rejected_value_changes_neither_memory_nor_disk(self):
        import json
        import shutil
        import tempfile
        from pathlib import Path
        from vodpipe.config import DEFAULTS, Config, ConfigError, deep_merge

        tmp = Path(tempfile.mkdtemp(prefix="vodpipe-cfg-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        path = tmp / "config.json"
        config = Config(deep_merge(DEFAULTS, {}), path)
        config.save()
        before = path.read_text(encoding="utf-8")

        with self.assertRaises(ConfigError):
            config.apply_and_save({"recording": {"quality": "\ud800"}})

        self.assertEqual(config.get("recording.quality"), "best")
        self.assertEqual(path.read_text(encoding="utf-8"), before)
        self.assertFalse((tmp / "config.json.tmp").exists(),
                         "a temp file holding secrets was left behind")
        # And the config still serialises, which it did not once poisoned.
        json.dumps(config.redacted(), ensure_ascii=False).encode("utf-8")

    def test_load_returns_the_normalised_config(self):
        """AUD2-054: load() validated a cleaned copy but kept the raw one."""
        import json
        import shutil
        import tempfile
        from pathlib import Path
        from vodpipe.config import Config

        tmp = Path(tempfile.mkdtemp(prefix="vodpipe-cfg2-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        path = tmp / "config.json"
        path.write_text(json.dumps({
            "channels": ["https://twitch.tv/SomeOne"],
            "transcription": {"language": "EN_us"},
        }), encoding="utf-8")

        config = Config.load(path)
        self.assertEqual(config.get("channels"), ["someone"])
        self.assertEqual(config.get("transcription.language"), "en-us")


class ChunkIdentityTests(unittest.TestCase):
    """AUD2-045: two Chunk(index=0) records could exist at once."""

    def test_adding_a_duplicate_index_returns_the_original(self):
        import shutil
        import tempfile
        from pathlib import Path
        from vodpipe.state import Chunk, Session, SessionStore

        tmp = Path(tempfile.mkdtemp(prefix="vodpipe-chunk-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        store = SessionStore(tmp)
        session = store.add(Session(session_id="s", channel="chan",
                                    started_at=0.0,
                                    directory=str(tmp / "chan" / "s")))
        first = store.add_chunk(session, Chunk(index=0, session_id="s",
                                               channel="chan", started_at=0.0))
        second = store.add_chunk(session, Chunk(index=0, session_id="s",
                                                channel="chan", started_at=1.0))
        self.assertIs(first, second)
        self.assertEqual([chunk.index for chunk in session.chunks], [0])

    def test_distinct_indexes_are_still_added(self):
        import shutil
        import tempfile
        from pathlib import Path
        from vodpipe.state import Chunk, Session, SessionStore

        tmp = Path(tempfile.mkdtemp(prefix="vodpipe-chunk2-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        store = SessionStore(tmp)
        session = store.add(Session(session_id="s", channel="chan",
                                    started_at=0.0,
                                    directory=str(tmp / "chan" / "s")))
        for index in range(3):
            store.add_chunk(session, Chunk(index=index, session_id="s",
                                           channel="chan", started_at=0.0))
        self.assertEqual([chunk.index for chunk in session.chunks], [0, 1, 2])


class AsrStreamSelectionTests(unittest.TestCase):
    """AUD2-050: ffmpeg's automatic pick could transcribe the wrong track."""

    def stream(self, index, kind, **extra):
        return {"index": index, "codec_type": kind, **extra}

    def test_the_default_disposition_wins(self):
        from vodpipe.media import choose_asr_stream
        streams = [
            self.stream(0, "video"),
            self.stream(1, "audio", disposition={"default": 1}, channels=2),
            # ffmpeg would prefer this one purely for having more channels.
            self.stream(2, "audio", disposition={"default": 0}, channels=6),
        ]
        self.assertEqual(choose_asr_stream(streams), 1)

    def test_without_a_default_the_first_audio_stream_wins(self):
        from vodpipe.media import choose_asr_stream
        streams = [self.stream(0, "video"),
                   self.stream(1, "audio", channels=2),
                   self.stream(2, "audio", channels=6)]
        self.assertEqual(choose_asr_stream(streams), 1)

    def test_no_audio_yields_none(self):
        from vodpipe.media import choose_asr_stream
        self.assertIsNone(choose_asr_stream([self.stream(0, "video")]))


class CliArgumentTests(unittest.TestCase):
    """AUD2-056: durations and offsets accepted values that broke the loops."""

    def parse(self, argv):
        return build_parser().parse_args(argv)

    def test_minutes_rejects_zero_negative_and_nonfinite(self):
        for bad in ("0", "-5", "nan", "inf"):
            with self.assertRaises(SystemExit, msg=bad):
                self.parse(["record", "chan", "--minutes", bad])

    def test_minutes_accepts_a_real_duration(self):
        self.assertEqual(self.parse(["record", "chan", "--minutes", "90"]).minutes,
                         90.0)

    def test_snapshot_offsets_are_validated(self):
        for flag, bad in (("--last", "0"), ("--last", "nan"),
                          ("--start", "-1"), ("--start", "inf"),
                          ("--end", "0")):
            with self.assertRaises(SystemExit, msg=f"{flag} {bad}"):
                self.parse(["snapshot", "sid", flag, bad])

    def test_a_zero_start_is_still_a_valid_offset(self):
        self.assertEqual(self.parse(["snapshot", "sid", "--start", "0"]).start, 0.0)


class ManifestContainmentTests(unittest.TestCase):
    """AUD2-064: artifact names off disk are joined onto the session directory.

    Recovery *deletes* what they point at, so a name that escapes the session is
    a way to make this application remove an arbitrary file. An absolute name
    replaces the prefix entirely on both platforms; `..` walks out.
    """

    def restore(self, **names):
        from vodpipe.state import Session
        return Session.from_dict({
            "session_id": "s", "channel": "chan", "directory": "d",
            "chunks": [{"index": 0, "session_id": "s", "channel": "chan",
                        "started_at": 0.0, **names}],
        }).chunks[0]

    def test_absolute_names_are_dropped(self):
        chunk = self.restore(master_name=r"C:/Users/Daniel/Desktop/important.mp4")
        self.assertEqual(chunk.master_name, "")

    def test_traversal_is_dropped(self):
        chunk = self.restore(ts_name="../../evil.ts")
        self.assertEqual(chunk.ts_name, "")

    def test_separators_of_either_kind_are_dropped(self):
        self.assertEqual(self.restore(proxy_name="sub/dir.mp4").proxy_name, "")
        self.assertEqual(self.restore(proxy_name=r"sub\dir.mp4").proxy_name, "")

    def test_ordinary_names_survive(self):
        chunk = self.restore(ts_name="chan_s_c000.ts",
                             master_name="chan_s_c000.mp4",
                             proxy_name="chan_s_c000_Proxy.mp4")
        self.assertEqual(chunk.ts_name, "chan_s_c000.ts")
        self.assertEqual(chunk.master_name, "chan_s_c000.mp4")
        self.assertEqual(chunk.proxy_name, "chan_s_c000_Proxy.mp4")

    def test_recovery_cannot_delete_outside_the_session(self):
        """End to end: a poisoned manifest plus a failing probe."""
        import json
        import shutil
        import tempfile
        from pathlib import Path
        from vodpipe.config import DEFAULTS, Config, deep_merge
        from vodpipe.pipeline import Pipeline

        tmp = Path(tempfile.mkdtemp(prefix="vodpipe-escape-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        outsider = tmp / "important.mp4"
        outsider.write_bytes(b"x" * 4096)

        masters = tmp / "masters"
        session_dir = masters / "chan" / "s"
        (session_dir / "master").mkdir(parents=True)
        (session_dir / "live").mkdir(parents=True)
        (session_dir / "session.json").write_text(json.dumps({
            "session_id": "s", "channel": "chan", "started_at": 0.0,
            "directory": str(session_dir), "status": "interrupted",
            "chunks": [{"index": 0, "session_id": "s", "channel": "chan",
                        "started_at": 0.0, "status": "complete",
                        "master_name": str(outsider), "duration": 10.0}],
        }), encoding="utf-8")

        config = Config(deep_merge(DEFAULTS, {
            "paths": {"masters_root": str(masters)},
            "watcher": {"enabled": False},
        }))
        pipeline = Pipeline(config)
        try:
            pipeline.recover()
        finally:
            pipeline.shutdown()
        self.assertTrue(outsider.exists(),
                        "recovery deleted a file outside the session")


class RetranscribeRollbackTests(unittest.TestCase):
    """AUD2-051: rollback restored the words but kept the failure's error."""

    def test_artifact_state_is_restored_completely(self):
        import shutil
        import tempfile
        from pathlib import Path
        from vodpipe.config import DEFAULTS, Config, deep_merge
        from vodpipe.pipeline import Pipeline
        from vodpipe.state import Chunk, Session

        tmp = Path(tempfile.mkdtemp(prefix="vodpipe-roll-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        config = Config(deep_merge(DEFAULTS, {
            "paths": {"masters_root": str(tmp)},
            "watcher": {"enabled": False},
            "secrets": {"deepgram_api_key": "k"},
        }))
        pipeline = Pipeline(config)
        self.addCleanup(pipeline.shutdown)

        directory = tmp / "chan" / "s"
        (directory / "master").mkdir(parents=True)
        session = pipeline.store.add(Session(
            session_id="s", channel="chan", started_at=0.0,
            directory=str(directory)))
        master = directory / "master" / "c000.mp4"
        master.write_bytes(b"x" * 4096)
        chunk = Chunk(index=0, session_id="s", channel="chan", started_at=0.0,
                      master_name="c000.mp4", duration=10.0, status="complete",
                      transcript_status="done", transcript_error="",
                      transcribed_through=10.0, word_count=42)
        session.chunks.append(chunk)

        def boom(*args, **kwargs):
            raise RuntimeError("provider is down")

        pipeline.transcriber.finalize = boom
        pipeline.transcriber.stash_words = lambda *a, **k: None
        pipeline.transcriber.restore_words = lambda *a, **k: None

        job = pipeline.retranscribe(session, chunk)
        pipeline.jobs.stop(timeout=30)

        self.assertEqual(chunk.transcript_status, "done")
        self.assertEqual(chunk.transcript_error, "",
                         "the failed attempt's error outlived the rollback")
        self.assertEqual(chunk.word_count, 42)
        self.assertEqual(chunk.transcribed_through, 10.0)
        self.assertEqual(pipeline.jobs.get(job.key).status, "failed")


class ExportGenerationTests(unittest.TestCase):
    """AUD2-009: an export set must describe one generation, not several."""

    def setUp(self):
        import shutil
        import tempfile
        from pathlib import Path
        self.tmp = Path(tempfile.mkdtemp(prefix="vodpipe-exp-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def words(self, *texts):
        from vodpipe.transcript import Word
        return [Word(text, index * 1.0, 0.5, 0.9)
                for index, text in enumerate(texts)]

    def test_a_publish_records_its_generation(self):
        from vodpipe.exports import MANIFEST_NAME, read_manifest, write_exports
        written = write_exports(self.tmp, self.words("hello", "world"))
        manifest = read_manifest(self.tmp)
        self.assertTrue((self.tmp / "source" / MANIFEST_NAME).exists())
        self.assertEqual(sorted(manifest["files"]), sorted(written))
        self.assertEqual(manifest["word_count"], 2)
        self.assertTrue(manifest["generation"])

    def test_the_generation_follows_the_words(self):
        from vodpipe.exports import read_manifest, write_exports
        write_exports(self.tmp, self.words("hello", "world"))
        first = read_manifest(self.tmp)["generation"]
        write_exports(self.tmp, self.words("hello", "world"))
        self.assertEqual(read_manifest(self.tmp)["generation"], first,
                         "the same words must yield the same generation")
        write_exports(self.tmp, self.words("something", "else"))
        self.assertNotEqual(read_manifest(self.tmp)["generation"], first)

    def test_transcript_json_carries_the_generation(self):
        import json
        from vodpipe.exports import read_manifest, write_exports
        write_exports(self.tmp, self.words("hello"))
        payload = json.loads(
            (self.tmp / "source" / "transcript.json").read_text(encoding="utf-8"))
        self.assertEqual(payload["generation"], read_manifest(self.tmp)["generation"])

    def test_a_render_failure_leaves_the_previous_generation_intact(self):
        """Nothing is replaced until every file has been rendered."""
        from vodpipe.exports import read_manifest, write_exports
        from vodpipe.transcript import CensorList

        write_exports(self.tmp, self.words("first", "generation"))
        before = {name: (self.tmp / name).read_text(encoding="utf-8")
                  for name in ("premiere.json", "transcript.srt",
                               "source/transcript.txt")}
        original = read_manifest(self.tmp)

        class Exploding(CensorList):
            def present_in(self, words):
                raise RuntimeError("disk full")

        exploding = Exploding([])
        exploding.exact = {"x"}          # make it truthy so it is consulted
        with self.assertRaises(RuntimeError):
            write_exports(self.tmp, self.words("second", "generation"),
                          censor=exploding)

        for name, text in before.items():
            self.assertEqual((self.tmp / name).read_text(encoding="utf-8"), text,
                             f"{name} was replaced by a publish that then failed")
        self.assertEqual(read_manifest(self.tmp), original)

    def test_an_empty_rebuild_still_retires_the_old_set(self):
        from vodpipe.exports import write_exports
        write_exports(self.tmp, self.words("hello", "world"))
        self.assertTrue((self.tmp / "premiere.json").exists())
        write_exports(self.tmp, [])
        self.assertFalse((self.tmp / "premiere.json").exists(),
                         "Premiere would go on importing a stale transcript")
        self.assertTrue((self.tmp / "source" / "transcript.txt").exists())


class ProcessLivenessTests(unittest.TestCase):
    """AUD2-001's stated premise, checked directly.

    The audit calls this "the most important result" and says the live process
    is misclassified as dead on this Windows runtime. It is not. Kernel-backed
    locking is still the stronger design, but the reproduction the audit reports
    does not hold here, so the finding's severity rests on the unlink races it
    also describes rather than on this claim.
    """

    def test_the_current_process_is_seen_as_alive(self):
        from vodpipe.locks import _process_alive
        self.assertTrue(_process_alive(os.getpid()))

    def test_an_absent_pid_is_seen_as_dead(self):
        from vodpipe.locks import _process_alive
        self.assertFalse(_process_alive(999999998))


if __name__ == "__main__":
    unittest.main()
