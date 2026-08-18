"""The rundown engine is pluggable: one prompt, several transports.

Added 2026-08-18, when `claude -p` answered three attempts in a row with
"You've hit your session limit" and the only alternative on offer was an
Anthropic API key -- which does not help someone whose spare capacity is a Kimi,
DeepSeek or ChatGPT subscription.

The contracts worth holding still are: which endpoint each provider talks to,
that the request carries the instruction and the transcript as separate roles,
that a truncated answer is refused rather than published, and that a subscription
CLI receives the transcript on stdin.
"""

from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import vodpipe.models as models
from vodpipe.config import DEFAULTS, Config, deep_merge
from vodpipe.models import (
    API_PROVIDERS,
    PROVIDER_NAMES,
    CliModel,
    ModelError,
    ModelUnavailable,
    NullModel,
    OpenAICompatibleModel,
    build_model,
)
from vodpipe.schema import ConfigError, validate


def config(**summary) -> Config:
    secrets = summary.pop("secrets", {})
    data = deep_merge(DEFAULTS, {
        "summary": summary,
        "secrets": secrets,
    })
    return Config(data)


class FakeResponse:
    """Enough of an HTTPResponse for the shared reader and the model lister."""

    def __init__(self, payload) -> None:
        self.body = (payload if isinstance(payload, bytes)
                     else json.dumps(payload).encode("utf-8"))
        self.fp = SimpleNamespace(raw=SimpleNamespace(_sock=None))
        self._offset = 0

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read1(self, size: int) -> bytes:
        chunk = self.body[self._offset:self._offset + size]
        self._offset += len(chunk)
        return chunk

    def read(self, size: int = -1) -> bytes:
        return self.read1(len(self.body) if size < 0 else size)


def answer(text: str, finish: str = "stop") -> dict:
    return {"choices": [{"finish_reason": finish,
                         "message": {"role": "assistant", "content": text}}]}


class ProviderSelectionTests(unittest.TestCase):
    def test_kimi_talks_to_moonshot_with_its_own_default_model(self):
        model = build_model(config(provider="kimi-api",
                                   secrets={"kimi_api_key": "k"}), None)
        self.assertIsInstance(model, OpenAICompatibleModel)
        self.assertEqual(model.endpoint,
                         "https://api.moonshot.ai/v1/chat/completions")
        self.assertEqual(model.model, "kimi-k3")
        self.assertEqual(model._headers()["authorization"], "Bearer k")

    def test_deepseek_talks_to_deepseek(self):
        model = build_model(config(provider="deepseek-api",
                                   secrets={"deepseek_api_key": "k"}), None)
        self.assertEqual(model.endpoint,
                         "https://api.deepseek.com/v1/chat/completions")
        self.assertEqual(model.model, "deepseek-v4-pro")

    def test_the_configured_model_wins_over_the_default(self):
        model = build_model(config(provider="kimi-api", model="kimi-k2.6",
                                   secrets={"kimi_api_key": "k"}), None)
        self.assertEqual(model.model, "kimi-k2.6")

    def test_a_provider_without_a_key_is_unavailable_not_broken(self):
        with self.assertRaises(ModelUnavailable):
            build_model(config(provider="deepseek-api"), None)

    def test_an_endpoint_of_your_own_needs_a_base_url(self):
        with self.assertRaises(ModelUnavailable):
            build_model(config(provider="openai-compatible",
                               model="local",
                               secrets={"openai_compatible_api_key": "k"}), None)
        model = build_model(config(
            provider="openai-compatible", model="local",
            base_url="http://127.0.0.1:11434/v1/",
            secrets={"openai_compatible_api_key": "k"}), None)
        self.assertEqual(model.endpoint,
                         "http://127.0.0.1:11434/v1/chat/completions")

    def test_none_is_still_off_and_claude_cli_is_still_the_default(self):
        self.assertIsInstance(build_model(config(provider="none"), None),
                              NullModel)
        model = build_model(config(), "claude.exe")
        self.assertEqual(model.name, "claude-cli")

    def test_a_blank_model_reports_what_the_endpoint_actually_offers(self):
        """Model ids move faster than this repository does, so the fix belongs
        in the error rather than in a search engine."""
        listing = FakeResponse({"data": [{"id": "gpt-oldest"}, {"id": "gpt-newest"}]})
        with patch.object(models.urllib.request, "urlopen",
                          return_value=listing):
            with self.assertRaises(ModelUnavailable) as caught:
                build_model(config(provider="openai-api",
                                   secrets={"openai_api_key": "k"}), None)
        message = str(caught.exception)
        self.assertIn("gpt-newest", message)
        self.assertIn("summary.model", message)

    def test_every_advertised_provider_can_be_named(self):
        """PROVIDER_NAMES is what the schema and the dashboard offer, so a name
        in it that build_model rejects is a setting that cannot be used."""
        for name in PROVIDER_NAMES:
            secrets = {value: "k" for value in
                       models.PROVIDER_SECRETS.values()}
            settings = dict(provider=name, model="a-model", secrets=secrets,
                            base_url="https://example.test/v1",
                            cli_command=["some-cli"])
            try:
                build_model(config(**settings), "claude.exe")
            except ModelUnavailable:  # pragma: no cover - would be a real gap
                self.fail(f"{name} cannot be built from a complete config")


