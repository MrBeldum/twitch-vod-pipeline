"""Objective rundown generation, pluggable across engines.

Settled decision: the rundown states what happened and when. It does not rate
moments, nominate clips, or characterise the streamer. That constraint lives in
the prompt below and is the reason this module exists at all rather than being a
one-line subprocess call.

`claude -p` runs against the user's existing subscription and therefore shares its
usage limits, which is exactly why the API provider is a first-class alternative
rather than an afterthought.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Protocol, Sequence

from .config import Config
from .models import Model, ModelError, ModelUnavailable, NullModel, build_model
from .transcript import Word, reading_segments
from .util import LOG, atomic_write_text, fmt_clock

INSTRUCTION = """\
You are given a timestamped transcript of one segment of a Twitch livestream.

Write an objective rundown in Markdown. Report only what was said and done.

Required structure:

## Overview
Two to four sentences stating what this segment consisted of. Factual only.

## Timeline
A bulleted list in chronological order. One bullet per distinct topic or activity,
each beginning with its timestamp range in [HH:MM:SS-HH:MM:SS] form, followed by a
plain statement of what happened. Use the timestamps given in the transcript. Aim
for a bullet every two to five minutes of material; merge trivial adjacent items.

## Topics covered
A flat list of the subjects discussed.

## References
People, organisations, works, games, places and links that were named. Omit the
section if there were none.

Rules, all mandatory:
- Describe; do not evaluate. No "highlight", "great moment", "funny", "notable",
  "interesting", "worth clipping", "viewers will enjoy".
- Do not recommend clips, edits, thumbnails or titles. Do not rank anything.
- Do not characterise the streamer's personality, mood, or intent beyond what was
  explicitly stated.
- Do not speculate about anything off-screen or unstated.
- If a passage is unintelligible or the transcript is garbled, say so plainly
  rather than guessing at it.
- Transcription is automatic and imperfect. Do not treat a single odd word as
  significant.
- Output only the Markdown rundown. No preamble, no sign-off.
"""


class Summarizer(Protocol):
    def summarize(self, transcript: str, header: str) -> str:
        ...


class NullSummarizer:
    def summarize(self, transcript: str, header: str) -> str:
        raise RuntimeError("summaries are disabled")


class ModelSummarizer:
    """The rundown prompt, over whichever transport is configured.

    All the awkward parts -- process timeouts, HTTP retries, `Retry-After`,
    truncated responses -- belong to `models.py`. What is left here is the one
    thing that is actually about rundowns: the instruction, and the header the
    model needs but cannot infer.
    """

    def __init__(self, model: Model, max_tokens: int = 8000) -> None:
        self.model = model
        self.max_tokens = max_tokens

    def summarize(self, transcript: str, header: str) -> str:
        return self.model.ask(f"{INSTRUCTION}\n\n{header}\n", transcript,
                              max_tokens=self.max_tokens)


def build_summarizer(config: Config, claude_path: str | None) -> Summarizer:
    provider = (config.get("summary.provider") or "claude-cli").lower()
    if provider == "none" or not config.get("summary.enabled", True):
        return NullSummarizer()
    try:
        model = build_model(config, claude_path, provider=provider)
    except ModelUnavailable as exc:
        # Preserved as a plain RuntimeError: callers of this function have
        # always treated any failure here as "the rundown cannot be written",
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
        f"Timestamps in the transcript are relative to the start of the broadcast."
    )


def build_model_input(words: Sequence[Word], session_offset: float) -> str:
    """Render structured words as exact, session-relative reading ranges."""
    return "\n".join(
        f"[{fmt_clock(segment.start + session_offset)}-"
        f"{fmt_clock(segment.end + session_offset)}] {segment.text}"
        for segment in reading_segments(words)
    )


def rundown_generation(path: Path) -> str | None:
    """Return the transcript generation persisted in a rundown comment."""
    if not path.is_file():
        return None
    for line in path.read_text(encoding="utf-8").splitlines()[:12]:
        prefix = "Transcript generation: "
        if line.startswith(prefix):
            generation = line[len(prefix):].strip()
            if len(generation) == 16 and all(
                    character in "0123456789abcdef" for character in generation):
                return generation
            return None
    return None


def write_rundown(path: Path, body: str, header: str, generation: str) -> None:
    atomic_write_text(
        path,
        f"<!--\n{header}\nTranscript generation: {generation}\n-->\n\n"
        f"{body.strip()}\n",
    )
    LOG.info("rundown written: %s", path)
