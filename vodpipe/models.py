"""Language-model transports for the rundown writer.

Kept separate from `summarize.py`, which is now only a prompt, because the
transport concerns are the substantial half: a subscription CLI that can hang, an
HTTP API that can rate-limit, and a response that can be cut off half way and
still look like an answer.

The truncation check is the part worth reading. `stop_reason == "max_tokens"`
(Anthropic) and `finish_reason == "length"` (everything OpenAI-shaped) mean the
model was still writing when it ran out of room; the text that came back is a
real prefix of a real answer, which is exactly why accepting it is dangerous -- a
rundown that stops mid-sentence reads as a short rundown.

**There are four kinds of transport here, not four providers.** *2026-08-18.*
`claude -p` shares the user's Claude subscription and hits its session limit on a
heavy recording day -- which is exactly what happened, losing a rundown to
"You've hit your session limit" three attempts in a row. So the engine is chosen
from:

* `claude-cli` -- headless `claude -p`, the original;
* `cli` -- any other subscription CLI, driven by `summary.cli_command`. This is
  how a ChatGPT or Gemini subscription is used: those sell a seat, not an
  endpoint, and their CLIs (`codex exec`, `gemini -p`) are the supported way in;
* `anthropic-api` -- the Anthropic Messages API;
* `kimi-api` / `deepseek-api` / `openai-api` / `openai-compatible` -- one
  OpenAI-shaped chat/completions transport pointed at a different base URL and
  key. Kimi and DeepSeek both publish OpenAI-compatible APIs, so they are
  configuration of one class rather than three classes of their own.

A provider is never asked to guess a model id: when `summary.model` is blank, or
the server rejects the one it was given, the transport asks the endpoint for its
own model list and puts the real ids in the error. Model names move faster than
this file can.
"""

from __future__ import annotations

import http.client
import json
import subprocess
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, Sequence

from .util import LOG, popen

ANTHROPIC_ENDPOINT = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"

# Retried: transport failures, rate limits, and the server's own faults. A 4xx
# other than 408/429 is a request this code got wrong, and sending it again
# four times only spends the user's quota to receive the same refusal.
#
# AUD2-033: every 5xx is retryable, not a hand-picked four. The fixed set left out
# 529 -- Anthropic's own "Overloaded", the single most common transient the API
# returns -- along with 505/507/508/510/511, so a routine overload was surfaced as
# a hard failure instead of being waited out. `asr.py` already retries the whole
# 5xx range; this now matches it.
RETRYABLE_STATUS = frozenset({408, 429})


def _is_retryable_status(code: int) -> bool:
    return code in RETRYABLE_STATUS or 500 <= code <= 599


class ModelError(RuntimeError):
    """The model could not be asked, or did not answer usably."""


class ModelUnavailable(ModelError):
    """No provider is configured. Callers may treat this as "skip", not "fail"."""


class Model(Protocol):
    name: str

    def ask(self, system: str, user: str, *, max_tokens: int = 8000,
            deadline: float | None = None) -> str:
        ...


class NullModel:
    name = "none"

    def ask(self, system: str, user: str, *, max_tokens: int = 8000,
            deadline: float | None = None) -> str:
        raise ModelUnavailable("no model provider is configured")


# ------------------------------------------------------------------ subscriptions


