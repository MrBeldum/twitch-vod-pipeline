"""Deterministic chat-moment analysis for the editor report.

Speed of chat is a signal, not the signal. A raid dump looks like a spike and
is usually not a clip; a quiet punchline that the chat answers with KEKW is.
This module classifies messages by *content* -- laugh emotes and phrases, hype,
copypasta, explicit clip calls, bits, raids -- then scores sliding windows so
the report engine is handed evidence rather than a raw firehose.

The scores are not the edit. They nominate ranges an editor would actually
scrub, with samples of what was said. The model still has to read the
transcript to decide whether the moment is usable on YouTube.
"""

from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass
from typing import Any, Iterable

from .chat import ChatMessage
from .util import atomic_write_json, fmt_clock

WINDOW_SECONDS = 10.0
HOP_SECONDS = 2.0
MIN_MESSAGES = 5
MAX_MOMENTS = 16

# Tokens (case-folded) that mean the chat is laughing, not just talking.
# Third-party emotes never appear in Twitch's `emotes` tag, so the body text
# is the only place KEKW / ICANT / 7TV names show up.
LAUGH_TOKENS = frozenset({
    "kekw", "kek", "lul", "lulw", "omegalul", "omegallul", "lol", "lmao",
    "lmfao", "rofl", "lel", "ikru", "icant", "joy", "joycam", "xdd", "xd",
    "haha", "hahaha", "hahahaha", "jajaja", "pepelaugh",
    "laugh", "crying", "dead", "ripbozo", "gottem", "owned",
    "ㅋㅋ", "ㅋㅋㅋ", "ㅋㅋㅋㅋ", "wwwww", "wwwwww",
})
LAUGH_EMOJIS = frozenset({"😂", "🤣", "💀", "😭", "😹"})
LAUGH_RE = re.compile(
    r"(?:ha){2,}|lmao+|lol+|kekw?|lulw?|omeg(?:a)?lul|x+d+|ㅋ{2,}|w{4,}",
    re.IGNORECASE,
)

HYPE_TOKENS = frozenset({
    "pog", "pogchamp", "poggers", "pogu", "poggies", "letsgo", "letsgodude",
    "nodders", "hyperclap", "clap", "ez", "gg", "fire", "sheesh", "bussin",
    "goat", "huge", "insane", "crazy", "holy",
})
HYPE_EMOJIS = frozenset({"🔥", "🚀", "💪", "👑", "⚡"})

CLIP_RE = re.compile(
    r"\b(?:clip\s*it|clip\s*that|clip\s*this|clipthat|clipit|!clip|"
    r"someone\s+clip|need(?:s)?\s+a\s+clip|that(?:'s| is)\s+a\s+clip)\b",
    re.IGNORECASE,
)

SHOCK_TOKENS = frozenset({
    "noway", "naw", "bruh", "bro", "dude", "what", "wut", "wutface",
    "holyshit", "omg", "omfg", "wait", "pause", "noshot",
})

RAID_IDS = frozenset({"raid", "unraid"})
SUB_IDS = frozenset({
    "sub", "resub", "subgift", "submysterygift", "giftpaidupgrade",
    "primepaidupgrade", "anongiftpaidupgrade",
})

_TOKEN_RE = re.compile(r"[^\W_]+", re.UNICODE)
_PUNCT_RE = re.compile(r"[^\w\s]+", re.UNICODE)


@dataclass(frozen=True)
class Moment:
    start: float
    end: float
    kind: str
    score: float
    messages: int
    unique_users: int
    laugh_fraction: float
    hype_fraction: float
    clip_calls: int
    copypasta: str
    bits: int
    samples: tuple[str, ...]
    reason: str
    session_offset: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["t"] = f"{fmt_clock(self.start + self.session_offset)}-" \
                    f"{fmt_clock(self.end + self.session_offset)}"
        data["samples"] = list(self.samples)
        return data


