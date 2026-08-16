"""Language-model transports for the rundown writer.

Kept separate from `summarize.py`, which is now only a prompt, because the
transport concerns are the substantial half: a subscription CLI that can hang, an
HTTP API that can rate-limit, and a response that can be cut off half way and
still look like an answer.

The truncation check is the part worth reading. `stop_reason == "max_tokens"`
means the model was still writing when it ran out of room; the text that came
back is a real prefix of a real answer, which is exactly why accepting it is
dangerous -- a rundown that stops mid-sentence reads as a short rundown.
"""

from __future__ import annotations

import http.client
import json
import subprocess
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Protocol

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


class ClaudeCliModel:
    """Headless `claude -p` against the user's existing subscription.

    The user's text goes on stdin, never argv: a two-hour transcript is far past
    the Windows command-line length limit.
    """

    name = "claude-cli"

    def __init__(self, executable: str, timeout: float = 900.0,
                 max_retries: int = 3) -> None:
        if not executable:
            raise ModelUnavailable(
                "claude executable not found; set tools.claude in config")
        self.executable = executable
        self.timeout = timeout
        self.max_retries = max(1, max_retries)

    def ask(self, system: str, user: str, *, max_tokens: int = 8000,
            deadline: float | None = None) -> str:
        """Ask, retrying a transient failure within one shared deadline.

        AUD3-003: this had no retry at all, while the API transport next door
        retries four times with backoff. That asymmetry was backwards: `claude -p`
        is the *default* provider and the one that shares the user's subscription
        usage limits, which this project has always known to be the likeliest
        transient (see CLAUDE.md, "a heavy recording day could bump into them").
        One blip permanently marked a rundown failed -- c000 of the 2026-08-16
        recording succeeded, was regenerated after a seam stitch, and lost the
        regeneration to a bare `claude -p failed (1):`.

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
                LOG.warning("claude -p attempt %d/%d failed (%s); retrying in "
                            "%.0fs", attempt + 1, self.max_retries, exc, delay)
                time.sleep(delay)
        raise last or ModelError("claude -p failed")

    def _ask_once(self, system: str, user: str, call_deadline: float) -> str:
        started = time.monotonic()
        budget = max(0.0, call_deadline - started)
        if budget <= 0.0:
            raise ModelError("claude -p total deadline expired")
        proc = popen(
            [self.executable,
             "--safe-mode",
             "--tools", "",
             "--disable-slash-commands",
             "--no-session-persistence",
             "--system-prompt", system,
             "--print",
             "--output-format", "text"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        timeout = call_deadline - time.monotonic()
        if timeout <= 0.0:
            proc.kill()
            _reap_killed_process(proc)
            raise ModelError("claude -p total deadline expired")
        try:
            stdout, stderr = proc.communicate(
                user.encode("utf-8", "replace"), timeout=timeout)
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
            raise ModelError(f"claude -p timed out after {budget:.0f}s")

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
                    f"claude -p exited 0 without writing an answer: {detail}")
            raise ModelError(f"claude -p failed ({proc.returncode}): {detail}")
        return text


def _reap_killed_process(proc: subprocess.Popen) -> None:
    def reap() -> None:
        try:
            proc.communicate(timeout=30)
        except subprocess.TimeoutExpired:       # pragma: no cover - kill ignored
            LOG.warning("claude -p did not exit after being killed")
        except Exception:                       # pragma: no cover - already dying
            pass

    threading.Thread(target=reap, name="claude-reaper", daemon=True).start()


class AnthropicApiModel:
    """The HTTP API, for when subscription limits are the binding constraint."""

    name = "anthropic-api"

    def __init__(self, api_key: str, model: str = "claude-sonnet-5",
                 timeout: float = 900.0, max_retries: int = 4) -> None:
        if not api_key:
            raise ModelUnavailable("no Anthropic API key configured")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.max_retries = max(1, max_retries)

    def ask(self, system: str, user: str, *, max_tokens: int = 8000,
            deadline: float | None = None) -> str:
        body = json.dumps({
            "model": self.model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }).encode("utf-8")

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
                last = ModelError("anthropic total deadline expired")
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
                LOG.warning("anthropic attempt %d/%d failed (%s); retrying in "
                            "%.0fs", attempt + 1, self.max_retries,
                            exc.reason, delay)
                time.sleep(delay)
        raise ModelError(f"anthropic failed after {attempts} attempt(s): "
                         f"{last}")

    def _post(self, body: bytes, *, deadline: float) -> dict[str, Any]:
        request = urllib.request.Request(
            ANTHROPIC_ENDPOINT,
            data=body,
            method="POST",
            headers={
                "x-api-key": self.api_key,
                "anthropic-version": ANTHROPIC_VERSION,
                "content-type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(
                    request, timeout=_remaining(deadline)) as response:
                response_body = _read_response(response, deadline)
        except urllib.error.HTTPError as exc:
            try:
                try:
                    detail = _read_response(exc, deadline).decode(
                        "utf-8", "replace")[:500]
                except (TimeoutError, OSError,
                        http.client.HTTPException) as read_exc:
                    detail = f"response body could not be read: {read_exc}"
            finally:
                exc.close()
            reason = ModelError(f"anthropic HTTP {exc.code}: {detail}")
            if exc.code in (401, 403):
                # A bad key is not a transient condition.
                raise ModelUnavailable(str(reason)) from exc
            if _is_retryable_status(exc.code):
                raise _Retryable(reason, _retry_after(exc)) from exc
            raise reason from exc
        except (urllib.error.URLError, TimeoutError, OSError,
                http.client.HTTPException) as exc:
            raise _Retryable(ModelError(f"anthropic transport error: {exc}"),
                             None) from exc
        try:
            payload = json.loads(response_body)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise _Retryable(ModelError(f"anthropic sent unparsable JSON: {exc}"),
                             None) from exc
        if not isinstance(payload, dict):
            raise ModelError("anthropic response was not a JSON object")
        return payload

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


class _Retryable(Exception):
    """Internal: a failure worth another attempt, with the server's own advice."""

    def __init__(self, reason: Exception, retry_after: float | None) -> None:
        super().__init__(str(reason))
        self.reason = reason
        self.retry_after = retry_after


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0.0:
        raise TimeoutError("anthropic total deadline expired")
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


def _read_response(response: Any, deadline: float) -> bytes:
    chunks: list[bytes] = []
    reader = getattr(response, "read1", None)
    if not callable(reader):
        reader = response.read
    while True:
        _set_response_timeout(response, _remaining(deadline))
        chunk = reader(64 * 1024)
        if time.monotonic() >= deadline:
            raise TimeoutError(
                "anthropic total deadline expired while reading response")
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


def build_model(config, claude_path: str | None, *,
                provider: str | None = None,
                timeout: float | None = None) -> Model:
    """The configured model transport, or `NullModel` when it is switched off.

    `provider` overrides the configured one, so a caller with a setting of its
    own does not have to reach into the config itself.
    """
    name = (provider or config.get("summary.provider") or "claude-cli").lower()
    seconds = float(timeout if timeout is not None
                    else config.get("summary.timeout_seconds", 900))
    if name == "none":
        return NullModel()
    if name == "anthropic-api":
        return AnthropicApiModel(
            config.secret("anthropic_api_key"),
            model=config.get("summary.model", "claude-sonnet-5"),
            timeout=seconds,
            max_retries=int(config.get("summary.max_retries", 3)),
        )
    if name == "claude-cli":
        return ClaudeCliModel(
            claude_path or "", timeout=seconds,
            max_retries=int(config.get("summary.max_retries", 3)))
    raise ModelError(f"unknown model provider: {name}")
