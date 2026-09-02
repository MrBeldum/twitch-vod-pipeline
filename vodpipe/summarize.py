"""Editor-facing report generation, pluggable across Claude Code and Grok Build.

The report is written for an editor who turns Twitch VODs into YouTube videos
(long-form and shorts). It still has to be *true* -- timestamps from the
transcript, chat claims from the chat -- but it is allowed to nominate, rank,
and recommend. The previous "objective rundown only, no clip recommendations"
constraint was withdrawn for this deliverable.

`rundown.md` is the old name. This module writes `report.md` and retires the
legacy file on the next successful (or empty) write so a chunk folder never
holds two answers that could disagree.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Protocol, Sequence

from .config import Config
from .models import Model, ModelError, ModelUnavailable, NullModel, build_model
from .transcript import Word, reading_segments
from .util import LOG, atomic_write_text, fmt_clock

REPORT_NAME = "report.md"
LEGACY_REPORT_NAME = "rundown.md"

INSTRUCTION = """\
You are a senior editor who cuts Twitch VODs into YouTube videos for a living —
both long-form (8–20 minutes, sometimes a full recap) and Shorts (15–60 seconds).
You are given:

1. A timestamped transcript of one segment of a broadcast (source of truth for
   what was SAID and when).
2. Optional chat analysis of the same segment: computed moments (laughter, hype,
   copypasta, clip-calls, shock) with sample messages. Chat is evidence of what
   LANDED with the audience, not a substitute for the transcript.

Write the report in Markdown. Be specific. Use the timestamps you were given.
Sound like an editor talking to another editor, not like a recap bot and not
like marketing copy.

Required structure:

## Overview
Three to six sentences: what this segment actually is, the energy, and whether
it is a YouTube piece, a Shorts mine, both, or skippable.

## Timeline
A chronological bullet list. One bullet per distinct topic, bit, game-state
change or activity. Each bullet starts with `[HH:MM:SS-HH:MM:SS]` and states
what happened. Aim for a bullet every two to five minutes; merge trivia; never
invent a beat the transcript does not support.

## Best moments (long-form)
The stretches you would actually put in a YouTube video. For each:
- `[HH:MM:SS-HH:MM:SS]`
- what happens (from the transcript)
- why it works on YouTube (setup → payoff, or a reaction that reads on camera)
- chat evidence if any (laugh spam, copypasta, clip calls). If chat is quiet,
  say so — silence is information, not a veto.
Do not nominate a chat spike that has no matching content in the transcript
(raids, sub trains, and emote dumps with nothing on stream are not clips).
If nothing here is usable, say so in one line.

## Shorts candidates
15–60s cuts with a hook in the first two seconds. For each: timestamp, the
hook, the payoff, and a working on-screen caption. Skip this section if none
survive that test.

## What to skip
Dead air, repeated bits, stalled games, tangents that will not hold a viewer
who did not watch live. Timestamps.

## Chat notes
Only if chat data was provided. What the chat tells you that the transcript
does not: in-jokes, a catchphrase that hit, a controversy, a raid. Do not
restate the moment list.

## Titles, chapters, thumbnails
Three title options (YouTube, not Twitch). Chapter marks if the segment can
carry them. One thumbnail direction (the frame and the text on it). Skip the
section if the segment is not a video.

