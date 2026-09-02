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

import json
import os
import shutil
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

    Grok is an *agent* runtime, not a completion endpoint, and the 2026-09-02
    examplechannel recording is what proved the difference matters: all five
    chunks produced masters, proxies, transcripts and chat, and every one of the
    fourteen report attempts died with `Max turns reached`. Four compounding
    causes, all of them ours:

    - **The CLI offloads a large prompt to a file.** Above roughly 24 KB of
      `--prompt-file`, Grok does not put the prompt in the conversation. It
      substitutes a stub -- the binary's own strings are `Full request
      offloaded to file` and `full text in the offloaded file` -- and the model
      has to `read_file` its way to the actual request. A two-hour transcript
      is ~104 KB, so *every* report took that path. With `--max-turns 1` the
      single turn was spent on the read and the run was cancelled before an
      answer existed. The failure was not intermittent; it was certain.
    - **`--tools ""` disables nothing.** An empty allowlist reads as "unset",
      so all 26 built-ins stayed live -- `run_terminal_command`,
      `spawn_subagent`, `image_gen` among them -- plus 297 MCP tools imported
      from the Claude Code configuration. A non-empty allowlist is the only
      thing that actually restricts, and the `GROK_CLAUDE_*_ENABLED` switches
      are the only thing that keeps another application's MCP servers out.
    - **`--output-format plain` is not the answer.** It prints every assistant
      message, so stdout is each "I'll start by reading..." narration
      concatenated with the report. A run that had *succeeded* under the old
      code would have published a report with the model's process notes welded
      onto the front. `json` carries the same text but adds `stopReason`, which
      is what lets a genuine max-turns exit be named as one.
    - **"Write `report.md`" read as an instruction to write a file.** It was
      meant as prose describing the deliverable. Under an agent it is a task,
      and the model spent turn after turn announcing the write. That sentence
      now lives here, where it is true, instead of in the shared prompt.

    So this transport stops pretending to be a single-turn Q&A and asks for
    what the runtime is actually good at: the transcript goes on disk under a
    name the model is told, the instruction stays small enough to arrive
    inline, and the report comes back as a file rather than as stdout. Against
    the c000 transcript that had failed fourteen times, this returns a complete
    seven-section report in eight turns.

    `--cwd` is a throwaway directory so Grok does not walk this repository
    looking for a project root. Blank `model` means the CLI default (Grok 4.6
    as of CLI 1.0.13).
    """

    name = "grok-cli"
    label = "grok -p"

    # Read the transcript, write the report. Nothing here reaches the network,
    # spawns an agent, or runs a command. `search_replace` is deliberately
    # absent: `write` creates the report in one call and there is nothing to
    # edit afterwards.
    TOOLS: tuple[str, ...] = ("read_file", "list_dir", "grep", "write")

    TRANSCRIPT_NAME = "transcript.md"
    REPORT_NAME = "report.md"

    # A report shorter than this is a stub or an apology, not a seven-section
    # editor report, and accepting it would publish nonsense over a chunk that
    # could have been re-queued instead.
    MIN_REPORT_CHARS = 400

    DELIVERY = """

--- how to deliver this report ---

The material is in `{transcript}` in your working directory: the transcript
first, then the chat evidence. Read that file in full before you write
anything. Nothing else in the directory is part of the job, and there is
nothing to look up elsewhere.

Write the finished report to `{report}` in the same directory using the `write`
tool. That file must hold the report and nothing else -- no preamble, no notes
on what you are doing, no account of your own process.

When `{report}` is written, reply with exactly: DONE
"""

    def __init__(self, executable: str, timeout: float = 900.0,
                 max_retries: int = 3, model: str = "",
                 max_turns: int = 40) -> None:
        if not executable:
            raise ModelUnavailable(
                "grok executable not found; set tools.grok in config")
        self.executable = executable
        self.timeout = timeout
        self.max_retries = max(1, max_retries)
        # Blank means the CLI default. As of Grok CLI 1.0.13 that is Grok 4.6
        # (reported in usage as grok-4.6-build). `grok-build` is no longer a
        # valid model id -- the CLI rejects it -- so it is not used as a default
        # and schema.RETIRED_MODELS rewrites it to blank on load.
        self.model = (model or "").strip()
        # Reading a two-hour transcript and writing the report took 8 turns on
        # the reference chunk. The budget is generous because the cost of being
        # wrong is asymmetric: an unused turn costs nothing, and one turn too
        # few throws away the whole call along with everything it had read.
        self.max_turns = max(2, int(max_turns))

    def _argv(self, prompt_file: str, cwd: str) -> list[str]:
        argv = [
            self.executable,
            "--prompt-file", prompt_file,
            # Carries `stopReason` beside the text. `plain` does not, and
            # cannot distinguish a finished answer from an abandoned one.
            "--output-format", "json",
            "--cwd", cwd,
            "--max-turns", str(self.max_turns),
            "--no-subagents",
            "--disable-web-search",
            "--always-approve",
            # Must be non-empty: an empty value reads as "no restriction".
            "--tools", ",".join(self.TOOLS),
        ]
        if self.model:
            argv += ["-m", self.model]
        return argv

    def _env(self) -> dict[str, str]:
        """A session carrying only what a report needs.

        The imported-configuration switches are load-bearing rather than tidy:
        without them Grok adopts the Claude Code configuration found on this
        machine, which on the reference install meant 297 Premiere Pro and
        After Effects MCP tools in the context of every report -- tools that
        edit a real project, offered to a model whose job is to write Markdown.
        """
        env = os.environ.copy()
        env["GROK_MEMORY"] = "0"
        env["GROK_DISABLE_AUTOUPDATER"] = "1"
        for name in ("GROK_CLAUDE_MCPS_ENABLED", "GROK_CLAUDE_SKILLS_ENABLED",
                     "GROK_CLAUDE_AGENTS_ENABLED", "GROK_CLAUDE_HOOKS_ENABLED",
                     "GROK_CLAUDE_RULES_ENABLED", "GROK_CODEX_MCPS_ENABLED",
                     "GROK_CODEX_SKILLS_ENABLED"):
            env[name] = "0"
        return env

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
        transcript_path = work / self.TRANSCRIPT_NAME
        report_path = work / self.REPORT_NAME
        try:
            transcript_path.write_text(user, encoding="utf-8")
            prompt_path.write_text(
                system.rstrip() + "\n" + self.DELIVERY.format(
                    transcript=self.TRANSCRIPT_NAME, report=self.REPORT_NAME),
                encoding="utf-8")
            code, stdout, stderr = _run_cli_raw(
                self._argv(str(prompt_path), str(work)),
                stdin_bytes=None,
                call_deadline=call_deadline,
                label=self.label,
                env=self._env(),
            )
            return self._answer(report_path, code, stdout, stderr)
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def _answer(self, report_path: Path, code: int, stdout: str,
                stderr: str) -> str:
        """The report, from the file if it is there and from stdout if not.

        The file is preferred even when the CLI exited non-zero. A model that
        wrote the report and then ran out of turns acknowledging it has already
        done the expensive part, and discarding that to honour an exit code
        would throw away a finished report over a missing "DONE".
        """
        report = self._read_report(report_path)
        if report:
            return report
        salvaged = self._salvage(stdout)
        if salvaged and code == 0:
            return salvaged
        raise ModelError(f"{self.label} failed ({code}): "
                         f"{self._detail(stdout, stderr)}")

    def _read_report(self, path: Path) -> str:
        try:
            text = path.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            return ""
        return text if len(text) >= self.MIN_REPORT_CHARS else ""

    def _salvage(self, stdout: str) -> str:
        """The report out of `text`, for a model that answered inline anyway.

        `text` is every assistant message run together, so any narration sits
        in front of the report with no separator. The report's own first line
        is the one reliable boundary: `summarize.INSTRUCTION` requires it to
        open on `## Overview`, and a heading is not something the narration
        produces. Anything before the first `## ` is process notes.
        """
        text = (self._payload(stdout).get("text") or "").strip()
        if not text:
            return ""
        marker = text.find("## ")
        if marker < 0:
            return ""
        report = text[marker:].strip()
        return report if len(report) >= self.MIN_REPORT_CHARS else ""

    def _detail(self, stdout: str, stderr: str) -> str:
        """Say what actually stopped the run, in the terms the CLI reports."""
        payload = self._payload(stdout)
        reason = str(payload.get("stopReason") or "").strip()
        turns = payload.get("num_turns")
        said = stderr.strip() or "no reason on stderr"
        if reason and reason != "end_turn":
            used = f" after {turns} turn(s) of {self.max_turns}" if turns else ""
            return (f"{said} (stopReason {reason}{used}; "
                    f"no {self.REPORT_NAME} was written)")[-800:]
        return f"{said} (no {self.REPORT_NAME} was written)"[-800:]

    @staticmethod
    def _payload(stdout: str) -> dict:
        """The JSON object `--output-format json` writes, even on a non-zero
        exit. A stdout that will not parse is a crash before the run rather
        than a different shape, so an unreadable payload is simply empty."""
        try:
            payload = json.loads(stdout)
        except (ValueError, TypeError):
            return {}
        return payload if isinstance(payload, dict) else {}


def _run_cli_raw(argv: list[str], *, stdin_bytes: bytes | None,
                 call_deadline: float, label: str,
                 env: dict[str, str] | None = None) -> tuple[int, str, str]:
    """Run the CLI to completion and hand back exactly what it said.

    Split out of `_run_cli` for `GrokCliModel`, which has to read stdout on a
    *non-zero* exit: `--output-format json` writes its object either way, and
    the `stopReason` in it is the difference between "the model ran out of
    turns" and the uninformative `grok -p failed (1):` that this project has
    twice had to diagnose from. A spawn failure, a deadline and a timeout still
    raise, because in those cases the CLI never got to say anything.
    """
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

    return (proc.returncode,
            stdout.decode("utf-8", "replace").strip(),
            stderr.decode("utf-8", "replace").strip())


def _run_cli(argv: list[str], *, stdin_bytes: bytes | None,
             call_deadline: float, label: str,
             env: dict[str, str] | None = None) -> str:
    code, text, stderr = _run_cli_raw(
        argv, stdin_bytes=stdin_bytes, call_deadline=call_deadline,
        label=label, env=env)
    if code != 0 or not text:
        # Report whatever the CLI actually said. Taking stderr alone
        # produced a bare `claude -p failed (1):` with nothing after the
        # colon whenever it wrote its reason to stdout, which is what a
        # `--print` mode CLI naturally does -- so the one message the user
        # had to diagnose from carried no information at all.
        detail = (stderr or text or "no output on stdout or stderr")[-800:]
        if code == 0:
            raise ModelError(
                f"{label} exited 0 without writing an answer: {detail}")
        raise ModelError(f"{label} failed ({code}): {detail}")
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
                            max_retries=retries, model=model,
                            max_turns=int(config.get("summary.max_turns", 40)))
    raise ModelError(f"unknown model provider: {name}")
