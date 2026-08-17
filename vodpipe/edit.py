"""Deciding what to remove from a chunk, and why.

This module is the whole editorial judgement of the pipeline and it holds no I/O.
It takes an audio loudness envelope, the finished transcript, and the censor list,
and returns an `EditPlan`: the ranges of the master to keep, the spans removed
with a reason for each, and the spans to mute.

The asymmetry that shapes every rule here is the same one the operator stated:

    leaving an "um" in costs a second of watching time.
    cutting a word in half is a defect the viewer hears immediately.

So the two signals are used for what each is actually good at, and neither is
trusted alone:

* **Acoustics propose.** Silence is measured from the audio, because the
  transcript cannot see it. Deepgram pads each word to abut its neighbour -- the
  median gap between one word's end and the next word's start is 0.000s across
  the 60,693-word reference recording -- so transcript gaps carry almost no
  information about where the speaker actually stopped.
* **The transcript vetoes.** A keep range is expanded until it fully contains
  every word it touches, so no acoustic decision can ever clip speech. This is
  not a theoretical guard: on a 5-minute sample of the reference recording,
  acoustic cuts alone clipped **37 of 701 words**, the worst by 230 ms. The veto
  removed all 37 and cost 1.1 percentage points of running time (20.5% -> 19.4%).

Measurements behind the thresholds, all from the 7-hour hasanabi reference
recording (60,693 words) unless stated:

* The loudness distribution is strongly bimodal -- p25 = -72 dB, p50 = -30 dB --
  so the noise floor sits in a wide dead zone and the result barely moves with
  it: -35 dB removes 21.0% of a talky sample, -41 dB removes 19.8%, -50 dB
  removes 19.0%. A threshold anywhere in that band is defensible, which is why
  `noise_floor_db` is a setting and not a calibration ritual.
* 825 adjacent identical word pairs occur. 147 of them are deliberate ("No. No.",
  "money, money, money", "duck, duck, go") and Deepgram punctuates every one of
  them; the stutters ("the the", "it's it's it's") carry no punctuation and sit
  at a 0.000s gap. Punctuation plus a gap bound separates the two cleanly: 640
  cut, 185 kept, no false positive found in a 25-sample manual review.
* 144 immediate phrase restarts ("I can I can", "that could be that could be").
* A repeated *number* is never a stutter. All four occurrences -- "fifty fifty",
  "twenty twenty eight", "ten ten thousand", "one one point" -- were figures, and
  dropping a copy changes the fact rather than the phrasing. A capitalisation
  test for proper nouns was measured and rejected: it blocked twelve correct cuts
  to prevent two questionable ones, and missed the case that prompted it.
* 257 "uh" and 121 "um", 125s in total. "mhmm" (27) and "uh-huh" (7) also occur
  and are *affirmations* -- deleting them changes what the speaker said, so they
  are excluded by name.
"""

from __future__ import annotations

import bisect
import math
import re
from dataclasses import dataclass, field
from typing import Iterable, Sequence

from .transcript import CensorList, Word
from .util import fmt_clock, round3

# Reasons a span leaves the edit. Used as the report's grouping key and as the
# `kind` recorded against every removed span.
SILENCE = "silence"
FILLER = "filler"
REPEAT = "repeat"

FILLER_MODES = ("off", "sounds", "smart")
REPEAT_MODES = ("off", "stutters", "restarts")
CENSOR_MODES = ("off", "mute")

# Hesitation sounds with no affirmative, negative or reactive meaning. Deepgram's
# filler_words option also writes down backchannels such as "mhmm" and "uh-huh";
# those are answers, not hesitation, and are deliberately absent.
VOCALISATIONS = frozenset({
    "uh", "uhh", "uhhh", "um", "umm", "ummm", "er", "err", "erm", "ah", "ahh",
})