Rules:
- Timestamps are broadcast-relative, in [HH:MM:SS] or [HH:MM:SS-HH:MM:SS].
- Do not invent quotes. Paraphrase is fine; a quote must appear in the transcript.
- Transcription is automatic and imperfect. One odd word is not a bit.
- If a passage is garbled, say so rather than guessing.
- Output only the Markdown report. No preamble, no sign-off.
- The report begins with the `## Overview` heading. Nothing precedes it.
"""


class Summarizer(Protocol):
    def summarize(self, transcript: str, header: str) -> str:
        ...


class NullSummarizer:
    def summarize(self, transcript: str, header: str) -> str:
        raise RuntimeError("summaries are disabled")


class ModelSummarizer:
    """The report prompt, over whichever transport is configured.

    Timeouts, retries and CLI argv belong to `models.py`. What is left here
    is the instruction, the header, and the optional chat evidence block.
    """

    def __init__(self, model: Model, max_tokens: int = 8000) -> None:
        self.model = model
        self.max_tokens = max_tokens

    def summarize(self, transcript: str, header: str) -> str:
        return self.model.ask(f"{INSTRUCTION}\n\n{header}\n", transcript,
                              max_tokens=self.max_tokens)


def build_summarizer(config: Config, claude_path: str | None,
                     grok_path: str | None = None) -> Summarizer:
    provider = (config.get("summary.provider") or "claude-cli").lower()
    if provider == "none" or not config.get("summary.enabled", True):
        return NullSummarizer()
    try:
        model = build_model(config, claude_path, grok_path=grok_path,
                            provider=provider)
    except ModelUnavailable as exc:
        # Preserved as a plain RuntimeError: callers of this function have
        # always treated any failure here as "the report cannot be written",
        # and `_summarize_inner` records it against the summary artifact.
        raise RuntimeError(str(exc)) from exc
    except ModelError as exc:
        raise RuntimeError(str(exc)) from exc
    if isinstance(model, NullModel):
        return NullSummarizer()
    return ModelSummarizer(
        model, max_tokens=int(config.get("summary.max_tokens", 8000)))


def build_header(channel: str, session_id: str, chunk_label: str,
                 session_offset: float, duration: float,
                 started_at: float) -> str:
    """Context the model needs but cannot infer from the transcript body."""
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(started_at))
    return (
        f"Channel: {channel}\n"
        f"Broadcast started: {when}\n"
        f"Segment: {chunk_label}, covering "
        f"{fmt_clock(session_offset)}-{fmt_clock(session_offset + duration)} "
        f"of the broadcast.\n"
        f"Timestamps in the transcript are relative to the start of the broadcast.\n"
        f"You are writing for an editor cutting this into YouTube videos."
    )


def build_model_input(words: Sequence[Word], session_offset: float) -> str:
    """Render structured words as exact, session-relative reading ranges."""
    return "\n".join(
        f"[{fmt_clock(segment.start + session_offset)}-"
        f"{fmt_clock(segment.end + session_offset)}] {segment.text}"
        for segment in reading_segments(words)
    )


def build_report_user(transcript: str, *, chat_notes: str = "",
                      moments_block: str = "",
                      message_count: int | None = None) -> str:
    """The user turn: transcript first, then chat evidence if we have it."""
    parts = ["## Transcript", "", transcript.rstrip() or "(no speech transcribed)"]
    if moments_block or chat_notes or message_count is not None:
        parts += ["", "## Chat"]
        if message_count is not None:
            parts.append(f"{message_count} messages in this segment.")
        if moments_block:
            parts += ["", "Computed moments (content-aware, not just rate):",
                      moments_block]
        if chat_notes:
            parts += ["", chat_notes]
        else:
            if message_count == 0:
                parts.append("Chat was captured and was empty for this segment.")
    else:
        parts += [
            "",
            "## Chat",
            "",
            "No chat was captured for this segment. Do not invent audience "
            "reaction. Write the report from the transcript alone and say that "
            "chat was unavailable.",
        ]
    return "\n".join(parts)


def report_path(directory: Path) -> Path:
    """The file that currently describes this chunk, preferring `report.md`."""
    current = directory / REPORT_NAME
    if current.is_file():
        return current
    return directory / LEGACY_REPORT_NAME


def report_generation(directory: Path) -> str | None:
    """Return the transcript generation persisted in a report HTML comment."""
    path = report_path(directory)
    if not path.is_file():
        return None
    return generation_from_report(path)


def generation_from_report(path: Path) -> str | None:
    if not path.is_file():
        return None
    try:
        lines = path.read_text(encoding="utf-8").splitlines()[:16]
    except OSError:
        return None
    for line in lines:
        prefix = "Transcript generation: "
        if line.startswith(prefix):
            generation = line[len(prefix):].strip()
            if len(generation) == 16 and all(
                    character in "0123456789abcdef" for character in generation):
                return generation
            return None
    return None


def write_report(directory: Path, body: str, header: str, generation: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / REPORT_NAME
    atomic_write_text(
        path,
        f"<!--\n{header}\nTranscript generation: {generation}\n-->\n\n"
        f"{body.strip()}\n",
    )
    legacy = directory / LEGACY_REPORT_NAME
    if legacy.exists() and legacy != path:
        try:
            legacy.unlink()
        except OSError as exc:
            LOG.warning("could not remove legacy rundown.md beside %s: %s",
                        path, exc)
    LOG.info("report written: %s", path)
    return path


def retire_report(directory: Path, why: str) -> list[Path]:
    """Delete report.md and any leftover rundown.md."""
    removed: list[Path] = []
    for name in (REPORT_NAME, LEGACY_REPORT_NAME):
        path = directory / name
        if not path.exists():
            continue
        path.unlink()
        removed.append(path)
        LOG.info("removed %s: %s", path, why)
    return removed


# Names older tests and call sites still use. They operate on a *file* path
# for compatibility: generation is read from that file; writes always land on
# report.md in the same folder and retire rundown.md beside it.
def rundown_generation(path: Path) -> str | None:
    if path.is_dir():
        return report_generation(path)
    return generation_from_report(path)


def write_rundown(path: Path, body: str, header: str, generation: str) -> None:
    directory = path if path.is_dir() else path.parent
    write_report(directory, body, header, generation)