def analyse_chat(messages: Iterable[ChatMessage], *,
                 duration: float, session_offset: float = 0.0,
                 window: float = WINDOW_SECONDS,
                 hop: float = HOP_SECONDS) -> list[Moment]:
    """Ranked, de-overlapped moments inside one chunk.

    `messages` may be session-relative or chunk-relative; `session_offset` is
    added only when rendering clocks for the report, not when scoring. Pass
    chunk-relative offsets (as `write_chat_exports` stores them).
    """
    items = [item for item in messages if item.offset >= -0.05]
    if not items or duration <= 0:
        return []
    window = max(4.0, float(window))
    hop = max(1.0, float(hop))
    duration = max(duration, max(item.offset for item in items) + 0.1)

    classified = [_classify(item) for item in items]
    baseline = len(items) / duration if duration else 0.0

    candidates: list[Moment] = []
    # Windows advance monotonically, so sweep an offset-sorted index with two
    # pointers instead of rescanning every message for every window. At two
    # hours and a 2s hop that is 3600 windows; the rescan cost ~19s of pure
    # Python on a 60k-message chunk and grows with chat volume.
    #
    # `order` is sorted by offset only, so the window's members are a
    # contiguous slice of it; re-sorting that slice by original index hands
    # `_score_window` the same bucket, in the same order, that the rescan
    # produced. That ordering is load-bearing -- `_samples` dedupes on first
    # occurrence and its weight sort is stable, and the copypasta tally
    # breaks ties on insertion order -- so it is preserved exactly rather
    # than left to whatever order the sweep happens to visit.
    order = sorted(range(len(classified)), key=lambda i: classified[i][0].offset)
    offsets = [classified[i][0].offset for i in order]
    lo = 0
    hi = 0
    start = 0.0
    while start < duration:
        end = min(duration, start + window)
        while lo < len(order) and offsets[lo] < start:
            lo += 1
        if hi < lo:
            hi = lo
        while hi < len(order) and offsets[hi] < end:
            hi += 1
        bucket = [classified[i] for i in sorted(order[lo:hi])]
        moment = _score_window(bucket, start, end, baseline,
                               session_offset=session_offset)
        if moment is not None:
            candidates.append(moment)
        start += hop
        if end >= duration:
            break

    return _suppress(candidates)[:MAX_MOMENTS]


def render_moments(moments: Iterable[Moment]) -> str:
    """Compact evidence block for the report engine, not the report itself."""
    rows = list(moments)
    if not rows:
        return "(no chat-detected moments in this segment)"
    lines = []
    for moment in rows:
        clock = f"{fmt_clock(moment.start + moment.session_offset)}-" \
                f"{fmt_clock(moment.end + moment.session_offset)}"
        lines.append(
            f"[{clock}] {moment.kind.upper()} — {moment.reason} "
            f"({moment.messages} msgs, {moment.unique_users} users, "
            f"score {moment.score:.1f})"
        )
        for sample in moment.samples[:4]:
            lines.append(f"    {sample}")
        if moment.copypasta:
            lines.append(f"    copypasta: {moment.copypasta!r}")
    return "\n".join(lines)


def write_moments(path, moments: Iterable[Moment], *,
                  channel: str, chunk: str, duration: float) -> None:
    rows = [item.to_dict() for item in moments]
    atomic_write_json(path, {
        "version": 1,
        "channel": channel,
        "chunk": chunk,
        "duration": duration,
        "moments": rows,
    })


@dataclass
class _Flags:
    laugh: bool = False
    hype: bool = False
    shock: bool = False
    clip: bool = False
    raid: bool = False
    sub: bool = False
    bits: int = 0
    normalised: str = ""
    tokens: tuple[str, ...] = ()


def _classify(message: ChatMessage) -> tuple[ChatMessage, _Flags]:
    text = message.text or ""
    folded = text.casefold()
    compact = re.sub(r"[\s_]+", "", folded)
    tokens = tuple(_TOKEN_RE.findall(folded))
    emote_tokens = tuple(name.casefold() for name in message.emotes)
    all_tokens = tokens + emote_tokens

    flags = _Flags(
        bits=max(0, int(message.bits)),
        normalised=_normalise_copy(text),
        tokens=all_tokens,
    )
    if any(token in LAUGH_TOKENS for token in all_tokens) \
            or any(ch in text for ch in LAUGH_EMOJIS) \
            or LAUGH_RE.search(folded):
        flags.laugh = True
    if any(token in HYPE_TOKENS for token in all_tokens) \
            or compact in {"w", "ww", "www"} \
            or any(ch in text for ch in HYPE_EMOJIS):
        flags.hype = True
    if any(token in SHOCK_TOKENS for token in all_tokens):
        flags.shock = True
    if CLIP_RE.search(text):
        flags.clip = True
    kind = (message.kind or "").lower()
    # USERNOTICE kinds we stored as text; raid/sub also travel as kind=system.
    if kind == "system" or "raid" in folded[:40]:
        flags.raid = True
    if kind == "usernotice" and not flags.raid:
        flags.sub = True
    return message, flags


def _normalise_copy(text: str) -> str:
    folded = _PUNCT_RE.sub("", (text or "").casefold())
    return " ".join(folded.split())