# Words a speaker doubles for emphasis rather than by accident. Only consulted
# for single-word repeats, and only as a second line of defence: the punctuation
# rule already keeps "money, money, money" and "No. No."
EMPHATIC = frozenset({
    "no", "yes", "yeah", "yep", "nope", "very", "really", "so", "go", "run",
    "stop", "wait", "please", "never", "ever", "more", "again", "ok", "okay",
    "hey", "oh", "ha", "haha", "bye", "hello", "hi", "come", "now", "far",
    # Idiomatic reduplications. "free free Palestine" is a chant, not a stutter,
    # and the punctuation rule does not save it because the provider writes it
    # without a comma.
    "free", "night", "win", "half",
})

# A repeated number is a ratio, a year or a figure being read out, essentially
# never a stutter -- and unlike a doubled word, dropping one changes the fact
# rather than the phrasing. Every case this caught on the reference recording
# was a false positive of exactly that kind: "fifty fifty" (a 50/50 split),
# "twenty twenty eight" (a year), "ten ten thousand", "one one point".
NUMBER_WORDS = frozenset({
    "zero", "one", "two", "three", "four", "five", "six", "seven", "eight",
    "nine", "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen",
    "sixteen", "seventeen", "eighteen", "nineteen", "twenty", "thirty",
    "forty", "fifty", "sixty", "seventy", "eighty", "ninety", "hundred",
    "thousand", "million", "billion", "trillion",
})

# Discourse markers the `smart` tier will remove, but only where the provider
# punctuated them as a parenthetical inside a clause. Measured removability on
# the reference recording: "like" 456/916, "you know" 166/262, "I mean" 71/112.
# "so", "actually", "basically" and "literally" are deliberately absent: 3-6% of
# their occurrences pass the test and every one of those is sentence-initial,
# where removal changes emphasis rather than tightening a sentence.
DISCOURSE_MARKERS = (("like",), ("you", "know"), ("i", "mean"))

_EDGE_PUNCTUATION = re.compile(r"^[^\w']+|[^\w']+$")
_TERMINAL = re.compile(r"[.!?,;:]$")

# Two floats describing the same instant, compared. Timings are persisted to the
# millisecond, so anything below this is representation noise.
EPSILON = 1e-4


def normalise(text: str) -> str:
    """A word reduced to what makes it the same word: no case, no edge marks."""
    return _EDGE_PUNCTUATION.sub("", text).lower()


def _punctuated(word: Word) -> bool:
    return bool(_TERMINAL.search(word.text.strip()))


def _numeric(text: str) -> bool:
    core = normalise(text)
    return bool(core) and (core.replace(",", "").replace(".", "").isdigit()
                           or core in NUMBER_WORDS)


@dataclass(frozen=True)
class Removal:
    """One span leaving the edit, in master time, with why."""

    start: float
    end: float
    kind: str
    detail: str = ""

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


@dataclass(frozen=True)
class Mute:
    """One span whose audio is silenced while its video is kept."""

    start: float
    end: float
    term: str = ""

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


@dataclass
class EditOptions:
    """Everything the plan is allowed to depend on."""

    remove_silence: bool = True
    noise_floor_db: float = -41.0
    min_silence_seconds: float = 0.200
    min_speech_seconds: float = 0.200
    margin_seconds: float = 0.200
    # Below this a cut is not worth the jump: the gap is closed instead. Without
    # it the margins and the word veto leave a tail of 0.1s cuts that add visible
    # churn and save nothing (p10 of removed spans on the reference sample).
    min_cut_seconds: float = 0.250
    # How far a word-derived cut edge may slide to find a quieter place to land.
    # Bounded by a neighbour's midpoint, and it moves toward lower energy, so it
    # cannot walk into speech.
    snap_seconds: float = 0.050
    fillers: str = "sounds"
    repeats: str = "restarts"
    censor: str = "mute"
    censor_margin_seconds: float = 0.050
    # Refuse to publish an edit that removes more than this much. A plan that
    # deletes most of a chunk is a broken threshold or a silent track, not an
    # editorial decision, and rendering it wastes half an hour of encoding.
    max_removed_fraction: float = 0.75