class OpenAIRequestTests(unittest.TestCase):
    def model(self, **changes) -> OpenAICompatibleModel:
        options = dict(base_url="https://example.test/v1", model="m",
                       provider="kimi-api", label="Kimi", timeout=30.0,
                       max_retries=1)
        options.update(changes)
        return OpenAICompatibleModel("secret", **options)

    def sent(self, response, **changes) -> tuple[str, dict]:
        model = self.model(**changes)
        with patch.object(models.urllib.request, "urlopen",
                          return_value=FakeResponse(response)) as urlopen:
            text = model.ask("the instruction", "the transcript")
        request = urlopen.call_args.args[0]
        return text, json.loads(request.data)

    def test_the_instruction_and_the_transcript_are_separate_roles(self):
        text, body = self.sent(answer("## Overview"))
        self.assertEqual(text, "## Overview")
        self.assertEqual(body["messages"], [
            {"role": "system", "content": "the instruction"},
            {"role": "user", "content": "the transcript"},
        ])
        self.assertEqual(body["model"], "m")
        self.assertEqual(body["max_tokens"], 8000)

    def test_a_truncated_answer_is_refused(self):
        """A rundown that stops mid-sentence reads as a short rundown."""
        with self.assertRaisesRegex(ModelError, "token limit"):
            self.sent(answer("## Overview", finish="length"))

    def test_content_delivered_as_parts_is_joined(self):
        text, _ = self.sent({"choices": [{"message": {"content": [
            {"type": "text", "text": "first"},
            {"type": "text", "text": "second"},
        ]}}]})
        self.assertEqual(text, "first\nsecond")

    def test_an_error_object_in_a_200_is_still_an_error(self):
        with self.assertRaisesRegex(ModelError, "quota"):
            self.sent({"error": {"message": "quota exceeded"}})

    def test_an_empty_answer_is_an_error(self):
        with self.assertRaisesRegex(ModelError, "empty"):
            self.sent(answer("   "))

    def test_the_renamed_token_parameter_is_adopted_once(self):
        """Newer OpenAI models reject max_tokens and name their replacement."""
        model = self.model()
        bodies = []

        def urlopen(request, timeout=None):
            bodies.append(json.loads(request.data))
            if "max_tokens" in bodies[-1]:
                raise models.urllib.error.HTTPError(
                    request.full_url, 400, "Bad Request", {},
                    _BodyStream(json.dumps({"error": {
                        "message": "Unsupported parameter: 'max_tokens' is not "
                                   "supported. Use 'max_completion_tokens'."}})))
            return FakeResponse(answer("## Overview"))

        with patch.object(models.urllib.request, "urlopen", side_effect=urlopen):
            self.assertEqual(model.ask("system", "user"), "## Overview")
        self.assertEqual(len(bodies), 2)
        self.assertIn("max_completion_tokens", bodies[1])
        self.assertEqual(model._token_field, "max_completion_tokens")

    def test_a_rejected_key_is_unavailable_rather_than_retried(self):
        model = self.model(max_retries=4)

        def urlopen(request, timeout=None):
            raise models.urllib.error.HTTPError(
                request.full_url, 401, "Unauthorized", {},
                _BodyStream('{"error": {"message": "bad key"}}'))

        with patch.object(models.urllib.request, "urlopen",
                          side_effect=urlopen) as sender:
            with self.assertRaises(ModelUnavailable):
                model.ask("system", "user")
        self.assertEqual(sender.call_count, 1)


class _BodyStream:
    """The file-like third argument HTTPError expects."""

    def __init__(self, text: str) -> None:
        self.body = text.encode("utf-8")
        self._offset = 0
        self.fp = SimpleNamespace(raw=SimpleNamespace(_sock=None))

    def read1(self, size: int) -> bytes:
        chunk = self.body[self._offset:self._offset + size]
        self._offset += len(chunk)
        return chunk

    def read(self, size: int = -1) -> bytes:
        return self.read1(len(self.body) if size < 0 else size)

    def close(self) -> None:
        pass


