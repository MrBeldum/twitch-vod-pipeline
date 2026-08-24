"""Removing a feature without breaking the installations that had it.

This build does not produce an edited cut. The operator's `config.json` and
every `session.json` written while it did still carry its keys and fields, and
both readers are deliberately strict -- `schema._walk` raises on an unknown
config path and `state._known_fields` raises on an unknown manifest field. So
"delete the code" is not enough: deleting a key from `SCHEMA` alone makes the
application refuse to start on the operator's own settings file, and deleting a
field from `_CHUNK_FIELDS` alone makes every recorded session unreadable.

Both are handled the same way and for the same reason: the name stays *known*
and is dropped on load, so it validates today and is gone from the file after
the next save. There was no test for any of this before; there is now, because
the failure mode is "the application will not start" and it only appears on a
machine that has the old file.
"""

from __future__ import annotations

import copy
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from vodpipe.config import DEFAULTS, deep_merge
from vodpipe.schema import (
    RETIRED_MODELS,
    RETIRED_PATHS,
    RETIRED_PROVIDERS,
    SCHEMA,
    ConfigError,
    validate,
)
from vodpipe.state import (
    _RETIRED_CHUNK_FIELDS,
    _CHUNK_FIELDS,
    Session,
    ManifestValidationError,
    _validate_manifest,
)

# What a config.json written by the build that had the edited cut looks like.
RETIRED_CONFIG = {
    "edit": {
        "enabled": True,
        "suffix": "_Edited",
        "folder_name": "Edited",
        "encoder": "auto",
        "quality": 22,
        "audio_bitrate": "192k",
        "remove_silence": True,
        "noise_floor_db": -41.0,
        "min_silence_seconds": 0.2,
        "min_speech_seconds": 0.2,
        "margin_seconds": 0.2,
        "min_cut_seconds": 0.25,
        "fillers": "sounds",
        "repeats": "restarts",
        "censor": "mute",
        "censor_margin_seconds": 0.05,
        "crossfade_ms": 20,
        "mute_ramp_ms": 10,
        "max_removed_fraction": 0.75,
    },
}

# ...and by the branch where a model decided the cut.
RETIRED_MODEL_CONFIG = {
    "edit": {
        "engine": "model",
        "model_provider": "claude-cli",
        "model_name": "claude-sonnet-5",
        "model_timeout_seconds": 900,
        "model_max_retries": 3,
        "model_window_seconds": 600,
        "model_authority": "override",
        "model_on_failure": "rules",
        "model_concurrency": 2,
        "model_cache": True,
        "min_removed_fraction": 0.01,
    },
}


# What a config.json written by the one-day build with four rundown engines
# looks like. The API keys matter as much as the settings: they are real
# secrets sitting in the operator's file, and retirement is what erases them.
RETIRED_SUMMARY_CONFIG = {
    "summary": {
        "provider": "claude-cli",
        "base_url": "https://api.moonshot.ai/v1",
        "cli_command": ["codex", "exec", "--sandbox", "read-only"],
    },
    "secrets": {
        "anthropic_api_key": "sk-ant-old",
        "openai_api_key": "sk-openai-old",
        "kimi_api_key": "sk-kimi-old",
        "deepseek_api_key": "sk-deepseek-old",
        "openai_compatible_api_key": "sk-other-old",
    },
}