@dataclass
class EditPlan:
    """What to keep, what was removed and why, and what to mute."""

    keep: list[tuple[float, float]] = field(default_factory=list)
    removals: list[Removal] = field(default_factory=list)
    mutes: list[Mute] = field(default_factory=list)
    source_duration: float = 0.0
    options: EditOptions = field(default_factory=EditOptions)

    @property
    def kept_seconds(self) -> float:
        return sum(b - a for a, b in self.keep)

    @property
    def removed_seconds(self) -> float:
        return max(0.0, self.source_duration - self.kept_seconds)

    @property
    def removed_fraction(self) -> float:
        if self.source_duration <= 0:
            return 0.0
        return self.removed_seconds / self.source_duration

    def removed_by(self, kind: str) -> float:
        return sum(item.duration for item in self.removals if item.kind == kind)

    def count_by(self, kind: str) -> int:
        return sum(1 for item in self.removals if item.kind == kind)

    @property
    def cuts(self) -> int:
        """Joins in the finished media -- what the viewer sees as jump cuts."""
        return max(0, len(self.keep) - 1)


# ------------------------------------------------------------------- envelope
#
# The envelope itself is produced and parsed in `media.py`, which owns every
# ffmpeg invocation and the output formats that come back from them. This module
# only ever sees a list of dB readings, which is what keeps it pure and lets the
# whole editorial decision be tested without touching a media file.


def silence_spans(envelope: Sequence[float], hop: float, threshold_db: float,
                  min_silence: float) -> list[tuple[float, float]]:
    """Runs quieter than `threshold_db` lasting at least `min_silence`."""
    spans: list[tuple[float, float]] = []
    run_start: int | None = None
    for index, level in enumerate(envelope):
        if level < threshold_db:
            if run_start is None:
                run_start = index
        elif run_start is not None:
            if (index - run_start) * hop >= min_silence:
                spans.append((run_start * hop, index * hop))
            run_start = None
    if run_start is not None and (len(envelope) - run_start) * hop >= min_silence:
        spans.append((run_start * hop, len(envelope) * hop))
    return spans


def _quietest(envelope: Sequence[float], hop: float,
              low: float, high: float, prefer: float) -> float:
    """The quietest instant in [low, high], ties broken toward `prefer`."""
    if not envelope or high <= low:
        return prefer
    first = max(0, int(low / hop))
    last = min(len(envelope) - 1, int(math.ceil(high / hop)))
    if last < first:
        return prefer
    best_at, best = prefer, None
    for index in range(first, last + 1):
        when = index * hop
        if when < low - EPSILON or when > high + EPSILON:
            continue
        level = envelope[index]
        if best is None or level < best - 0.5 or (
                abs(level - best) <= 0.5 and abs(when - prefer) < abs(best_at - prefer)):
            best, best_at = level, when
    return best_at if best is not None else prefer


# ------------------------------------------------------------------- ranges


def merge_ranges(ranges: Iterable[tuple[float, float]],
                 gap: float = 0.0) -> list[tuple[float, float]]:
    """Sorted, non-overlapping ranges; anything closer than `gap` is joined."""
    ordered = sorted((a, b) for a, b in ranges if b > a + EPSILON)
    merged: list[list[float]] = []
    for start, end in ordered:
        if merged and start <= merged[-1][1] + gap + EPSILON:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(a, b) for a, b in merged]


def subtract_range(ranges: Sequence[tuple[float, float]],
                   start: float, end: float) -> list[tuple[float, float]]:
    """`ranges` with [start, end) taken out of each."""
    out: list[tuple[float, float]] = []
    for a, b in ranges:
        if end <= a + EPSILON or start >= b - EPSILON:
            out.append((a, b))
            continue
        if a < start - EPSILON:
            out.append((a, start))
        if end < b - EPSILON:
            out.append((end, b))
    return out


def _complement(spans: Sequence[tuple[float, float]],
                 start: float, end: float) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    cursor = start
    for a, b in spans:
        if a > cursor + EPSILON:
            out.append((cursor, min(a, end)))
        cursor = max(cursor, b)
    if cursor < end - EPSILON:
        out.append((cursor, end))
    return [(a, b) for a, b in out if b > a + EPSILON]


# ------------------------------------------------------------------ fillers


