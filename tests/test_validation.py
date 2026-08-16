"""Input, path and config validation (AUD-009, AUD-010, AUD-038).

Channel names become directory names, filename prefixes and deletion globs, so
this is the containment boundary for the whole application.
"""

from __future__ import annotations

import copy
import json
import os
import shutil
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from vodpipe.channels import InvalidChannel, parse_channel
from vodpipe.config import CLEAR, DEFAULTS, MASK, Config, deep_merge
from vodpipe.schema import ConfigError, validate
from vodpipe.util import ensure_within, safe_name_component


class ChannelParsingTests(unittest.TestCase):
    def test_plain_names(self):
        self.assertEqual(parse_channel("SomeStreamer"), "somestreamer")
        self.assertEqual(parse_channel("  user_123 "), "user_123")
        self.assertEqual(parse_channel("@Handle99"), "handle99")

    def test_urls(self):
        for text in ("https://twitch.tv/SomeOne",
                     "https://www.twitch.tv/SomeOne",
                     "http://m.twitch.tv/SomeOne",
                     "twitch.tv/SomeOne",
                     "https://twitch.tv/SomeOne?tt_content=x"):
            self.assertEqual(parse_channel(text), "someone", text)

    def test_traversal_is_rejected(self):
        for text in ("../evil", "..\\evil", "a/../../b", "./x"):
            with self.assertRaises(InvalidChannel, msg=text):
                parse_channel(text)

    def test_absolute_and_unc_paths_are_rejected(self):
        for text in ("C:\\Windows", "/etc/passwd", "\\\\server\\share",
                     "//server/share", "D:/data"):
            with self.assertRaises(InvalidChannel, msg=text):
                parse_channel(text)

    def test_separators_and_punctuation_are_rejected(self):
        for text in ("a/b", "a\\b", "a:b", "a*b", "a?b", 'a"b', "a<b", "a|b",
                     "a,b", "a b", "a.b", "a'b"):
            with self.assertRaises(InvalidChannel, msg=text):
                parse_channel(text)

    def test_control_characters_are_rejected(self):
        for text in ("a\nb", "a\rb", "a\x00b", "a\tb"):
            with self.assertRaises(InvalidChannel, msg=repr(text)):
                parse_channel(text)

    def test_windows_reserved_names_are_rejected(self):
        for text in ("con", "PRN", "aux", "nul", "com1", "LPT9"):
            with self.assertRaises(InvalidChannel, msg=text):
                parse_channel(text)

    def test_length_bounds(self):
        with self.assertRaises(InvalidChannel):
            parse_channel("ab")                      # too short
        with self.assertRaises(InvalidChannel):
            parse_channel("x" * 26)                  # too long
        with self.assertRaises(InvalidChannel):
            parse_channel("x" * 5000)
        self.assertEqual(parse_channel("abc"), "abc")

    def test_empty_is_rejected(self):
        for text in ("", "   ", "@"):
            with self.assertRaises(InvalidChannel, msg=repr(text)):
                parse_channel(text)

    def test_non_twitch_urls_are_rejected(self):
        for text in ("https://evil.com/someone", "https://twitch.tv.evil.com/x"):
            with self.assertRaises(InvalidChannel, msg=text):
                parse_channel(text)

    def test_multi_component_urls_are_rejected(self):
        with self.assertRaises(InvalidChannel):
            parse_channel("https://twitch.tv/someone/videos/12345")


class PathContainmentTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="vodpipe-contain-"))

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_child_is_allowed(self):
        target = self.root / "a" / "b.txt"
        target.parent.mkdir(parents=True)
        target.write_text("x", encoding="utf-8")
        self.assertEqual(ensure_within(self.root, target), target.resolve())

    def test_root_itself_is_allowed(self):
        ensure_within(self.root, self.root)

    def test_escape_is_refused_after_resolution(self):
        with self.assertRaises(ValueError):
            ensure_within(self.root, self.root / ".." / "elsewhere")

    def test_unrelated_absolute_path_is_refused(self):
        with self.assertRaises(ValueError):
            ensure_within(self.root, Path("C:/Windows/win.ini"))

    def test_safe_component_rejects_separators_and_reserved(self):
        for bad in ("../x", "a/b", "a\\b", "", "   ", ".", "..", "con", "a:b",
                    "x" * 100, "a\0b"):
            with self.assertRaises(ValueError, msg=repr(bad)):
                safe_name_component(bad)

    def test_safe_component_trims_surrounding_whitespace(self):
        # Windows discards trailing dots and spaces itself, so normalising here
        # keeps the name we use identical to the one on disk.
        self.assertEqual(safe_name_component("  Proxies  "), "Proxies")
        self.assertEqual(safe_name_component("Proxies"), "Proxies")