class RetiredConfigTests(unittest.TestCase):
    def test_a_config_from_the_build_that_had_the_feature_still_loads(self):
        cleaned = validate(deep_merge(copy.deepcopy(DEFAULTS), RETIRED_CONFIG))
        self.assertNotIn("edit", cleaned)

    def test_a_config_from_the_model_branch_also_loads(self):
        merged = deep_merge(copy.deepcopy(DEFAULTS), RETIRED_CONFIG)
        cleaned = validate(deep_merge(merged, RETIRED_MODEL_CONFIG))
        self.assertNotIn("edit", cleaned)

    def test_the_whole_section_is_retired_not_its_keys_one_by_one(self):
        """`_walk` only recurses into a branch that still has a rule underneath
        it, so retiring the thirty keys individually left the `edit` container
        itself unknown -- and an installed config.json that has it would stop
        the application starting. Retiring the section covers every variant of
        the feature, including keys this build never knew."""
        self.assertIn("edit", RETIRED_PATHS)
        self.assertEqual([name for name in SCHEMA if name.startswith("edit.")],
                         [])

    def test_a_setting_from_a_branch_this_build_never_had_still_loads(self):
        cleaned = validate(deep_merge(
            copy.deepcopy(DEFAULTS),
            {"edit": {"a_key_from_some_future_variant": [1, 2, 3]}}))
        self.assertNotIn("edit", cleaned)

    def test_a_key_that_was_never_ours_is_still_refused(self):
        """Retirement is a named list, not a general amnesty -- the strict walk
        is what stops a typo elsewhere silently doing nothing."""
        with self.assertRaises(ConfigError):
            validate(deep_merge(copy.deepcopy(DEFAULTS),
                                {"recording": {"invented_setting": 1}}))

    def test_nothing_is_both_live_and_retired(self):
        self.assertEqual(set(SCHEMA) & RETIRED_PATHS, set())



class RetiredSummaryEngineTests(unittest.TestCase):
    """The paid-API rundown engines, removed 2026-08-19.

    Unlike `edit` above, these are retired key by key rather than by section:
    `summary` and `secrets` both still carry live rules, so `_walk` goes on
    recursing into them and neither container becomes unknown. Retiring the
    section would have been wrong here -- it would have taken
    `secrets.deepgram_api_key` with it.
    """

    def test_a_config_from_the_build_that_had_them_still_loads(self):
        cleaned = validate(deep_merge(copy.deepcopy(DEFAULTS),
                                      RETIRED_SUMMARY_CONFIG))
        self.assertNotIn("base_url", cleaned["summary"])
        self.assertNotIn("cli_command", cleaned["summary"])

    def test_the_api_keys_are_dropped_rather_than_carried_forward(self):
        """They are gone from the file after the next save, which is the
        point: a key for an engine nothing can select is a live secret with
        no purpose."""
        cleaned = validate(deep_merge(copy.deepcopy(DEFAULTS),
                                      RETIRED_SUMMARY_CONFIG))
        self.assertEqual(sorted(cleaned["secrets"]),
                         ["deepgram_api_key", "twitch_oauth_token"])
        self.assertNotIn("sk-kimi-old", json.dumps(cleaned))

    def test_the_live_secrets_survive_beside_the_retired_ones(self):
        merged = deep_merge(copy.deepcopy(DEFAULTS), RETIRED_SUMMARY_CONFIG)
        merged["secrets"]["deepgram_api_key"] = "dg-keep"
        cleaned = validate(merged)
        self.assertEqual(cleaned["secrets"]["deepgram_api_key"], "dg-keep")

    def test_a_retired_engine_name_falls_back_to_the_one_that_remains(self):
        """The retired thing here is a *value*, not a path, so `RETIRED_PATHS`
        does not reach it -- and the operator's installed config.json named
        `kimi-api` the day before. Refusing it would be the exact failure
        retirement exists to prevent, so `_summary_provider` rewrites it."""
        for name in RETIRED_PROVIDERS:
            cleaned = validate(deep_merge(copy.deepcopy(DEFAULTS),
                                          {"summary": {"provider": name}}))
            self.assertEqual(cleaned["summary"]["provider"], "claude-cli", name)

    def test_a_retired_engines_model_name_goes_with_it(self):
        """`summary.model` names a model *for the selected engine*. Left behind,
        `kimi-k3` would be handed to `claude -p --model` and turn a silent
        fallback into a failed rundown once per chunk."""
        cleaned = validate(deep_merge(
            copy.deepcopy(DEFAULTS),
            {"summary": {"provider": "kimi-api", "model": "kimi-k3"}}))
        self.assertEqual(cleaned["summary"]["provider"], "claude-cli")
        self.assertNotIn("model", cleaned["summary"])

    def test_a_live_engines_model_name_is_left_alone(self):
        cleaned = validate(deep_merge(
            copy.deepcopy(DEFAULTS),
            {"summary": {"provider": "claude-cli", "model": "claude-opus-5"}}))
        self.assertEqual(cleaned["summary"]["model"], "claude-opus-5")

    def test_grok_build_is_rewritten_to_the_cli_default(self):
        """Grok CLI 1.0.5 rejects `grok-build` as an unknown model id. Blank
        means the CLI default, which is currently Grok 4.6."""
        self.assertIn("grok-build", RETIRED_MODELS)
        cleaned = validate(deep_merge(
            copy.deepcopy(DEFAULTS),
            {"summary": {"provider": "grok-cli", "model": "grok-build"}}))
        self.assertEqual(cleaned["summary"]["provider"], "grok-cli")
        self.assertEqual(cleaned["summary"]["model"], "")

    def test_an_engine_that_was_never_ours_is_still_refused(self):
        """The fallback is a named list, not a general amnesty: a typo in the
        engine name must still be caught while the operator is looking at it."""
        with self.assertRaises(ConfigError):
            validate(deep_merge(copy.deepcopy(DEFAULTS),
                                {"summary": {"provider": "gemini-api"}}))

    def test_no_retired_engine_is_also_a_live_one(self):
        from vodpipe.models import PROVIDER_NAMES

        self.assertEqual(set(PROVIDER_NAMES) & RETIRED_PROVIDERS, set())

    def test_nothing_is_both_live_and_retired(self):
        self.assertEqual(set(SCHEMA) & RETIRED_PATHS, set())