def filler_spans(words: Sequence[Word], mode: str = "sounds") -> list[Removal]:
    """Word indices that carry no meaning, by the tier named in `mode`."""
    if mode not in FILLER_MODES:
        raise ValueError(f"unknown filler mode: {mode}")
    if mode == "off":
        return []

    found: list[Removal] = []
    taken: set[int] = set()
    for index, word in enumerate(words):
        if normalise(word.text) in VOCALISATIONS:
            found.append(Removal(word.start, word.end, FILLER, word.text.strip()))
            taken.add(index)

    if mode != "smart":
        return found

    for index in range(len(words)):
        for phrase in DISCOURSE_MARKERS:
            span = range(index, index + len(phrase))
            if index + len(phrase) > len(words) or any(i in taken for i in span):
                continue
            if tuple(normalise(words[i].text) for i in span) != phrase:
                continue
            previous = words[index - 1] if index else None
            last = words[index + len(phrase) - 1]
            # A parenthetical *inside* a clause: something before it ended with a
            # comma, and the marker itself closes with one. A sentence-initial
            # marker passes neither test, which is the point -- removing the
            # "Actually," that opens a sentence changes its emphasis.
            opens = previous is not None and previous.text.strip().endswith(",")
            closes = last.text.strip().endswith(",")
            if not (opens and closes):
                continue
            taken.update(span)
            found.append(Removal(
                words[index].start, last.end, FILLER,
                " ".join(words[i].text.strip() for i in span)))
    return found


# ------------------------------------------------------------------ repeats


def repeat_spans(words: Sequence[Word], mode: str = "restarts",
                 max_gap: float = 0.150) -> list[Removal]:
    """False starts: every copy but the last of an immediately repeated unit.

    Two rules, both gated on the provider's punctuation. A deliberate repetition
    is punctuated -- "No. No.", "money, money, money", "really, really racist" --
    and a stutter is not. On the reference recording that single test keeps 147
    of the 185 pairs it should keep; a 0.150s gap bound catches the other 38.
    """
    if mode not in REPEAT_MODES:
        raise ValueError(f"unknown repeat mode: {mode}")
    if mode == "off":
        return []

    keys = [normalise(word.text) for word in words]
    doomed: dict[int, str] = {}

    if mode == "restarts":
        # Longest first, so "that could be that could be" is seen as one restart
        # rather than three unrelated single-word repeats.
        index = 0
        while index < len(words):
            matched = 0
            for size in (5, 4, 3, 2):
                if index + 2 * size > len(words):
                    continue
                first = keys[index:index + size]
                second = keys[index + size:index + 2 * size]
                if not all(first) or first != second:
                    continue
                if any(_punctuated(words[index + k]) for k in range(size)):
                    continue
                if any(_numeric(words[index + k].text) for k in range(size)):
                    continue
                joint = words[index + size].start - words[index + size - 1].end
                if joint > max_gap:
                    continue
                matched = size
                break
            if matched:
                phrase = " ".join(words[index + k].text.strip()
                                  for k in range(matched))
                for k in range(matched):
                    doomed[index + k] = phrase
                index += 2 * matched
                continue
            index += 1

    # Single-word runs, in both tiers: keep only the last copy.
    index = 0
    while index < len(words) - 1:
        end = index
        while (end + 1 < len(words) and keys[end]
               and keys[end] == keys[end + 1]):
            end += 1
        if end > index:
            for k in range(index, end):
                if k in doomed:
                    continue
                current, following = words[k], words[k + 1]
                if _punctuated(current):
                    continue
                if following.start - current.end > max_gap:
                    continue
                if keys[k] in EMPHATIC or _numeric(current.text):
                    continue
                doomed[k] = current.text.strip()
            index = end
        index += 1

    # One span per contiguous run, not per word: "that could be that could be"
    # is a single three-word false start and the report should read as one.
    out: list[Removal] = []
    for index in sorted(doomed):
        if out and index - 1 in doomed and abs(out[-1].end - words[index].start) < EPSILON:
            out[-1] = Removal(out[-1].start, words[index].end, REPEAT, out[-1].detail)
        else:
            out.append(Removal(words[index].start, words[index].end, REPEAT,
                               doomed[index]))
    return out


# ------------------------------------------------------------------- censor