class ConfigSchemaTests(unittest.TestCase):
    def test_defaults_are_valid(self):
        validate(DEFAULTS)

    def config(self) -> Config:
        return Config(deep_merge(DEFAULTS, {}), Path("unused.json"))

    def assert_rejected(self, overlay, needle=""):
        config = self.config()
        before = config.data["paths"]["masters_root"]
        with self.assertRaises(ConfigError, msg=str(overlay)) as caught:
            config.apply(overlay)
        if needle:
            self.assertIn(needle, str(caught.exception))
        # Transactional: a rejected update changes nothing.
        self.assertEqual(config.data["paths"]["masters_root"], before)

    def test_zero_and_negative_durations(self):
        self.assert_rejected({"transcription": {"slice_seconds": 0}})
        self.assert_rejected({"recording": {"chunk_seconds": -1}})

    def test_wrong_types(self):
        self.assert_rejected({"paths": {"masters_root": None}})
        self.assert_rejected({"paths": {"masters_root": 5}})
        self.assert_rejected({"proxies": {"enabled": "yes"}})
        self.assert_rejected({"channels": "notalist"})
        self.assert_rejected({"recording": {"chunk_seconds": True}})

    def test_non_finite_numbers(self):
        self.assert_rejected({"proxies": {"retention_days": float("nan")}})
        self.assert_rejected({"proxies": {"retention_days": float("inf")}})

    def test_unknown_keys_are_refused(self):
        self.assert_rejected({"nonsense": 1}, "not a known setting")
        self.assert_rejected({"recording": {"nonsense": 1}}, "not a known setting")

    def test_invalid_enums(self):
        self.assert_rejected({"summary": {"provider": "gpt"}})
        self.assert_rejected({"proxies": {"encoder": "magic"}})
        self.assert_rejected({"transcription": {"provider": "whisper"}})

    def test_port_bounds(self):
        self.assert_rejected({"dashboard": {"port": 0}})
        self.assert_rejected({"dashboard": {"port": 70000}})

    def test_language_must_be_a_language_tag(self):
        """A typo here reaches Premiere as a header nothing can read (AUD-035)."""
        for bad in ("english", "e", "en-", "en gb", "", 5):
            self.assert_rejected({"transcription": {"language": bad}})

    def test_language_is_normalised_on_the_way_in(self):
        config = self.config()
        config.apply({"transcription": {"language": " EN_GB "}})
        self.assertEqual(config.get("transcription.language"), "en-gb")

    def test_seam_bounds(self):
        self.assert_rejected({"transcription": {"seam_seconds": 0}})
        self.assert_rejected({"transcription": {"seam_seconds": 1000}})
        self.assert_rejected({"transcription": {"stitch_chunk_boundaries": "yes"}})

    def test_snapshot_caps_are_bounded(self):
        self.assert_rejected({"snapshots": {"max_concurrent": 0}})
        self.assert_rejected({"snapshots": {"max_concurrent": 1.5}})
        self.assert_rejected({"snapshots": {"max_per_session": 99}})

    def test_a_per_session_cap_cannot_exceed_the_global_one(self):
        self.assert_rejected(
            {"snapshots": {"max_concurrent": 2, "max_per_session": 3}},
            "cannot exceed")

    def test_probe_timeout_is_bounded(self):
        self.assert_rejected({"watcher": {"probe_timeout_seconds": 0}})
        self.assert_rejected({"watcher": {"probe_timeout_seconds": 10_000}})

    def test_overlap_must_be_smaller_than_a_slice(self):
        self.assert_rejected(
            {"transcription": {"slice_seconds": 10, "overlap_seconds": 10}},
            "smaller than slice_seconds")

    def test_reserve_must_not_exceed_the_floor(self):
        self.assert_rejected(
            {"recording": {"free_space_floor_gb": 5, "hard_reserve_gb": 10}},
            "must not exceed")

    def test_proxy_height_must_be_even(self):
        self.assert_rejected({"proxies": {"height": 541}}, "even")

    def test_non_loopback_binding_is_refused(self):
        """No authentication, so a reachable bind would expose everything."""
        for host in ("0.0.0.0", "192.168.1.10", "example.com"):
            self.assert_rejected({"dashboard": {"host": host}}, "loopback")

    def test_unsafe_names_in_path_components(self):
        self.assert_rejected({"proxies": {"folder_name": "../escape"}})
        self.assert_rejected({"proxies": {"folder_name": "a/b"}})
        self.assert_rejected({"proxies": {"suffix": "a/b"}})

    def test_invalid_channel_in_the_list(self):
        self.assert_rejected({"channels": ["../evil"]})
        self.assert_rejected({"channels": ["C:\\x"]})

    def test_channel_settings_are_validated(self):
        self.assert_rejected({"channel_settings": {"../x": {"auto_record": True}}})
        self.assert_rejected({"channel_settings": {"good": {"unknown": 1}}})

    def test_valid_updates_are_applied(self):
        config = self.config()
        config.apply({"proxies": {"height": 480}, "summary": {"provider": "none"}})
        self.assertEqual(config.get("proxies.height"), 480)
        self.assertEqual(config.get("summary.provider"), "none")

    def test_channels_are_normalised_on_the_way_in(self):
        config = self.config()
        config.apply({"channels": ["SomeOne", "https://twitch.tv/Another"]})
        self.assertEqual(config.get("channels"), ["another", "someone"])

    def test_numeric_and_boolean_channels_are_not_coerced(self):
        for value in (1234, True, ["channel"], {"name": "channel"}):
            self.assert_rejected({"channels": [value]})