class RetiredManifestTests(unittest.TestCase):
    def setUp(self):
        # `_validate_manifest` also checks the rooted layout
        # (<masters_root>/<channel>/<session_id>/session.json), so the paths
        # have to be real ones in that shape.
        self.tmp = Path(tempfile.mkdtemp(prefix="vodpipe-retire-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.root = self.tmp / "masters"
        directory = self.root / "chan" / "sess"
        directory.mkdir(parents=True)
        self.path = directory / "session.json"

    def check(self, raw: dict) -> None:
        _validate_manifest(raw, self.root, self.path)

    def manifest(self, **chunk_extra) -> dict:
        chunk = {
            "index": 0, "session_id": "sess", "channel": "chan",
            "started_at": 1.0, "master_name": "chan_c000.mp4",
            "duration": 10.0, "status": "complete",
        }
        chunk.update(chunk_extra)
        return {
            "session_id": "sess", "channel": "chan", "started_at": 1.0,
            "directory": str(self.path.parent), "status": "complete",
            "chunks": [chunk],
        }

    def test_a_manifest_written_with_the_edit_fields_still_validates(self):
        raw = self.manifest(edit_status="done", edit_error="",
                            edit_name="chan_c000_Edited.mp4")
        self.check(raw)

    def test_the_retired_fields_are_dropped_on_load(self):
        raw = self.manifest(edit_status="done", edit_error="it broke",
                            edit_name="chan_c000_Edited.mp4")
        session = Session.from_dict(raw)
        stored = session.chunks[0].to_dict()
        for field in _RETIRED_CHUNK_FIELDS:
            self.assertNotIn(field, stored)

    def test_a_retired_error_does_not_resurface_in_the_errors_map(self):
        """`errors` is what the dashboard renders. A failure from an artifact
        this build does not produce would be a problem nobody can act on."""
        raw = self.manifest(edit_status="error", edit_error="it broke")
        session = Session.from_dict(raw)
        self.assertEqual(session.chunks[0].errors, {})

    def test_a_retired_errors_entry_is_accepted_rather_than_fatal(self):
        raw = self.manifest(errors={"edit": "it broke"})
        self.check(raw)

    def test_an_invented_field_is_still_refused(self):
        with self.assertRaises(ManifestValidationError):
            self.check(self.manifest(invented_field=1))

    def test_nothing_is_both_live_and_retired(self):
        self.assertEqual(_CHUNK_FIELDS & _RETIRED_CHUNK_FIELDS, frozenset())

    def test_a_saved_manifest_round_trips_without_the_retired_fields(self):
        """The point of dropping rather than preserving: after one save the old
        names are gone from the file, so the next build need not know them."""
        raw = self.manifest(edit_status="done", edit_name="x.mp4")
        once = json.loads(json.dumps(Session.from_dict(raw).to_dict()))
        self.check(once)
        self.assertEqual(
            [name for name in once["chunks"][0] if name.startswith("edit")], [])


if __name__ == "__main__":
    unittest.main()