def mute_spans(words: Sequence[Word], censor: CensorList | None,
               margin: float = 0.050) -> list[Mute]:
    """Audio spans to silence. Muting is chosen over cutting on purpose: it
    leaves the timeline, the transcript and the lip sync untouched, so a wrong
    call costs a moment of missing audio rather than a broken edit."""
    if censor is None or not words:
        return []
    flagged = censor.flag(words)
    spans = [
        Mute(max(0.0, word.start - margin), word.end + margin, word.text.strip())
        for word, hit in zip(words, flagged) if hit
    ]
    merged: list[Mute] = []
    for span in sorted(spans, key=lambda item: item.start):
        if merged and span.start <= merged[-1].end + EPSILON:
            previous = merged[-1]
            merged[-1] = Mute(previous.start, max(previous.end, span.end),
                              f"{previous.term} {span.term}".strip())
        else:
            merged.append(span)
    return merged


# --------------------------------------------------------------------- plan


def plan_edit(words: Sequence[Word], envelope: Sequence[float], hop: float,
              duration: float, options: EditOptions,
              censor: CensorList | None = None) -> EditPlan:
    """Decide the edit. Pure: same inputs, same plan, no I/O and no clock."""
    if duration <= 0:
        raise ValueError("cannot plan an edit for media with no duration")
    words = [word for word in words if word.end > 0 and word.start < duration]

    removals: list[Removal] = []
    removals += filler_spans(words, options.fillers)
    removals += repeat_spans(words, options.repeats)
    # A word can be both the filler tier's and the repeat tier's ("uh uh"); one
    # span each is what the report and the subtraction below both want.
    removals = _dedupe(removals)

    # 1. Acoustics propose the keep ranges.
    word_cover = merge_ranges((word.start, word.end) for word in words)
    cover_starts = [start for start, _ in word_cover]
    if options.remove_silence and envelope:
        silences = silence_spans(envelope, hop, options.noise_floor_db,
                                 options.min_silence_seconds)
        islands = _complement(silences, 0.0, duration)
        speech = [
            island for island in islands
            if island[1] - island[0] >= options.min_speech_seconds - EPSILON
            or _overlaps(word_cover, cover_starts, island[0], island[1])
        ]
        keep = merge_ranges(
            (max(0.0, a - options.margin_seconds),
             min(duration, b + options.margin_seconds))
            for a, b in speech)
    else:
        keep = [(0.0, duration)]

    # An empty keep list here means the acoustics found no speech at all and the
    # transcript has no word to argue otherwise. It is deliberately *not*
    # rescued into "keep everything": that turns a silent track or a wrong
    # threshold into a 40-minute re-encode of an unchanged chunk, where the
    # ceiling check below turns it into a message that says which setting to
    # look at.

    # 2. The transcript vetoes: no keep boundary may fall inside a word.
    keep = _cover_words(keep, words)

    # 3. Only now are the deliberate word removals taken out, so that step 2
    #    cannot put a filler back by growing a range over it.
    # Snap first, then record the *snapped* span, so the report and the totals
    # describe the cut that was actually made rather than the one proposed.
    word_starts = [word.start for word in words]
    snapped: list[Removal] = []
    for removal in removals:
        start, end = _snap_removal(removal, words, word_starts, envelope, hop,
                                   options.snap_seconds)
        snapped.append(Removal(start, end, removal.kind, removal.detail))
        keep = subtract_range(keep, start, end)
    removals = snapped

    # 4. A cut shorter than min_cut_seconds is not worth the jump. Gaps holding a
    #    deliberate removal are never closed -- that is the whole point of them.
    keep = _close_pointless_gaps(keep, options.min_cut_seconds, removals)
    keep = [(max(0.0, a), min(duration, b)) for a, b in keep]
    keep = [(a, b) for a, b in keep if b - a > EPSILON]

    plan = EditPlan(keep=keep, removals=sorted(removals, key=lambda r: r.start),
                    mutes=mute_spans(words, censor if options.censor == "mute"
                                     else None, options.censor_margin_seconds),
                    source_duration=duration, options=options)

    if plan.removed_fraction > options.max_removed_fraction:
        raise EditRefused(
            f"the plan removes {100 * plan.removed_fraction:.0f}% of the chunk, "
            f"above the {100 * options.max_removed_fraction:.0f}% ceiling. That "
            f"is a silent or misconfigured source rather than an edit; check "
            f"edit.noise_floor_db and that the master has speech on it.")
    return plan