class SecretLifecycleTests(unittest.TestCase):
    def config(self) -> Config:
        return Config(deep_merge(DEFAULTS, {}), Path("unused.json"))

    def test_set_then_read(self):
        config = self.config()
        config.apply({"secrets": {"deepgram_api_key": "abc123"}})
        self.assertEqual(config.secret("deepgram_api_key"), "abc123")

    def test_mask_keeps_the_stored_value(self):
        config = self.config()
        config.apply({"secrets": {"deepgram_api_key": "abc123"}})
        config.apply({"secrets": {"deepgram_api_key": MASK}})
        self.assertEqual(config.secret("deepgram_api_key"), "abc123")

    def test_blank_keeps_the_stored_value(self):
        """The UI sends empty for untouched password fields."""
        config = self.config()
        config.apply({"secrets": {"deepgram_api_key": "abc123"}})
        config.apply({"secrets": {"deepgram_api_key": ""}})
        self.assertEqual(config.secret("deepgram_api_key"), "abc123")

    def test_clear_erases_it(self):
        config = self.config()
        config.apply({"secrets": {"deepgram_api_key": "abc123"}})
        config.apply({"secrets": {"deepgram_api_key": CLEAR}})
        self.assertEqual(config.secret("deepgram_api_key"), "")

    def test_replace_overwrites(self):
        config = self.config()
        config.apply({"secrets": {"deepgram_api_key": "one"}})
        config.apply({"secrets": {"deepgram_api_key": "two"}})
        self.assertEqual(config.secret("deepgram_api_key"), "two")

    def test_redacted_never_leaks_a_value(self):
        config = self.config()
        config.apply({"secrets": {"deepgram_api_key": "supersecret"}})
        self.assertNotIn("supersecret", json.dumps(config.redacted()))


class CorruptConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="vodpipe-corrupt-"))
        self.path = self.tmp / "config.json"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_missing_file_uses_defaults(self):
        config = Config.load(self.path)
        self.assertEqual(config.get("dashboard.port"), 8420)

    def test_malformed_json_fails_loudly(self):
        """Silently using defaults would overwrite real settings on next save."""
        self.path.write_text("{not json", encoding="utf-8")
        with self.assertRaises(ConfigError):
            Config.load(self.path)
        # The original file must survive for the user to fix.
        self.assertTrue(self.path.exists())

    def test_non_object_json_fails_loudly(self):
        for text in ("[]", '"a string"', "42", "null"):
            self.path.write_text(text, encoding="utf-8")
            with self.assertRaises(ConfigError, msg=text):
                Config.load(self.path)

    def test_invalid_stored_values_fail_loudly(self):
        self.path.write_text('{"dashboard": {"port": 0}}', encoding="utf-8")
        with self.assertRaises(ConfigError):
            Config.load(self.path)

    def test_valid_file_loads(self):
        self.path.write_text('{"proxies": {"height": 360}}', encoding="utf-8")
        self.assertEqual(Config.load(self.path).get("proxies.height"), 360)


class ConfigPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="vodpipe-config-save-"))
        self.path = self.tmp / "config.json"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_two_instances_saving_different_keys_preserve_both(self):
        first = Config.load(self.path)
        second = Config.load(self.path)

        first.set("proxies.height", 480)
        first.save()
        second.set("summary.provider", "none")
        second.save()

        reloaded = Config.load(self.path)
        self.assertEqual(reloaded.get("proxies.height"), 480)
        self.assertEqual(reloaded.get("summary.provider"), "none")

    def test_stale_masked_secret_save_keeps_latest_secret(self):
        first = Config.load(self.path)
        second = Config.load(self.path)

        first.apply_and_save({"secrets": {"deepgram_api_key": "latest-key"}})
        second.apply_and_save({
            "summary": {"provider": "none"},
            "secrets": {"deepgram_api_key": MASK},
        })

        self.assertEqual(Config.load(self.path).secret("deepgram_api_key"),
                         "latest-key")

    def test_mutate_and_save_reads_latest_for_channel_updates(self):
        first = Config.load(self.path)
        second = Config.load(self.path)

        first.mutate_and_save(lambda data: data["channels"].append("FirstOne"))
        result = second.mutate_and_save(
            lambda data: data["channels"].append("SecondOne") or "added")

        self.assertEqual(result, "added")
        self.assertEqual(Config.load(self.path).get("channels"),
                         ["firstone", "secondone"])

    def test_mutate_and_save_rolls_back_validation_failure(self):
        config = Config.load(self.path)
        config.apply_and_save({"channels": ["existing"]})
        before_memory = copy.deepcopy(config.data)
        before_disk = self.path.read_bytes()

        with self.assertRaises(ConfigError):
            config.mutate_and_save(
                lambda data: data["channels"].append("../invalid"))

        self.assertEqual(config.data, before_memory)
        self.assertEqual(self.path.read_bytes(), before_disk)

    def test_mutate_and_save_rolls_back_and_cleans_temp_on_write_failure(self):
        config = Config.load(self.path)
        config.apply_and_save({"channels": ["existing"]})
        before_memory = copy.deepcopy(config.data)
        before_disk = self.path.read_bytes()

        with mock.patch("vodpipe.util.os.replace",
                        side_effect=OSError("injected replace failure")):
            with self.assertRaises(OSError):
                config.mutate_and_save(
                    lambda data: data["channels"].append("newchannel"))

        self.assertEqual(config.data, before_memory)
        self.assertEqual(self.path.read_bytes(), before_disk)
        self.assertEqual(list(self.tmp.glob(".*.tmp")), [])

    def test_runtime_roots_are_staged_until_restart(self):
        startup_masters = self.tmp / "masters-startup"
        startup_work = self.tmp / "work-startup"
        staged_masters = self.tmp / "masters-staged"
        staged_work = self.tmp / "work-staged"
        config = Config(deep_merge(DEFAULTS, {
            "paths": {
                "masters_root": str(startup_masters),
                "work_root": str(startup_work),
            },
        }), self.path)

        config.apply_and_save({"paths": {
            "masters_root": str(staged_masters),
            "work_root": str(staged_work),
        }})

        self.assertEqual(config.get("paths.masters_root"), str(staged_masters))
        self.assertEqual(config.redacted()["paths"]["work_root"], str(staged_work))
        self.assertEqual(config.masters_root, startup_masters.resolve())
        self.assertEqual(config.work_root, startup_work.resolve())
        restarted = Config.load(self.path)
        self.assertEqual(restarted.masters_root, staged_masters.resolve())
        self.assertEqual(restarted.work_root, staged_work.resolve())

    def test_config_write_fsyncs_file_and_parent_where_supported(self):
        config = Config.load(self.path)
        with mock.patch("vodpipe.util.os.fsync", wraps=os.fsync) as fsync:
            config.apply_and_save({"summary": {"provider": "none"}})

        expected_calls = 1 if os.name == "nt" else 2
        self.assertGreaterEqual(fsync.call_count, expected_calls)

    @unittest.skipIf(os.name == "nt", "Windows permissions are ACL-based")
    def test_config_is_written_with_restrictive_permissions(self):
        Config.load(self.path).apply_and_save({
            "secrets": {"deepgram_api_key": "not-for-other-users"},
        })
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(payload["secrets"]["deepgram_api_key"],
                         "not-for-other-users")


if __name__ == "__main__":
    unittest.main()
