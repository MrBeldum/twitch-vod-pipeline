"""The rundown engine registry, which now offers one engine and "off".

This file used to cover four transports -- `claude -p`, any other subscription
CLI, the Anthropic Messages API, and one OpenAI-shaped `/chat/completions` class
pointed at Kimi, DeepSeek, OpenAI or a compatible endpoint. They were added
2026-08-18 and removed 2026-08-19; the reasoning is in `vodpipe/models.py` and
the retirement itself is covered by `tests/test_retirement.py`.

What is left is the contract that outlives any particular engine: the names the
schema offers, the names `build_model` can actually build, and the one
capability verdict the dashboard, recovery, the API and the job all read.
"""

from __future__ import annotations

import unittest

from vodpipe.config import DEFAULTS, Config, deep_merge
from vodpipe.models import (
    PROVIDER_NAMES,
    ClaudeCliModel,
    GrokCliModel,
    ModelUnavailable,
    NullModel,
    build_model,
)
from vodpipe.schema import ConfigError, validate


def config(**summary) -> Config:
    secrets = summary.pop("secrets", {})
    return Config(deep_merge(DEFAULTS, {"summary": summary,
                                        "secrets": secrets}))


class ProviderSelectionTests(unittest.TestCase):
    def test_none_is_off_and_claude_cli_is_the_default(self):
        self.assertIsInstance(build_model(config(provider="none"), None),
                              NullModel)
        model = build_model(config(), "claude.exe")
        self.assertIsInstance(model, ClaudeCliModel)
        self.assertEqual(model.name, "claude-cli")

    def test_every_advertised_provider_can_be_named(self):
        """PROVIDER_NAMES is what the schema and the dashboard offer, so a name
        in it that build_model rejects is a setting that cannot be used."""
        for name in PROVIDER_NAMES:
            try:
                build_model(config(provider=name), "claude.exe",
                            grok_path="grok.exe")
            except ModelUnavailable:  # pragma: no cover - would be a real gap
                self.fail(f"{name} cannot be built from a complete config")

    def test_a_missing_claude_executable_is_unavailable_not_broken(self):
        """`ModelUnavailable` is the "skip this report" signal; a bare
        `ModelError` would mark the chunk failed instead."""
        with self.assertRaises(ModelUnavailable):
            build_model(config(provider="claude-cli"), None)

    def test_a_missing_grok_executable_is_unavailable_not_broken(self):
        with self.assertRaises(ModelUnavailable):
            build_model(config(provider="grok-cli"), "claude.exe", grok_path=None)

    def test_grok_cli_is_a_first_class_engine(self):
        model = build_model(config(provider="grok-cli"), "claude.exe",
                            grok_path="grok.exe")
        self.assertIsInstance(model, GrokCliModel)
        self.assertEqual(model.name, "grok-cli")
        self.assertEqual(model.model, "")

    def test_a_named_model_reaches_the_command_and_a_blank_one_does_not(self):
        """`summary.model` was dead config for `claude-cli` while the API
        engines existed -- build_model only ever handed it to them. With one
        engine left it either does something or it should not be a setting."""
        named = build_model(config(model="claude-opus-5"), "claude.exe")
        argv = named._argv("instruction")
        self.assertEqual(argv[argv.index("--model") + 1], "claude-opus-5")

        self.assertNotIn("--model",
                         build_model(config(model="  "), "claude.exe")._argv("i"))

    def test_the_timeout_and_attempt_budget_reach_the_transport(self):
        model = build_model(
            config(timeout_seconds=120.0, max_retries=5), "claude.exe")
        self.assertEqual(model.timeout, 120.0)
        self.assertEqual(model.max_retries, 5)


class ProviderSchemaTests(unittest.TestCase):
    def base(self, **summary) -> dict:
        return deep_merge(DEFAULTS, {"summary": summary})

    def test_every_provider_name_is_a_legal_setting(self):
        for name in PROVIDER_NAMES:
            validate(self.base(provider=name))

    def test_an_unknown_provider_is_refused(self):
        with self.assertRaises(ConfigError):
            validate(self.base(provider="mystery-api"))

    def test_a_retired_engine_falls_back_rather_than_refusing_to_start(self):
        """Removed 2026-08-19. The installed config.json named one of these the
        day before, so refusing it would turn the removal into "the application
        will not start". See tests/test_retirement.py."""
        for name in ("kimi-api", "deepseek-api", "openai-api",
                     "openai-compatible", "anthropic-api", "cli"):
            cleaned = validate(self.base(provider=name))
            self.assertEqual(cleaned["summary"]["provider"], "claude-cli", name)

    def test_a_blank_model_is_legal_because_it_means_the_default(self):
        cleaned = validate(self.base(provider="claude-cli", model=""))
        self.assertEqual(cleaned["summary"]["model"], "")


class CapabilityTests(unittest.TestCase):
    """One verdict, read by the dashboard, recovery, the API and the job."""

    def pipeline(self, **summary):
        import shutil
        import tempfile
        from pathlib import Path

        from vodpipe.pipeline import Pipeline

        root = Path(tempfile.mkdtemp(prefix="vodpipe-caps-"))
        self.addCleanup(shutil.rmtree, root, True)
        settings = Config(deep_merge(DEFAULTS, {
            "paths": {"masters_root": str(root / "masters"),
                      "work_root": str(root / "work")},
            "recording": {"free_space_floor_gb": 0, "hard_reserve_gb": 0},
            "watcher": {"enabled": False},
            "summary": summary,
        }), root / "config.json")
        settings.masters_root.mkdir(parents=True, exist_ok=True)
        pipeline = Pipeline(settings)
        self.addCleanup(pipeline.shutdown, job_timeout=10)
        return pipeline

    def test_the_engine_is_available_exactly_when_claude_is(self):
        import dataclasses

        pipeline = self.pipeline(provider="claude-cli")
        # `Tools` is frozen, so swap the whole record rather than one field.
        pipeline.tools = dataclasses.replace(pipeline.tools, claude="")
        available, reason = pipeline._summary_capability()
        self.assertFalse(available)
        self.assertIn("claude", reason)

        pipeline.tools = dataclasses.replace(pipeline.tools,
                                             claude="claude.exe")
        self.assertEqual(pipeline._summary_capability(), (True, ""))

    def test_grok_is_available_exactly_when_grok_is(self):
        import dataclasses

        pipeline = self.pipeline(provider="grok-cli")
        pipeline.tools = dataclasses.replace(pipeline.tools, grok="")
        available, reason = pipeline._summary_capability()
        self.assertFalse(available)
        self.assertIn("grok", reason)

        pipeline.tools = dataclasses.replace(pipeline.tools, grok="grok.exe")
        self.assertEqual(pipeline._summary_capability(), (True, ""))

    def test_switching_the_engine_off_is_reported_as_such(self):
        pipeline = self.pipeline(provider="none")
        available, reason = pipeline._summary_capability()
        self.assertFalse(available)
        self.assertIn("disabled", reason)

    def test_the_payload_names_the_selected_engine(self):
        import dataclasses

        pipeline = self.pipeline(provider="claude-cli")
        pipeline.tools = dataclasses.replace(pipeline.tools,
                                             claude="claude.exe")
        capabilities = pipeline.state_payload()["capabilities"]
        self.assertEqual(capabilities["summary_provider"], "claude-cli")
        self.assertTrue(capabilities["claude_cli"])
        self.assertTrue(capabilities["summary_available"])


if __name__ == "__main__":
    unittest.main()