class _ProcessModel:
    """A headless CLI asked once, inside one absolute deadline.

    The user's text goes on stdin, never argv: a two-hour transcript is far past
    the Windows command-line length limit.
    """

    name = "cli"
    label = "the CLI"

    def __init__(self, executable: str, timeout: float = 900.0,
                 max_retries: int = 3) -> None:
        self.executable = executable
        self.timeout = timeout
        self.max_retries = max(1, max_retries)

    # -- subclass contract -------------------------------------------------

    def _argv(self, system: str) -> list[str]:
        raise NotImplementedError

    def _stdin(self, system: str, user: str) -> str:
        return user

    # -- transport ---------------------------------------------------------

    def ask(self, system: str, user: str, *, max_tokens: int = 8000,
            deadline: float | None = None) -> str:
        """Ask, retrying a transient failure within one shared deadline.

        AUD3-003: this had no retry at all, while the API transport next door
        retries four times with backoff. That asymmetry was backwards: a
        subscription CLI is the one sharing the user's usage limits, which this
        project has always known to be the likeliest transient (see CLAUDE.md,
        "a heavy recording day could bump into them"). One blip permanently
        marked a rundown failed.

        A non-zero exit is not classifiable from out here: the CLI reports a rate
        limit, a network drop and a bad flag identically. Retrying is still right
        because the cost is bounded by the same absolute deadline the single
        attempt already had, and a background rundown that takes a minute longer
        is strictly better than one that is simply absent.
        """
        call_deadline = time.monotonic() + max(0.0, self.timeout)
        if deadline is not None:
            call_deadline = min(call_deadline, deadline)

        last: ModelError | None = None
        for attempt in range(self.max_retries):
            try:
                return self._ask_once(system, user, call_deadline)
            except ModelError as exc:
                last = exc
                if attempt == self.max_retries - 1:
                    break
                delay = min(30.0, 2.0 ** attempt * 2.0)
                remaining = call_deadline - time.monotonic()
                # No point sleeping into a deadline that leaves no room to ask.
                if remaining <= delay:
                    break
                LOG.warning("%s attempt %d/%d failed (%s); retrying in %.0fs",
                            self.label, attempt + 1, self.max_retries, exc, delay)
                time.sleep(delay)
        raise last or ModelError(f"{self.label} failed")

    def _ask_once(self, system: str, user: str, call_deadline: float) -> str:
        started = time.monotonic()
        budget = max(0.0, call_deadline - started)
        if budget <= 0.0:
            raise ModelError(f"{self.label} total deadline expired")
        proc = popen(
            self._argv(system),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        timeout = call_deadline - time.monotonic()
        if timeout <= 0.0:
            proc.kill()
            _reap_killed_process(proc)
            raise ModelError(f"{self.label} total deadline expired")
        try:
            stdout, stderr = proc.communicate(
                self._stdin(system, user).encode("utf-8", "replace"),
                timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            cleanup_timeout = min(
                30.0, max(0.0, call_deadline - time.monotonic()))
            if cleanup_timeout > 0.0:
                try:
                    proc.communicate(timeout=cleanup_timeout)
                except subprocess.TimeoutExpired:
                    _reap_killed_process(proc)
                except Exception:               # pragma: no cover - already dying
                    pass
            else:
                # Reap and drain out of band rather than extending a caller's
                # absolute deadline for process cleanup.
                _reap_killed_process(proc)
            raise ModelError(f"{self.label} timed out after {budget:.0f}s")

        text = stdout.decode("utf-8", "replace").strip()
        if proc.returncode != 0 or not text:
            # Report whatever the CLI actually said. Taking stderr alone
            # produced a bare `claude -p failed (1):` with nothing after the
            # colon whenever it wrote its reason to stdout, which is what a
            # `--print` mode CLI naturally does -- so the one message the user
            # had to diagnose from carried no information at all.
            detail = (stderr.decode("utf-8", "replace").strip()
                      or text
                      or "no output on stdout or stderr")[-800:]
            if proc.returncode == 0:
                raise ModelError(
                    f"{self.label} exited 0 without writing an answer: {detail}")
            raise ModelError(
                f"{self.label} failed ({proc.returncode}): {detail}")
        return text


class ClaudeCliModel(_ProcessModel):
    """Headless `claude -p` against the user's existing subscription."""

    name = "claude-cli"
    label = "claude -p"

    def __init__(self, executable: str, timeout: float = 900.0,
                 max_retries: int = 3) -> None:
        if not executable:
            raise ModelUnavailable(
                "claude executable not found; set tools.claude in config")
        super().__init__(executable, timeout=timeout, max_retries=max_retries)

    def _argv(self, system: str) -> list[str]:
        return [self.executable,
                "--safe-mode",
                "--tools", "",
                "--disable-slash-commands",
                "--no-session-persistence",
                "--system-prompt", system,
                "--print",
                "--output-format", "text"]


# The placeholder a configured command uses to receive the instruction. A token
# equal to it is replaced wholesale; a token containing it is substituted inside,
# so both `--system-prompt {system}` and `--instructions={system}` work.
SYSTEM_PLACEHOLDER = "{system}"


class CliModel(_ProcessModel):
    """Any other headless CLI that answers on stdout.

    `claude -p` keeps its own class because its flags are known and its failure
    modes are documented here. This one exists for every other subscription tool
    -- OpenAI's `codex exec`, `gemini -p`, `opencode run` -- where the only thing
    this project can know is the shape of the contract:

    * the command comes verbatim from `summary.cli_command`;
    * any token containing `{system}` receives the instruction;
    * the transcript always arrives on **stdin**, because it is far past the
      Windows command-line length limit and no CLI takes it as an argument;
    * the rundown is whatever the process writes to **stdout**.

    If no token carries `{system}`, the instruction is prepended to stdin
    instead, separated by a blank line -- which is what a CLI that reads its whole
    prompt from stdin expects, and keeps the two halves in the order the model
    should read them.
    """

    name = "cli"

    def __init__(self, command: Sequence[str], timeout: float = 900.0,
                 max_retries: int = 3) -> None:
        parts = [str(part) for part in (command or []) if str(part) != ""]
        if not parts:
            raise ModelUnavailable(
                "summary.cli_command is empty; set it to the command that runs "
                'your CLI, e.g. ["codex", "exec", "--sandbox", "read-only"]')
        self.command = parts
        super().__init__(parts[0], timeout=timeout, max_retries=max_retries)
        self.label = Path(parts[0]).name or parts[0]

    def _argv(self, system: str) -> list[str]:
        return [part.replace(SYSTEM_PLACEHOLDER, system) for part in self.command]

    def _stdin(self, system: str, user: str) -> str:
        if any(SYSTEM_PLACEHOLDER in part for part in self.command):
            return user
        return f"{system}\n\n{user}"


def _reap_killed_process(proc: subprocess.Popen) -> None:
    def reap() -> None:
        try:
            proc.communicate(timeout=30)
        except subprocess.TimeoutExpired:       # pragma: no cover - kill ignored
            LOG.warning("a model CLI did not exit after being killed")
        except Exception:                       # pragma: no cover - already dying
            pass

    threading.Thread(target=reap, name="model-reaper", daemon=True).start()


# --------------------------------------------------------------------- HTTP APIs


class _HttpModel:
    """One absolute deadline, bounded retries, and a body a subclass supplies.

    Everything here was written for `AnthropicApiModel` and is unchanged by being
    shared: a deadline that spans every attempt rather than each one, the
    server's own `Retry-After` in preference to our backoff, and a socket timeout
    re-armed from the remaining budget on every read so a slow drip cannot outlive
    the deadline it was given.
    """

    name = "http"
    label = "the model API"

    def __init__(self, api_key: str, *, model: str, endpoint: str,
                 timeout: float = 900.0, max_retries: int = 4) -> None:
        self.api_key = api_key
        self.model = model
        self.endpoint = endpoint
        self.timeout = timeout
        self.max_retries = max(1, max_retries)

    # -- subclass contract -------------------------------------------------

    def _headers(self) -> dict[str, str]:
        raise NotImplementedError

    def _body(self, system: str, user: str, max_tokens: int) -> bytes:
        raise NotImplementedError

    def _parse(self, payload: dict[str, Any]) -> str:
        raise NotImplementedError

    # -- transport ---------------------------------------------------------

    def ask(self, system: str, user: str, *, max_tokens: int = 8000,
            deadline: float | None = None) -> str:
        body = self._body(system, user, max_tokens)

        # A single deadline across every attempt. Retrying with a per-attempt
        # timeout let a sequence of slow failures run for far longer than the
        # configured timeout suggested it could.
        own_deadline = time.monotonic() + max(0.0, self.timeout)
        deadline = own_deadline if deadline is None else min(deadline,
                                                              own_deadline)
        last: Exception | None = None
        attempts = 0
        for attempt in range(self.max_retries):
            if time.monotonic() >= deadline:
                last = ModelError(f"{self.label} total deadline expired")
                break
            attempts = attempt + 1
            try:
                return self._parse(self._post(body, deadline=deadline))
            except _Retryable as exc:
                last = exc.reason
                if attempt == self.max_retries - 1:
                    break
                delay = exc.retry_after
                if delay is None:
                    delay = min(30.0, 2.0 ** attempt * 2.0)
                delay = min(delay, max(0.0, deadline - time.monotonic()))
                if delay <= 0:
                    break
                LOG.warning("%s attempt %d/%d failed (%s); retrying in %.0fs",
                            self.label, attempt + 1, self.max_retries,
                            exc.reason, delay)
                time.sleep(delay)
        raise ModelError(f"{self.label} failed after {attempts} attempt(s): "
                         f"{last}")

    def _post(self, body: bytes, *, deadline: float) -> dict[str, Any]:
        request = urllib.request.Request(
            self.endpoint, data=body, method="POST", headers=self._headers())
        try:
            with urllib.request.urlopen(
                    request,
                    timeout=_remaining(deadline, self.label)) as response:
                response_body = _read_response(response, deadline, self.label)
        except urllib.error.HTTPError as exc:
            try:
                try:
                    detail = _read_response(exc, deadline, self.label).decode(
                        "utf-8", "replace")[:500]
                except (TimeoutError, OSError,
                        http.client.HTTPException) as read_exc:
                    detail = f"response body could not be read: {read_exc}"
            finally:
                exc.close()
            raise self._http_error(exc, detail) from exc
        except (urllib.error.URLError, TimeoutError, OSError,
                http.client.HTTPException) as exc:
            raise _Retryable(
                ModelError(f"{self.label} transport error: {exc}"), None) from exc
        try:
            payload = json.loads(response_body)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise _Retryable(
                ModelError(f"{self.label} sent unparsable JSON: {exc}"),
                None) from exc
        if not isinstance(payload, dict):
            raise ModelError(f"{self.label} response was not a JSON object")
        return payload

    def _http_error(self, exc: urllib.error.HTTPError,
                    detail: str) -> Exception:
        reason = ModelError(f"{self.label} HTTP {exc.code}: {detail}")
        if exc.code in (401, 403):
            # A bad key is not a transient condition.
            return ModelUnavailable(str(reason))
        if _is_retryable_status(exc.code):
            return _Retryable(reason, _retry_after(exc))
        return reason


class AnthropicApiModel(_HttpModel):
    """The Anthropic Messages API, for when subscription limits bind."""

    name = "anthropic-api"
    label = "anthropic"

    def __init__(self, api_key: str, model: str = "claude-sonnet-5",
                 timeout: float = 900.0, max_retries: int = 4) -> None:
        if not api_key:
            raise ModelUnavailable("no Anthropic API key configured")
        super().__init__(api_key, model=model or "claude-sonnet-5",
                         endpoint=ANTHROPIC_ENDPOINT, timeout=timeout,
                         max_retries=max_retries)

    def _headers(self) -> dict[str, str]:
        return {
            "x-api-key": self.api_key,
            "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json",
        }

    def _body(self, system: str, user: str, max_tokens: int) -> bytes:
        return json.dumps({
            "model": self.model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }).encode("utf-8")

    def _parse(self, payload: dict[str, Any]) -> str:
        stop = payload.get("stop_reason")
        if stop == "max_tokens":
            # AUD2-034. The prefix that came back is indistinguishable from a
            # complete short answer, which is precisely what makes accepting it
            # unsafe: a rundown ends mid-sentence and a review silently loses
            # every verdict after the cut.
            raise ModelError(
                "anthropic stopped at the token limit, so its answer is "
                "incomplete; raise summary.max_tokens or shorten the input")
        blocks = payload.get("content")
        if not isinstance(blocks, list):
            raise ModelError("anthropic response had no content blocks")
        # Joined with a newline rather than concatenated: separate text blocks
        # are separate pieces of output, and running them together merged the
        # last word of one into the first of the next.
        parts = [block.get("text", "") for block in blocks
                 if isinstance(block, dict) and block.get("type") == "text"]
        text = "\n".join(part for part in parts if part).strip()
        if not text:
            raise ModelError("anthropic returned an empty response")
        return text


@dataclass(frozen=True)
class ApiProvider:
    """One OpenAI-shaped endpoint: where it lives and what unlocks it."""

    name: str
    label: str
    base_url: str
    secret: str
    default_model: str
    keys_at: str


# Checked against each vendor's published API reference on 2026-08-18. The base
# URL and the key are the only things that differ; the request is the same
# `/chat/completions` in every case, which is why one class serves all of them.
API_PROVIDERS: dict[str, ApiProvider] = {
    "kimi-api": ApiProvider(
        "kimi-api", "Kimi (Moonshot)", "https://api.moonshot.ai/v1",
        "kimi_api_key", "kimi-k3", "https://platform.kimi.ai/"),
    "deepseek-api": ApiProvider(
        "deepseek-api", "DeepSeek", "https://api.deepseek.com/v1",
        "deepseek_api_key", "deepseek-v4-pro", "https://platform.deepseek.com/"),
    "openai-api": ApiProvider(
        "openai-api", "OpenAI", "https://api.openai.com/v1",
        "openai_api_key", "", "https://platform.openai.com/api-keys"),
    # Anything else that speaks the same protocol: OpenRouter, Groq, Together, a
    # local llama.cpp or Ollama server. `summary.base_url` is required here
    # because there is nothing sensible to default it to.
    "openai-compatible": ApiProvider(
        "openai-compatible", "OpenAI-compatible endpoint", "",
        "openai_compatible_api_key", "", ""),
}


class OpenAICompatibleModel(_HttpModel):
    """`POST {base}/chat/completions`, which is what almost everyone speaks.

    Two wrinkles are handled here rather than left to the operator:

    * **`max_tokens` was renamed.** Newer OpenAI reasoning models reject it and
      demand `max_completion_tokens`; most other vendors accept only the old
      name. Rather than making the user discover that from a 400, the first
      rejection that names the new parameter is retried once with it, and the
      choice sticks for the life of the object.
    * **model ids move.** A blank or rejected model is answered by asking the
      endpoint for its own list and putting the real ids in the error, so the fix
      is in the message rather than in a search engine.
    """

    def __init__(self, api_key: str, *, base_url: str, model: str,
                 provider: str = "openai-compatible", label: str | None = None,
                 timeout: float = 900.0, max_retries: int = 4) -> None:
        base = (base_url or "").strip().rstrip("/")
        if not base:
            raise ModelUnavailable(
                f"{provider} has no base URL; set summary.base_url")
        if not api_key:
            raise ModelUnavailable(f"no API key configured for {provider}")
        self.base_url = base
        self.name = provider
        self.label = label or provider
        self._token_field = "max_tokens"
        super().__init__(api_key, model=(model or "").strip(),
                         endpoint=f"{base}/chat/completions",
                         timeout=timeout, max_retries=max_retries)
        if not self.model:
            raise ModelUnavailable(
                f"no model is set for {self.label}; set summary.model to one "
                f"of: {self._offered_models()}")

    def _headers(self) -> dict[str, str]:
        return {
            "authorization": f"Bearer {self.api_key}",
            "content-type": "application/json",
        }

    def _body(self, system: str, user: str, max_tokens: int) -> bytes:
        return json.dumps({
            "model": self.model,
            self._token_field: max_tokens,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }).encode("utf-8")

    def ask(self, system: str, user: str, *, max_tokens: int = 8000,
            deadline: float | None = None) -> str:
        try:
            return super().ask(system, user, max_tokens=max_tokens,
                               deadline=deadline)
        except ModelError as exc:
            if (self._token_field == "max_tokens"
                    and "max_completion_tokens" in str(exc)):
                # The endpoint told us the parameter's new name. Take it at its
                # word once; a second failure is a real failure.
                LOG.info("%s wants max_completion_tokens; retrying with it",
                         self.label)
                self._token_field = "max_completion_tokens"
                return super().ask(system, user, max_tokens=max_tokens,
                                   deadline=deadline)
            raise

    def _http_error(self, exc: urllib.error.HTTPError,
                    detail: str) -> Exception:
        error = super()._http_error(exc, detail)
        if (isinstance(error, ModelError)
                and not isinstance(error, ModelUnavailable)
                and self.model
                and self.model in detail):
            # The server named our model in its complaint, so the model is the
            # thing to fix. Say what it will accept instead.
            return ModelError(
                f"{error} -- {self.label} offers: {self._offered_models()}")
        return error

    def _offered_models(self) -> str:
        """The endpoint's own model list, for an error message. Never raises."""
        request = urllib.request.Request(
            f"{self.base_url}/models", method="GET", headers=self._headers())
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                payload = json.loads(response.read(1 << 20))
        except Exception as exc:                # pragma: no cover - best effort
            return f"(could not list models: {exc})"
        rows = payload.get("data") if isinstance(payload, dict) else None
        ids = [str(row.get("id")) for row in rows or []
               if isinstance(row, dict) and row.get("id")]
        if not ids:
            return "(the endpoint listed no models)"
        shown = ", ".join(sorted(ids)[:24])
        return shown if len(ids) <= 24 else f"{shown}, ... ({len(ids)} in total)"

    def _parse(self, payload: dict[str, Any]) -> str:
        error = payload.get("error")
        if isinstance(error, dict) and error.get("message"):
            # Some compatible servers answer 200 with an error object inside.
            raise ModelError(f"{self.label} returned an error: "
                             f"{str(error.get('message'))[:400]}")
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices:
            raise ModelError(f"{self.label} response had no choices")
        first = choices[0]
        if not isinstance(first, dict):
            raise ModelError(f"{self.label} response had an unreadable choice")
        if first.get("finish_reason") == "length":
            # Same reasoning as Anthropic's max_tokens: a truncated rundown is
            # indistinguishable from a short one at every point downstream.
            raise ModelError(
                f"{self.label} stopped at the token limit, so its answer is "
                f"incomplete; raise summary.max_tokens or shorten the input")
        message = first.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        text = _content_text(content)
        if not text:
            raise ModelError(f"{self.label} returned an empty response")
        return text


def _content_text(content: Any) -> str:
    """`content` is a string almost everywhere and a list of parts elsewhere."""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = [part.get("text", "") for part in content
                 if isinstance(part, dict)
                 and part.get("type") in (None, "text", "output_text")]
        return "\n".join(part for part in parts if part).strip()
    return ""


class _Retryable(Exception):
    """Internal: a failure worth another attempt, with the server's own advice."""

    def __init__(self, reason: Exception, retry_after: float | None) -> None:
        super().__init__(str(reason))
        self.reason = reason
        self.retry_after = retry_after


def _remaining(deadline: float, label: str = "the model") -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0.0:
        raise TimeoutError(f"{label} total deadline expired")
    return remaining


def _set_response_timeout(response: Any, timeout: float) -> None:
    """Keep every socket read bounded by the remaining total deadline."""
    fp = getattr(response, "fp", None)
    raw = getattr(fp, "raw", None)
    if raw is None:
        # urllib may wrap HTTPResponse in addinfourl, leaving the buffered
        # socket one level deeper than it is on a bare HTTPResponse.
        raw = getattr(getattr(fp, "fp", None), "raw", None)
    sock = getattr(raw, "_sock", None)
    if sock is not None and hasattr(sock, "settimeout"):
        sock.settimeout(max(0.001, timeout))


def _read_response(response: Any, deadline: float,
                   label: str = "the model") -> bytes:
    chunks: list[bytes] = []
    reader = getattr(response, "read1", None)
    if not callable(reader):
        reader = response.read
    while True:
        _set_response_timeout(response, _remaining(deadline, label))
        chunk = reader(64 * 1024)
        if time.monotonic() >= deadline:
            raise TimeoutError(
                f"{label} total deadline expired while reading response")
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)


def _retry_after(exc: urllib.error.HTTPError) -> float | None:
    """The server's `Retry-After`, in seconds, if it gave one we can use."""
    try:
        raw = (exc.headers.get("Retry-After") or "").strip()
    except Exception:                          # pragma: no cover - odd headers
        return None
    if not raw:
        return None
    try:
        # Only the delta-seconds form. The HTTP-date form is legal but rare, and
        # parsing it wrong would be worse than falling back to our own backoff.
        return max(0.0, min(300.0, float(raw)))
    except ValueError:
        return None


# ------------------------------------------------------------------- the registry

# Every engine name, in the order the dashboard offers them. The schema builds
# its choice list from this, so a provider is added in exactly one place.
PROVIDER_NAMES: tuple[str, ...] = (
    "claude-cli",
    "anthropic-api",
    "kimi-api",
    "deepseek-api",
    "openai-api",
    "openai-compatible",
    "cli",
    "none",
)

# Which secret each engine needs, for `doctor`, the dashboard, and the pipeline's
# capability check. `claude-cli` and `cli` need none: they spend a subscription,
# not a key.
PROVIDER_SECRETS: dict[str, str] = {
    "anthropic-api": "anthropic_api_key",
    **{name: provider.secret for name, provider in API_PROVIDERS.items()},
}


def provider_default_model(provider: str) -> str:
    """The model a provider uses when `summary.model` is blank."""
    if provider == "anthropic-api":
        return "claude-sonnet-5"
    entry = API_PROVIDERS.get(provider)
    return entry.default_model if entry else ""


def build_model(config, claude_path: str | None, *,
                provider: str | None = None,
                timeout: float | None = None) -> Model:
    """The configured model transport, or `NullModel` when it is switched off.

    `provider` overrides the configured one, so a caller with a setting of its
    own does not have to reach into the config itself.

    `summary.model` is provider-scoped: it names the model for whichever engine
    is selected, and blank means "this provider's default". That is what makes
    switching engines a one-field change rather than two, and why switching to a
    provider with no default (OpenAI, or a compatible endpoint) reports the
    endpoint's real model list instead of guessing one.
    """
    name = (provider or config.get("summary.provider") or "claude-cli").lower()
    seconds = float(timeout if timeout is not None
                    else config.get("summary.timeout_seconds", 900))
    retries = int(config.get("summary.max_retries", 3))
    model = str(config.get("summary.model", "") or "").strip()

    if name == "none":
        return NullModel()
    if name == "claude-cli":
        return ClaudeCliModel(claude_path or "", timeout=seconds,
                              max_retries=retries)
    if name == "cli":
        return CliModel(config.get("summary.cli_command") or [],
                        timeout=seconds, max_retries=retries)
    if name == "anthropic-api":
        return AnthropicApiModel(
            config.secret("anthropic_api_key"),
            model=model or provider_default_model(name),
            timeout=seconds,
            max_retries=retries,
        )
    entry = API_PROVIDERS.get(name)
    if entry is not None:
        base = str(config.get("summary.base_url", "") or "").strip()
        return OpenAICompatibleModel(
            config.secret(entry.secret),
            base_url=base or entry.base_url,
            model=model or entry.default_model,
            provider=entry.name,
            label=entry.label,
            timeout=seconds,
            max_retries=retries,
        )
    raise ModelError(f"unknown model provider: {name}")
