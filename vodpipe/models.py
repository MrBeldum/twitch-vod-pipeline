"""The report writer's transports: headless `claude -p` and headless `grok -p`.

Kept separate from `summarize.py`, which is only a prompt, because the transport
concerns are the substantial half: a subscription CLI can hang, can be rate
limited, and can exit non-zero for reasons that are not classifiable from out
here.

Both engines spend a local subscription CLI, not a paid HTTP API. The paid-API
engines (Anthropic, Kimi, DeepSeek, OpenAI-compatible) were added 2026-08-18
and removed 2026-08-19 after they failed on the ordinary case: Kimi refused one
report with `max organization concurrency: 1` and the next as `high risk`
content, having been handed a Twitch transcript. A fallback that fails on the
ordinary case is not a fallback. Do not re-add a paid API without reading that
history. `grok-cli` is a second *subscription CLI*, the same shape as
`claude-cli`, which is the thing that was actually earning its keep.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Protocol

from .util import LOG, popen


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


# ------------------------------------------------------------------- the engine


class ClaudeCliModel:
    """Headless `claude -p` against the user's existing subscription.

    The user's text goes on stdin, never argv: a two-hour transcript is far past
    the Windows command-line length limit.
    """

    name = "claude-cli"
    label = "claude -p"

    def __init__(self, executable: str, timeout: float = 900.0,
                 max_retries: int = 3, model: str = "") -> None:
        if not executable:
            raise ModelUnavailable(
                "claude executable not found; set tools.claude in config")
        self.executable = executable
        self.timeout = timeout
        self.max_retries = max(1, max_retries)
        # Blank is the normal setting and means "whatever the subscription
        # defaults to". The CLI knows which models the subscription covers and
        # this repository does not, so naming one is an override, not a default.
        self.model = (model or "").strip()

    def _argv(self, system: str) -> list[str]:
        argv = [self.executable,
                "--safe-mode",
                "--tools", "",
                "--disable-slash-commands",
                "--no-session-persistence",
                "--system-prompt", system,
                "--print",
                "--output-format", "text"]
        if self.model:
            argv += ["--model", self.model]
        return argv

    # -- transport ---------------------------------------------------------

    def ask(self, system: str, user: str, *, max_tokens: int = 8000,
            deadline: float | None = None) -> str:
        """Ask, retrying a transient failure within one shared deadline.

        AUD3-003: this had no retry at all, while an API transport that used to
        live next door retried four times with backoff. That asymmetry was
        backwards: a subscription CLI is the one sharing the user's usage limits,
        which this project has always known to be the likeliest transient (see
        CLAUDE.md, "a heavy recording day could bump into them"). One blip
        permanently marked a rundown failed.

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
        return _run_cli(
            self._argv(system),
            stdin_bytes=user.encode("utf-8", "replace"),
            call_deadline=call_deadline,
            label=self.label,
        )