class EditRefused(RuntimeError):
    """The plan was rejected before anything was rendered."""


def _dedupe(removals: Sequence[Removal]) -> list[Removal]:
    seen: dict[tuple[int, int], Removal] = {}
    for removal in removals:
        key = (int(round(removal.start * 1000)), int(round(removal.end * 1000)))
        seen.setdefault(key, removal)
    return sorted(seen.values(), key=lambda item: item.start)


def _overlaps(spans: Sequence[tuple[float, float]], starts: Sequence[float],
              low: float, high: float) -> bool:
    """Does [low, high) meet any of `spans`? Binary search, not a scan.

    Called once per silence island against every word, which on a two-hour chunk
    is ~950 islands against 17,000 words. As a linear scan that is 16 million
    comparisons per plan; the whole point of planning before encoding is that it
    is quick enough to be worth doing first.
    """
    if not spans:
        return False
    index = bisect.bisect_right(starts, high)
    # Only the span starting at or before `high` can reach back into the window,
    # because the spans are ordered and disjoint.
    if index > 0 and spans[index - 1][1] > low + EPSILON:
        return True
    return index < len(spans) and spans[index][0] < high - EPSILON


def _cover_words(keep: Sequence[tuple[float, float]],
                 words: Sequence[Word]) -> list[tuple[float, float]]:
    """Grow every range until no word is only partly inside it.

    Expressed as a union rather than a search: merging the keep ranges with every
    word's own span does all three things at once -- extends a range that only
    partly covers a word, bridges two ranges a word spans, and restores a word
    that fell entirely inside a silence. That last case means the acoustics
    called speech silent, and between a transcript that heard a word and a
    threshold that did not, the word wins.
    """
    return merge_ranges(list(keep) + [(word.start, word.end) for word in words
                                      if word.end > word.start])


def _snap_removal(removal: Removal, words: Sequence[Word],
                  word_starts: Sequence[float], envelope: Sequence[float],
                  hop: float, snap: float) -> tuple[float, float]:
    """Slide a word-derived cut edge outward to the quietest nearby instant.

    The movement is deliberately one-way. It may only *grow* the removal, and
    only into the gap before the previous word ends or after the next one starts,
    so a cut can never reach a neighbouring word however quiet that word's
    interior happens to be -- a stop consonant is quieter than the room, and an
    energy search alone will walk straight into one.

    Growing is safe and shrinking is not, because Deepgram pads a word to abut
    its neighbour: the reported boundary already sits at or past the true end of
    the speech. Where the words genuinely abut there is no gap, the search has
    nowhere to go, and the cut stays exactly where the transcript put it.
    """
    if snap <= 0 or not envelope:
        return removal.start, removal.end
    # Words are start-ordered and de-overlapped by `transcript.normalise`, so
    # the neighbours are the entries either side of this span in that order.
    left = bisect.bisect_left(word_starts, removal.start + EPSILON)
    before = words[left - 1] if left > 0 else None
    right = bisect.bisect_left(word_starts, removal.end - EPSILON)
    after = words[right] if right < len(words) else None
    left_floor = max(0.0, removal.start - snap)
    if before is not None:
        left_floor = max(left_floor, before.end)
    right_ceiling = removal.end + snap
    if after is not None:
        right_ceiling = min(right_ceiling, after.start)

    start = removal.start
    if left_floor < removal.start - EPSILON:
        start = min(removal.start,
                    _quietest(envelope, hop, left_floor, removal.start,
                              removal.start))
    end = removal.end
    if right_ceiling > removal.end + EPSILON:
        end = max(removal.end,
                  _quietest(envelope, hop, removal.end, right_ceiling,
                            removal.end))
    return start, end