def _score_window(bucket: list[tuple[ChatMessage, _Flags]],
                  start: float, end: float, baseline: float, *,
                  session_offset: float) -> Moment | None:
    n = len(bucket)
    width = max(0.5, end - start)
    if n < MIN_MESSAGES and not any(flags.clip or flags.raid for _, flags in bucket):
        return None

    users = {item.user.casefold() for item, _ in bucket if item.user}
    laughs = sum(1 for _, flags in bucket if flags.laugh)
    hypes = sum(1 for _, flags in bucket if flags.hype)
    shocks = sum(1 for _, flags in bucket if flags.shock)
    clips = sum(1 for _, flags in bucket if flags.clip)
    raids = sum(1 for _, flags in bucket if flags.raid)
    subs = sum(1 for _, flags in bucket if flags.sub)
    bits = sum(flags.bits for _, flags in bucket)

    counts: dict[str, int] = {}
    for _, flags in bucket:
        if flags.normalised:
            counts[flags.normalised] = counts.get(flags.normalised, 0) + 1
    copypasta = ""
    copypasta_n = 0
    if counts:
        copypasta, copypasta_n = max(counts.items(), key=lambda item: item[1])
        if copypasta_n < 4 or copypasta_n / n < 0.35 or len(copypasta) < 6:
            copypasta, copypasta_n = "", 0

    rate = n / width
    z_rate = 0.0
    if baseline > 0:
        z_rate = (rate - baseline) / max(0.15, math.sqrt(baseline))

    laugh_frac = laughs / n if n else 0.0
    hype_frac = hypes / n if n else 0.0
    unique_frac = (len(users) / n) if n else 0.0

    # A raid is a spike of joins, not a clip. Penalise windows whose only
    # content is a raid dump or a sub train with no laugh/hype/clip.
    raid_penalty = 4.0 if raids and laugh_frac < 0.15 and clips == 0 else 0.0

    score = (
        1.4 * max(0.0, z_rate)
        + 4.5 * laugh_frac * math.sqrt(n)
        + 3.0 * hype_frac * math.sqrt(n)
        + 1.6 * (shocks / n) * math.sqrt(n)
        + 5.5 * clips
        + 2.8 * (copypasta_n / n if n else 0.0) * math.sqrt(n)
        + 0.4 * math.log1p(bits)
        + 0.6 * subs
        + 0.8 * unique_frac * math.sqrt(n)
        - raid_penalty
    )
    if score < 2.4 and clips == 0:
        return None

    kind, reason = _label(
        laugh_frac=laugh_frac, hype_frac=hype_frac, clips=clips,
        copypasta_n=copypasta_n, n=n, shocks=shocks, raids=raids,
        z_rate=z_rate, bits=bits,
    )
    samples = _samples(bucket)
    return Moment(
        start=start,
        end=end,
        kind=kind,
        score=round(score, 3),
        messages=n,
        unique_users=len(users),
        laugh_fraction=round(laugh_frac, 3),
        hype_fraction=round(hype_frac, 3),
        clip_calls=clips,
        copypasta=copypasta,
        bits=bits,
        samples=samples,
        reason=reason,
        session_offset=session_offset,
    )


def _label(*, laugh_frac: float, hype_frac: float, clips: int, copypasta_n: int,
           n: int, shocks: int, raids: int, z_rate: float, bits: int
           ) -> tuple[str, str]:
    if clips:
        return "clip-call", f"chat is calling for a clip ({clips}x)"
    if copypasta_n >= 4 and copypasta_n / n >= 0.35:
        return "copypasta", f"repeated line ×{copypasta_n}"
    if laugh_frac >= 0.28:
        pct = int(laugh_frac * 100)
        return "laughter", f"{pct}% laugh emotes/phrases"
    if hype_frac >= 0.28:
        pct = int(hype_frac * 100)
        return "hype", f"{pct}% hype emotes/phrases"
    if shocks / n >= 0.25 if n else False:
        return "shock", "chat in disbelief"
    if bits >= 100:
        return "bits", f"{bits} bits in the window"
    if raids:
        return "raid", "raid / incoming dump"
    if z_rate >= 2.5:
        return "spike", "chat rate well above the chunk baseline"
    return "reaction", "elevated, mixed reaction"


def _samples(bucket: list[tuple[ChatMessage, _Flags]]) -> tuple[str, ...]:
    """A few distinct lines: prefer clip calls, then laughs, then longest."""
    scored: list[tuple[int, str]] = []
    seen: set[str] = set()
    for item, flags in bucket:
        text = (item.text or "").strip()
        if not text:
            continue
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        weight = 0
        if flags.clip:
            weight += 50
        if flags.laugh:
            weight += 20
        if flags.hype:
            weight += 12
        if flags.shock:
            weight += 8
        if len(text) > 12:
            weight += 4
        scored.append((weight, f"{item.user}: {text[:160]}"))
    scored.sort(key=lambda row: -row[0])
    return tuple(text for _, text in scored[:5])


def _suppress(moments: list[Moment]) -> list[Moment]:
    """Keep the highest-scoring of overlapping windows (IoU > 0.4)."""
    ordered = sorted(moments, key=lambda item: -item.score)
    kept: list[Moment] = []
    for candidate in ordered:
        if any(_iou(candidate, other) > 0.4 for other in kept):
            continue
        kept.append(candidate)
    kept.sort(key=lambda item: item.start)
    return kept


def _iou(left: Moment, right: Moment) -> float:
    lo = max(left.start, right.start)
    hi = min(left.end, right.end)
    overlap = max(0.0, hi - lo)
    union = max(left.end, right.end) - min(left.start, right.start)
    return overlap / union if union > 0 else 0.0