class GrokCliModel:
    """Headless `grok -p` against the user's Grok subscription.

    Grok does not read the prompt from stdin -- `--prompt-file` is the only
    way to hand it a two-hour transcript without hitting the Windows
    command-line length limit. `--cwd` is a throwaway directory so Grok does
    not walk this repository looking for a project root. Tools are disabled:
    a report is an answer, not an agent turn. Blank `model` means the CLI
    default (Grok 4.6 as of CLI 1.0.5).
    """

    name = "grok-cli"
    label = "grok -p"

    def __init__(self, executable: str, timeout: float = 900.0,
                 max_retries: int = 3, model: str = "") -> None:
        if not executable:
            raise ModelUnavailable(
                "grok executable not found; set tools.grok in config")
        self.executable = executable
        self.timeout = timeout
        self.max_retries = max(1, max_retries)
        # Blank means the CLI default. As of Grok CLI 1.0.5 that is Grok 4.6
        # (reported in usage as grok-4.6-build). `grok-build` is no longer a
        # valid model id — the CLI rejects it — so it is not used as a default
        # and schema.RETIRED_MODELS rewrites it to blank on load.
        self.model = (model or "").strip()

    def _argv(self, system: str, prompt_file: str, cwd: str) -> list[str]:
        argv = [
            self.executable,
            "--prompt-file", prompt_file,
            "--output-format", "plain",
            "--cwd", cwd,
            "--max-turns", "1",
            "--no-subagents",
            "--disable-web-search",
            "--always-approve",
            "--no-auto-update",
            "--tools", "",
        ]
        if self.model:
            argv += ["-m", self.model]
        # Keep the instruction off argv when it would blow the Windows limit
        # together with the rest of the flags; fold it into the prompt file
        # instead. `_ask_once` decides which way we went.
        if system:
            argv += ["--system-prompt-override", system]
        return argv

    def ask(self, system: str, user: str, *, max_tokens: int = 8000,
            deadline: float | None = None) -> str:
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
                if remaining <= delay:
                    break
                LOG.warning("%s attempt %d/%d failed (%s); retrying in %.0fs",
                            self.label, attempt + 1, self.max_retries, exc, delay)
                time.sleep(delay)
        raise last or ModelError(f"{self.label} failed")

    def _ask_once(self, system: str, user: str, call_deadline: float) -> str:
        work = Path(tempfile.mkdtemp(prefix="vodpipe-grok-"))
        prompt_path = work / "prompt.txt"
        argv_system = system
        body = user
        # `CreateProcess` on Windows refuses a command line longer than 32767
        # characters; the practical console limit is 8191. Fold a long
        # instruction into the file rather than fail the spawn.
        trial = self._argv(system, str(prompt_path), str(work))
        encoded = " ".join(str(part) for part in trial)
        if len(encoded) > 7500:
            argv_system = ""
            body = f"{system.rstrip()}\n\n{user}"
        prompt_path.write_text(body, encoding="utf-8")
        env = os.environ.copy()
        env["GROK_MEMORY"] = "0"
        env["GROK_DISABLE_AUTOUPDATER"] = "1"
        try:
            return _run_cli(
                self._argv(argv_system, str(prompt_path), str(work)),
                stdin_bytes=None,
                call_deadline=call_deadline,
                label=self.label,
                env=env,
            )
        finally:
            try:
                prompt_path.unlink(missing_ok=True)
            except OSError:
                pass
            try:
                work.rmdir()
            except OSError:
                pass


def _run_cli(argv: list[str], *, stdin_bytes: bytes | None,
             call_deadline: float, label: str,
             env: dict[str, str] | None = None) -> str:
    started = time.monotonic()
    budget = max(0.0, call_deadline - started)
    if budget <= 0.0:
        raise ModelError(f"{label} total deadline expired")
    popen_kwargs: dict = dict(
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if env is not None:
        popen_kwargs["env"] = env
    proc = popen(argv, **popen_kwargs)
    timeout = call_deadline - time.monotonic()
    if timeout <= 0.0:
        proc.kill()
        _reap_killed_process(proc)
        raise ModelError(f"{label} total deadline expired")
    try:
        stdout, stderr = proc.communicate(stdin_bytes, timeout=timeout)
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
            _reap_killed_process(proc)
        raise ModelError(f"{label} timed out after {budget:.0f}s")

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
                f"{label} exited 0 without writing an answer: {detail}")
        raise ModelError(
            f"{label} failed ({proc.returncode}): {detail}")
    return text


def _reap_killed_process(proc: subprocess.Popen) -> None:
    def reap() -> None:
        try:
            proc.communicate(timeout=30)
        except subprocess.TimeoutExpired:       # pragma: no cover - kill ignored
            LOG.warning("a model CLI did not exit after being killed")
        except Exception:                       # pragma: no cover - already dying
            pass

    threading.Thread(target=reap, name="model-reaper", daemon=True).start()


# ----------------------------------------------------------------- the registry

# Every engine name, in the order the dashboard offers them. The schema builds
# its choice list from this, so the set of engines lives in exactly one place.
# Adding a name back here needs nothing else; *removing* one needs
# `schema.RETIRED_PATHS` the same as any other setting, because
# `summary.provider` is validated as a closed choice and an installed
# config.json naming a dropped engine would stop the application starting.
PROVIDER_NAMES: tuple[str, ...] = (
    "claude-cli",
    "grok-cli",
    "none",
)


def build_model(config, claude_path: str | None, *,
                grok_path: str | None = None,
                provider: str | None = None,
                timeout: float | None = None) -> Model:
    """The configured model transport, or `NullModel` when it is switched off.

    `provider` overrides the configured one, so a caller with a setting of its
    own does not have to reach into the config itself.
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
                              max_retries=retries, model=model)
    if name == "grok-cli":
        return GrokCliModel(grok_path or "", timeout=seconds,
                            max_retries=retries, model=model)
    raise ModelError(f"unknown model provider: {name}")