def _close_pointless_gaps(keep: Sequence[tuple[float, float]], minimum: float,
                          removals: Sequence[Removal]) -> list[tuple[float, float]]:
    if minimum <= 0 or len(keep) < 2:
        return list(keep)
    deliberate = [(item.start, item.end) for item in removals]
    out = [keep[0]]
    for start, end in keep[1:]:
        gap_start, gap_end = out[-1][1], start
        gap = gap_end - gap_start
        holds_removal = any(rs < gap_end + EPSILON and re > gap_start - EPSILON
                            for rs, re in deliberate)
        if gap < minimum and not holds_removal:
            out[-1] = (out[-1][0], end)
        else:
            out.append((start, end))
    return out


# ------------------------------------------------------------------ remapping


def remap_words(words: Sequence[Word],
                keep: Sequence[tuple[float, float]]) -> list[Word]:
    """The transcript of the *edited* media.

    Without this the operator gets a finished cut with no transcript, which
    throws away the text-based editing the rest of the pipeline exists to enable.
    A word is placed by its own start; a word whose range was removed is gone.

    The result has to be a *loadable* transcript: `words_from_json` refuses a
    file whose words step backwards, to a microsecond. Two things here are what
    guarantee that, and both are load-bearing:

    - **a word is clamped into the extent of the range that holds it.** The
      caller passes the ranges that were really rendered, and those are locked
      to frame boundaries, so a boundary the planner put exactly on a word's
      start moves up to one frame *later*. `word.start - start` then goes
      negative and an unclamped offset places the word before its own range,
      overlapping the last word of the previous one -- 48 words of the 8,022 in
      the reference chunk, by 2-10 ms, i.e. one frame at 60 fps.
    - **the endpoints are rounded, and the duration derived from them.**
      Rounding a start and a duration independently lets their sum land a
      millisecond past the next word's rounded start. Rounding is monotonic, so
      rounding both ends of a non-overlapping span cannot produce one.
    """
    offsets: list[tuple[float, float, float]] = []
    elapsed = 0.0
    for start, end in keep:
        offsets.append((start, end, elapsed))
        elapsed += end - start

    starts = [start for start, _, _ in offsets]
    out: list[Word] = []
    for word in words:
        # Placed by midpoint, not by start. A removed word's start lands exactly
        # on the boundary of the range that was cut around it, so a start-based
        # test hands it back at the tail of the preceding range -- the deleted
        # "uh" reappears in the transcript of a file that no longer contains it.
        middle = (word.start + word.end) / 2.0
        index = bisect.bisect_right(starts, middle + EPSILON) - 1
        for start, end, base in offsets[max(0, index):max(0, index) + 1]:
            if start - EPSILON <= middle <= end + EPSILON:
                span = max(0.0, end - start)
                low = min(max(0.0, word.start - start), span)
                high = min(max(low, word.end - start), span)
                new_start = round3(base + low)
                duration = round3(round3(base + high) - new_start)
                # A word left with no extent is not in the file: the frames it
                # was spoken over were not kept. Premiere draws a zero-length
                # word as an unclickable gap in the transcript panel, so it is
                # dropped rather than published as one -- the same rule
                # `normalise` applies for the same reason.
                if duration > 0.0:
                    out.append(Word(word.text, new_start, duration,
                                    word.confidence))
                break
    return out


def remap_mutes(mutes: Sequence[Mute],
                keep: Sequence[tuple[float, float]]) -> list[tuple[float, float]]:
    """Mute spans expressed on the edited timeline."""
    out: list[tuple[float, float]] = []
    elapsed = 0.0
    for start, end in keep:
        for mute in mutes:
            lo, hi = max(mute.start, start), min(mute.end, end)
            if hi > lo:
                out.append((elapsed + (lo - start), elapsed + (hi - start)))
        elapsed += end - start
    return merge_ranges(out)


# --------------------------------------------------------------------- report