class SubscriptionCliTests(unittest.TestCase):
    """A ChatGPT or Gemini seat has no API; its CLI is the supported way in."""

    def run_cli(self, command, stdout=b"## Overview"):
        class Process:
            returncode = 0

            def __init__(self) -> None:
                self.stdin = None

            def communicate(self, data=None, *, timeout=None):
                Process.received = data
                return stdout, b""

        model = CliModel(command, timeout=30.0, max_retries=1)
        with patch.object(models, "popen", return_value=Process()) as spawn:
            text = model.ask("the instruction", "the transcript")
        return text, spawn.call_args.args[0], Process.received.decode("utf-8")

    def test_the_transcript_always_arrives_on_stdin(self):
        """It is far past the Windows command-line limit; nothing else works."""
        _, argv, stdin = self.run_cli(["codex", "exec", "--sandbox", "read-only"])
        self.assertEqual(argv, ["codex", "exec", "--sandbox", "read-only"])
        self.assertTrue(stdin.endswith("the transcript"))
        self.assertIn("the instruction", stdin)

    def test_a_placeholder_moves_the_instruction_into_the_arguments(self):
        _, argv, stdin = self.run_cli(["gemini", "-p", "{system}"])
        self.assertEqual(argv, ["gemini", "-p", "the instruction"])
        self.assertEqual(stdin, "the transcript")

    def test_a_placeholder_inside_a_longer_argument_is_substituted(self):
        _, argv, _ = self.run_cli(["tool", "--instructions={system}"])
        self.assertEqual(argv, ["tool", "--instructions=the instruction"])

    def test_no_command_is_unavailable_rather_than_a_crash(self):
        with self.assertRaisesRegex(ModelUnavailable, "cli_command"):
            CliModel([])

    def test_the_label_is_the_command_so_a_failure_says_what_failed(self):
        model = CliModel([r"C:\tools\codex.exe", "exec"])
        self.assertEqual(model.label, "codex.exe")


class ProviderSchemaTests(unittest.TestCase):
    def base(self, **summary) -> dict:
        return deep_merge(DEFAULTS, {"summary": summary})

    def test_every_provider_name_is_a_legal_setting(self):
        for name in PROVIDER_NAMES:
            extra = {}
            if name == "openai-compatible":
                extra["base_url"] = "https://example.test/v1"
            if name == "cli":
                extra["cli_command"] = ["codex", "exec"]
            validate(self.base(provider=name, **extra))

    def test_an_unknown_provider_is_refused(self):
        with self.assertRaises(ConfigError):
            validate(self.base(provider="mystery-api"))

    def test_an_endpointless_compatible_provider_is_refused_at_entry(self):
        """Otherwise it fails in the background, once per chunk, hours later."""
        with self.assertRaisesRegex(ConfigError, "base_url"):
            validate(self.base(provider="openai-compatible"))

    def test_a_commandless_cli_provider_is_refused_at_entry(self):
        with self.assertRaisesRegex(ConfigError, "cli_command"):
            validate(self.base(provider="cli"))

    def test_both_are_allowed_while_rundowns_are_switched_off(self):
        validate(self.base(provider="cli", enabled=False))

    def test_a_base_url_must_be_an_api_root(self):
        for bad in ("ftp://example.test/v1", "example.test/v1",
                    "https://example.test/v1?key=1"):
            with self.assertRaises(ConfigError, msg=bad):
                validate(self.base(provider="openai-compatible", base_url=bad))

    def test_a_blank_model_is_legal_because_it_means_the_default(self):
        cleaned = validate(self.base(provider="kimi-api", model=""))
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
        secrets = summary.pop("secrets", {})
        settings = Config(deep_merge(DEFAULTS, {
            "paths": {"masters_root": str(root / "masters"),
                      "work_root": str(root / "work")},
            "recording": {"free_space_floor_gb": 0, "hard_reserve_gb": 0},
            "watcher": {"enabled": False},
            "summary": summary,
            "secrets": secrets,
        }), root / "config.json")
        settings.masters_root.mkdir(parents=True, exist_ok=True)
        pipeline = Pipeline(settings)
        self.addCleanup(pipeline.shutdown, job_timeout=10)
        return pipeline

    def test_each_api_engine_reports_its_own_missing_key(self):
        for name, provider in API_PROVIDERS.items():
            settings = {"provider": name, "model": "m"}
            if name == "openai-compatible":
                settings["base_url"] = "https://example.test/v1"
            pipeline = self.pipeline(**settings)
            available, reason = pipeline._summary_capability()
            self.assertFalse(available, name)
            self.assertIn(provider.secret, reason)

            pipeline = self.pipeline(secrets={provider.secret: "k"}, **settings)
            available, reason = pipeline._summary_capability()
            self.assertTrue(available, f"{name}: {reason}")

    def test_the_cli_engine_reports_a_missing_command(self):
        pipeline = self.pipeline(provider="cli", cli_command=["codex", "exec"])
        self.assertEqual(pipeline._summary_capability(), (True, ""))

    def test_the_payload_names_the_selected_engine_and_its_key(self):
        pipeline = self.pipeline(provider="kimi-api",
                                 secrets={"kimi_api_key": "k"})
        capabilities = pipeline.state_payload()["capabilities"]
        self.assertEqual(capabilities["summary_provider"], "kimi-api")
        self.assertEqual(capabilities["summary_key_name"], "kimi_api_key")
        self.assertTrue(capabilities["summary_key"])
        self.assertTrue(capabilities["summary_available"])


if __name__ == "__main__":
    unittest.main()