def render_report(plan: EditPlan, *, source: str, offset: float = 0.0,
                  envelope: Sequence[float] | None = None,
                  hop: float = 0.010) -> str:
    """`edit.md`: every decision, with a timecode, so it can be audited.

    An automatic edit the operator cannot check is one they have to trust, and
    the whole reason the previous attempt at this was cancelled is that a cut
    they could not see was a cut they could not correct.
    """
    options = plan.options
    lines = [
        f"# Edit report — {source}",
        "",
        f"- source: {fmt_clock(plan.source_duration)}",
        f"- edited: {fmt_clock(plan.kept_seconds)} "
        f"({100 * (1 - plan.removed_fraction):.1f}% of the original)",
        f"- removed: {fmt_clock(plan.removed_seconds)} "
        f"({100 * plan.removed_fraction:.1f}%) in {plan.cuts} cuts",
        "",
        "| what | spans | time removed |",
        "|---|---|---|",
    ]
    silence_removed = plan.removed_seconds - (
        plan.removed_by(FILLER) + plan.removed_by(REPEAT))
    lines.append(f"| silence | {plan.cuts} | {fmt_clock(max(0.0, silence_removed))} |")
    for kind, label in ((FILLER, "fillers"), (REPEAT, "repeats / false starts")):
        lines.append(f"| {label} | {plan.count_by(kind)} | "
                     f"{fmt_clock(plan.removed_by(kind))} |")
    lines.append(f"| muted (kept, silenced) | {len(plan.mutes)} | "
                 f"{fmt_clock(sum(m.duration for m in plan.mutes))} |")

    lines += [
        "",
        "## Settings used",
        "",
        f"- noise floor `{options.noise_floor_db:g} dB`, "
        f"silences longer than `{options.min_silence_seconds:g}s`, "
        f"speech shorter than `{options.min_speech_seconds:g}s` dropped, "
        f"margin `{options.margin_seconds:g}s`",
        f"- fillers `{options.fillers}`, repeats `{options.repeats}`, "
        f"censor `{options.censor}`",
    ]

    if envelope:
        lines += ["", "## Noise floor sensitivity", "",
                  "How much silence each threshold would remove, so the setting "
                  "can be judged against this recording rather than guessed.",
                  "", "| threshold | silence found |", "|---|---|"]
        for threshold in (-30.0, -35.0, -41.0, -47.0, -55.0):
            spans = silence_spans(envelope, hop, threshold,
                                  options.min_silence_seconds)
            total = sum(b - a for a, b in spans)
            mark = "  ← in use" if abs(threshold - options.noise_floor_db) < 0.5 else ""
            lines.append(f"| {threshold:g} dB | {fmt_clock(total)}{mark} |")

    for kind, label in ((FILLER, "Fillers removed"),
                        (REPEAT, "Repeats and false starts removed")):
        items = [item for item in plan.removals if item.kind == kind]
        if not items:
            continue
        lines += ["", f"## {label} ({len(items)})", "",
                  "| at | for | text |", "|---|---|---|"]
        for item in items[:400]:
            lines.append(f"| {fmt_clock(item.start + offset)} | "
                         f"{item.duration:.2f}s | {item.detail or '—'} |")
        if len(items) > 400:
            lines.append(f"| … | | {len(items) - 400} more |")

    if plan.mutes:
        lines += ["", f"## Muted ({len(plan.mutes)})", "",
                  "Audio silenced, video and transcript untouched.", "",
                  "| at | for | term |", "|---|---|---|"]
        for mute in plan.mutes[:400]:
            lines.append(f"| {fmt_clock(mute.start + offset)} | "
                         f"{mute.duration:.2f}s | {mute.term or '—'} |")
        if len(plan.mutes) > 400:
            lines.append(f"| … | | {len(plan.mutes) - 400} more |")

    lines += ["", "---", "",
              "The master this was cut from is untouched. If a cut is wrong, "
              "change the settings and re-run the edit; nothing here is "
              "destructive.", ""]
    return "\n".join(lines)


def describe(plan: EditPlan) -> str:
    """One line for the log and the dashboard."""
    return (f"{fmt_clock(plan.kept_seconds)} of "
            f"{fmt_clock(plan.source_duration)} "
            f"({100 * plan.removed_fraction:.0f}% removed, {plan.cuts} cuts, "
            f"{plan.count_by(FILLER)} fillers, {plan.count_by(REPEAT)} repeats, "
            f"{len(plan.mutes)} muted)")
