"""Transcript model: words, pause-based segmentation, and boundary stitching.

Word timings come from the ASR provider natively, which is what lets this pipeline
skip forced alignment entirely. The only genuinely tricky part left is the seam
between two rolling slices, handled by `merge_streams` below.
"""

from __future__ import annotations

import json
import math
import os
import re
import secrets
import shutil
import threading
import errno
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

from .locks import ResourceLock
from .util import LOG, round3

# Adobe starts a new speech segment at every pause longer than this. Keeping the
# same figure is what makes pauses visible to Premiere's text-based editing.
PREMIERE_PAUSE_SECONDS = 0.4

# Looser grouping for human-facing outputs, where one line per breath is noise.
READING_PAUSE_SECONDS = 0.7
READING_MAX_SECONDS = 7.0
READING_MAX_WORDS = 14

MIN_WORD_SECONDS = 0.01

# Separate ASR requests can move a word by a few frames. Exact normalized token
# matches inside this bound are the same reading even when their spans no longer
# overlap at all.
SLICE_MATCH_DRIFT_SECONDS = 0.5

# A generation spans several sibling files, so a single os.replace() cannot make
# it atomic. Each target directory points at a shared journal while a publication
# is in flight. A prepared journal means restore every backup; a committed one
# means keep the new files and only remove transaction debris.
PUBLICATION_MARKER = ".transcript-publication.json"
_PUBLISH_LOCK = threading.RLock()
PUBLICATION_LOCK_NAME = ".transcript-publication.lock"
PUBLICATION_LOCK_TIMEOUT = 300.0


@dataclass
class Word:
    text: str
    start: float
    duration: float
    confidence: float = 1.0

    @property
    def end(self) -> float:
        return self.start + self.duration

    def shifted(self, offset: float) -> "Word":
        return Word(self.text, round3(self.start + offset), self.duration,
                    self.confidence)

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "start": round3(self.start),
            "duration": round3(self.duration),
            "confidence": round3(self.confidence),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Word":
        if not isinstance(data, dict):
            raise ValueError("word entry is not an object")
        missing = [key for key in ("text", "start", "duration", "confidence")
                   if key not in data]
        if missing:
            raise ValueError(f"word entry is missing {', '.join(missing)}")
        text = data["text"]
        if not isinstance(text, str) or not text.strip():
            raise ValueError("word text is blank or invalid")

        values: dict[str, float] = {}
        for key in ("start", "duration", "confidence"):
            value = data[key]
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"word {key} is not a number")
            number = float(value)
            if not math.isfinite(number):
                raise ValueError(f"word {key} is not finite")
            values[key] = number
        if values["start"] < 0.0 or values["duration"] < 0.0:
            raise ValueError("word timing is negative")
        if not 0.0 <= values["confidence"] <= 1.0:
            raise ValueError("word confidence is outside 0..1")
        return cls(
            text=text,
            start=values["start"],
            duration=values["duration"],
            confidence=values["confidence"],
        )


@dataclass
class Segment:
    words: list[Word] = field(default_factory=list)

    @property
    def start(self) -> float:
        return self.words[0].start if self.words else 0.0

    @property
    def end(self) -> float:
        return self.words[-1].end if self.words else 0.0

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)

    @property
    def text(self) -> str:
        return " ".join(word.text for word in self.words).strip()


def normalise(words: Iterable[Word]) -> list[Word]:
    """Sort, clamp and de-overlap a word stream so downstream maths is safe.

    Guarantees on the result: sorted, every duration at least `MIN_WORD_SECONDS`,
    and every adjacent pair satisfying `previous.end <= next.start`. A word must
    occupy a real, positive slice of the timeline -- Premiere renders a
    zero-length word as an unclickable gap in the transcript panel.

    Two words starting at the same instant cannot both satisfy that contract, so
    one is discarded rather than trimmed to nothing. Trimming used to leave the
    pair still overlapping, silently breaking the de-overlap guarantee callers
    rely on.
    """
    ordered = sorted((word for word in words if word.text.strip()),
                     key=lambda word: (word.start, word.end))
    cleaned: list[Word] = []
    for word in ordered:
        candidate = Word(word.text.strip(), round3(max(0.0, word.start)),
                         round3(max(MIN_WORD_SECONDS, word.duration)),
                         max(0.0, min(1.0, word.confidence)))
        if not cleaned:
            cleaned.append(candidate)
            continue

        previous = cleaned[-1]
        room = candidate.start - previous.start
        if room < MIN_WORD_SECONDS:
            # No room for both. Keep the more confident reading, then the longer
            # one, then the earlier -- deterministic either way.
            better = (candidate.confidence, candidate.duration) > \
                     (previous.confidence, previous.duration)
            if better:
                cleaned[-1] = candidate
            continue

        if candidate.start < previous.end:
            # Ordinary seam overlap: trim the earlier word, whose timing came
            # from less audio context than the later one's.
            previous.duration = round3(room)
        cleaned.append(candidate)
    return cleaned


def clamp_words(words: Iterable[Word], duration: float) -> list[Word]:
    """Clamp complete word spans to the media interval they belong to."""
    limit = max(0.0, float(duration))
    clamped: list[Word] = []
    for word in words:
        start = max(0.0, min(limit, word.start))
        end = max(0.0, min(limit, word.end))
        if end - start < MIN_WORD_SECONDS:
            continue
        clamped.append(Word(word.text, round3(start), round3(end - start),
                            word.confidence))
    return normalise(clamped)


def segment_words(words: Sequence[Word], max_gap: float = PREMIERE_PAUSE_SECONDS,
                  max_seconds: float | None = None,
                  max_words: int | None = None) -> list[Segment]:
    """Split a flat word stream into segments at pauses longer than `max_gap`."""
    segments: list[Segment] = []
    current: list[Word] = []
    for word in words:
        if current:
            gap = word.start - current[-1].end
            too_long = max_seconds is not None and word.end - current[0].start > max_seconds
            too_many = max_words is not None and len(current) >= max_words
            if gap > max_gap or too_long or too_many:
                segments.append(Segment(current))
                current = []
        current.append(word)
    if current:
        segments.append(Segment(current))
    return segments


def reading_segments(words: Sequence[Word]) -> list[Segment]:
    return segment_words(words, READING_PAUSE_SECONDS,
                         READING_MAX_SECONDS, READING_MAX_WORDS)


# ------------------------------------------------------------------ slice seams


def merge_streams(
    existing: Sequence[Word],
    incoming: Sequence[Word],
    overlap_start: float,
) -> list[Word]:
    """Join a new slice onto the accumulated stream without losing or doubling words.

    Consecutive slices deliberately overlap, so the same speech is transcribed
    twice around the seam. We resolve it by cutting at the widest pause inside the
    overlap: no word is ever split, and the newer slice wins for everything after
    the cut because it had full audio context for those words, where the older
    slice had them truncated at its tail.
    """
    existing = list(existing)
    incoming = list(incoming)
    if not existing:
        return normalise(incoming)
    if not incoming:
        return normalise(existing)

    seam = _seam_time(existing, incoming, overlap_start)
    added = [word for word in incoming if word.start >= seam]

    # Resolve lexical identity before looking at time coverage. A 50-150ms shift
    # can put two short instances of the same word on opposite sides of a gap,
    # where overlap alone sees two words. Sequence alignment also prevents a
    # repeated token from matching an arbitrary occurrence further away.
    matched = _aligned_existing(existing, added, overlap_start)
    kept = [word for index, word in enumerate(existing)
            if word.start < seam and index not in matched]

    # Everything after the seam normally comes from the newer slice. But ASR is
    # not deterministic: the new pass can simply fail to report a word the old
    # pass heard. Dropping every existing word past the seam would delete it for
    # good, so an existing word survives unless token alignment identified the
    # same reading or a lexically related clipped reading covers it. Mere timing
    # overlap with a different word is not enough to erase either one.
    superseded = _covered_by_related(added)
    orphans = [word for index, word in enumerate(existing)
               if word.start >= seam and index not in matched
               and not superseded(word)]

    return normalise(kept + orphans + added)


def _aligned_existing(existing: Sequence[Word], incoming: Sequence[Word],
                      overlap_start: float) -> set[int]:
    """Monotonically align equal normalized tokens near the rolling seam."""
    if not existing or not incoming:
        return set()

    drift = SLICE_MATCH_DRIFT_SECONDS
    old = [
        (index, word, _word_key(word.text))
        for index, word in enumerate(existing)
        if _midpoint(word) >= overlap_start - drift
    ]
    old_end = max((word.end for word in existing), default=overlap_start)
    new = [
        (index, word, _word_key(word.text))
        for index, word in enumerate(incoming)
        if _midpoint(word) <= old_end + drift
    ]
    if not old or not new:
        return set()

    # LCS with timing as a secondary score. The candidate region is only the
    # short overlap at the old slice's tail, not the accumulated transcript.
    scores: list[list[tuple[int, float]]] = [
        [(0, 0.0)] * (len(new) + 1) for _ in range(len(old) + 1)
    ]
    choices: list[list[str]] = [
        [""] * (len(new) + 1) for _ in range(len(old) + 1)
    ]
    for old_pos in range(1, len(old) + 1):
        for new_pos in range(1, len(new) + 1):
            above = scores[old_pos - 1][new_pos]
            left = scores[old_pos][new_pos - 1]
            if above >= left:
                best, choice = above, "old"
            else:
                best, choice = left, "new"

            old_word, old_key = old[old_pos - 1][1:]
            new_word, new_key = new[new_pos - 1][1:]
            midpoint_drift = abs(_midpoint(old_word) - _midpoint(new_word))
            if old_key and old_key == new_key and midpoint_drift <= drift:
                prior = scores[old_pos - 1][new_pos - 1]
                aligned = (prior[0] + 1, prior[1] - midpoint_drift)
                if aligned > best:
                    best, choice = aligned, "match"
            scores[old_pos][new_pos] = best
            choices[old_pos][new_pos] = choice

    matched: set[int] = set()
    old_pos, new_pos = len(old), len(new)
    while old_pos and new_pos:
        choice = choices[old_pos][new_pos]
        if choice == "match":
            matched.add(old[old_pos - 1][0])
            old_pos -= 1
            new_pos -= 1
        elif choice == "old":
            old_pos -= 1
        else:
            new_pos -= 1
    return matched


def _covered_by_related(incoming: Sequence[Word]):
    """Predicate: did a newer, lexically related reading cover this word?"""

    def covered(word: Word) -> bool:
        old_key = _word_key(word.text)
        for replacement in incoming:
            if not _word_overlaps(word, replacement):
                continue
            new_key = _word_key(replacement.text)
            # Prefixes catch a clipped tail such as "wor" -> "world". An
            # unrelated overlapping word is not evidence that the old pass was
            # wrong; both survive and normalise() makes their timing safe.
            if (old_key and new_key and
                    (old_key == new_key or
                     (min(len(old_key), len(new_key)) >= 2 and
                      (old_key.startswith(new_key) or
                       new_key.startswith(old_key))))):
                return True
        return False

    return covered


def _midpoint(word: Word) -> float:
    return (word.start + word.end) / 2.0


def _word_key(text: str) -> str:
    return "".join(character for character in text.casefold()
                   if character.isalnum())


def _seam_time(existing: Sequence[Word], incoming: Sequence[Word],
               overlap_start: float) -> float:
    """The earliest point in the overlap where neither stream has a word in flight.

    Earliest rather than widest on purpose. Everything after the seam comes from
    the newer slice, and the newer slice is the better one throughout the overlap:
    the older slice's audio was truncated at its tail, so its last word or two are
    frequently clipped ("wor" for "world"). Cutting as early as is safe hands the
    whole contested region to the transcription that actually heard all of it.
    """
    region_end = existing[-1].end if existing else overlap_start

    times = {overlap_start}
    times.update(word.end for word in existing
                 if overlap_start <= word.end <= region_end)
    times.update(word.start for word in incoming
                 if overlap_start <= word.start <= region_end)

    for candidate in sorted(times):
        if not _splits_any(existing, candidate) and not _splits_any(incoming, candidate):
            return round3(candidate)

    # Every candidate lands inside some word. Cutting at the first incoming word
    # start is then the safest remaining option: it cannot split an incoming word,
    # and normalise() trims whatever the older stream had running across it.
    later = [word.start for word in incoming if word.start >= overlap_start]
    return round3(later[0] if later else region_end)


def _splits_any(words: Sequence[Word], time: float) -> bool:
    return any(word.start < time < word.end for word in words)


# ------------------------------------------------------------------ chunk seams

# A word this close to a chunk boundary is one of the clipped halves the seam pass
# exists to repair, so it is always replaced by the seam's reading rather than
# kept alongside it. Wide enough to catch a fragment whose timing disagrees with
# the seam's by a fraction of a second; narrow enough that nothing further into
# the chunk is ever at risk.
BOUNDARY_CLIP_SECONDS = 0.35

# Independent ASR passes can move the same word several frames without changing
# its reading. Lexical matching within this window replaces that word cleanly;
# temporal overlap alone is deliberately insufficient because it can erase a
# different, adjacent word.
SEAM_MATCH_DRIFT_SECONDS = 0.75


def stitch_seam(
    previous_words: Sequence[Word],
    following_words: Sequence[Word],
    seam_words: Sequence[Word],
    *,
    seam_start: float,
    pivot: float,
    following_lead: float,
) -> tuple[list[Word], list[Word]]:
    """Rewrite two chunks' transcripts from one transcription that spans their join.

    Rolling slices overlap, so a word crossing a *slice* seam is heard whole by at
    least one of them. A word crossing a *chunk* boundary is not: the two chunks
    are separate files, transcribed independently, and each hears only its own
    half -- "world" becomes "wor" at the end of one and "ld" at the start of the
    next, or vanishes from both.

    The fix is a third transcription of audio that spans the join, taken from both
    files. `seam_words` are its words, timed from the start of that audio;
    `seam_start` is where that audio begins on the previous chunk's own clock, and
    `pivot` is the seam-local instant where the previous chunk's contribution ends.

    `seam_start` is passed rather than derived from the previous chunk's duration
    on purpose. If the tail slice came back shorter than was asked for -- a chunk
    whose media is a little shorter than its recorded duration -- deriving the
    offset from the duration would shift every seam word by the difference.

    Ownership is decided by where the majority of a word was spoken -- midpoint
    before `pivot` means it belongs to the previous chunk -- so a straddling word
    lands in exactly one transcript, whole, and never in both. Each side's seam
    region is then *replaced* rather than merged, which is what makes re-running
    this produce the same answer every time.

    What each side keeps is decided per word, not per region:

    * anything within `BOUNDARY_CLIP_SECONDS` of the join is discarded. Those are
      the clipped fragments by construction, and the seam pass is the only
      transcription that heard that moment whole.
    * anything else the seam pass also transcribed is replaced by its reading,
      which had the better audio.
    * everything the seam pass did not cover survives untouched. ASR is not
      deterministic, and deleting a chunk's words merely because a second pass did
      not repeat them would be a worse bug than the clipping this fixes.

    A seam pass that returns nothing at all changes nothing at all.
    """
    if not seam_words:
        return list(previous_words), list(following_words)

    cut = max(0.0, seam_start)
    # Where the join actually falls on the previous chunk's clock.
    boundary = cut + pivot

    # The same seam words in each chunk's own clock. Both full sets are needed for
    # the coverage test: a word straddling the join belongs to one chunk but
    # supersedes a fragment in the other.
    in_previous = clamp_words(
        [Word(word.text, round3(cut + word.start), word.duration,
              word.confidence) for word in seam_words],
        boundary,
    )
    in_following = clamp_words(
        [Word(word.text, round3(word.start - pivot), word.duration,
              word.confidence) for word in seam_words],
        following_lead,
    )

    previous_side: list[Word] = []
    following_side: list[Word] = []
    for word in seam_words:
        if word.start + word.duration / 2.0 < pivot:
            previous_side.append(Word(
                word.text, round3(cut + word.start), word.duration,
                word.confidence))
        else:
            following_side.append(Word(
                word.text, round3(word.start - pivot), word.duration,
                word.confidence))
    previous_side = clamp_words(previous_side, boundary)
    following_side = clamp_words(following_side, following_lead)

    return (
        _merge_seam_side(
            previous_words,
            in_previous,
            previous_side,
            clipped=lambda word: (
                word.end > boundary - BOUNDARY_CLIP_SECONDS),
        ),
        _merge_seam_side(
            following_words,
            in_following,
            following_side,
            clipped=lambda word: word.start < BOUNDARY_CLIP_SECONDS,
        ),
    )


def _merge_seam_side(existing: Sequence[Word], heard: Sequence[Word],
                     owned: Sequence[Word], *, clipped) -> list[Word]:
    """Replace safely matched readings while preserving unrelated neighbours."""
    matched_existing: set[int] = set()
    matched_heard: set[int] = set()
    candidates: list[tuple[float, int, int]] = []
    for old_index, old in enumerate(existing):
        old_key = _seam_word_key(old.text)
        if not old_key:
            continue
        for heard_index, replacement in enumerate(heard):
            if old_key != _seam_word_key(replacement.text):
                continue
            drift = abs((old.start + old.end) / 2.0 -
                        (replacement.start + replacement.end) / 2.0)
            if drift <= SEAM_MATCH_DRIFT_SECONDS:
                candidates.append((drift, old_index, heard_index))
    for _, old_index, heard_index in sorted(candidates):
        if old_index in matched_existing or heard_index in matched_heard:
            continue
        matched_existing.add(old_index)
        matched_heard.add(heard_index)

    kept = [word for index, word in enumerate(existing)
            if index not in matched_existing and not clipped(word)]
    added: list[Word] = []
    for replacement in owned:
        # A differently-worded neighbour that merely overlaps in time is not
        # evidence that it should be deleted. Keep it and decline the unsafe
        # replacement instead of letting normalise() choose one arbitrarily.
        conflicts = any(_word_overlaps(replacement, word) for word in kept)
        if not conflicts:
            added.append(replacement)
    return normalise(kept + added)


def _seam_word_key(text: str) -> str:
    return "".join(character for character in text.casefold()
                   if character.isalnum())


def _word_overlaps(left: Word, right: Word) -> bool:
    return left.start < right.end and right.start < left.end


# ------------------------------------------------------------------ persistence


def words_to_json(words: Sequence[Word]) -> list[dict[str, Any]]:
    return [word.to_dict() for word in words]


class CorruptWordsFile(RuntimeError):
    """`words.json` exists but cannot be read. Never the same as "not there yet"."""


class PublicationRecoveryError(RuntimeError):
    """A marker-covered generation cannot yet be made safe to read."""


def words_from_json(data: Any) -> list[Word]:
    if not isinstance(data, list):
        raise CorruptWordsFile("words is absent or is not a list")
    words: list[Word] = []
    previous_end = 0.0
    for index, item in enumerate(data):
        try:
            word = Word.from_dict(item)
        except (TypeError, ValueError, KeyError) as exc:
            raise CorruptWordsFile(f"word entry {index} is malformed: {exc}") from exc
        if word.start < previous_end - 1e-6:
            raise CorruptWordsFile(
                f"word entry {index} overlaps or precedes the previous word")
        words.append(word)
        previous_end = word.end
    return words


def words_json_text(words: Sequence[Word], meta: dict[str, Any]) -> str:
    """Serialise a word list, refusing one this module could not read back.

    The reader is strict to a microsecond, so the single writer every
    `words.json` goes through has to hold itself to the same rule -- and it
    checks the *rendered* entries, because rounding a start and a duration
    separately is one of the ways a valid word list becomes an invalid file.

    A words.json that will not load is not a cosmetic defect. Nothing can read
    it afterwards: not recovery, not `retranscribe`, and not the edited cut's
    generation check, which reads it to decide whether to spend another
    forty-minute encode. Refusing costs one publish and leaves the previous
    outputs untouched, which is the recoverable direction.
    """
    rendered = words_to_json(words)
    try:
        words_from_json(rendered)
    except CorruptWordsFile as exc:
        raise CorruptWordsFile(
            f"refusing to write a words.json that cannot be read back: {exc}"
        ) from exc
    return json.dumps({**meta, "words": rendered}, ensure_ascii=False)


def save_words(path: Path, words: Sequence[Word], meta: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(words_json_text(words, meta), encoding="utf-8")
    temp.replace(path)


def publish_text_sets(
    publications: Sequence[tuple[Path, dict[str, str], Sequence[str]]],
) -> None:
    """Commit one or more complete text generations, or restore all of them.

    All rendering must happen before this function is called. Backups and staged
    files for every directory are durable before the first canonical target is
    replaced. This is also the two-chunk transaction used by boundary stitching.
    """
    if not publications:
        return

    directories = [Path(directory).resolve()
                   for directory, _, _ in publications]
    if len(set(directories)) != len(directories):
        raise ValueError("a publication cannot name the same directory twice")

    with _publication_locks(directories):
        for directory in directories:
            _reconcile_publication_locked(directory)

        common = (directories[0].parent if len(directories) == 1 else
                  Path(os.path.commonpath([str(path) for path in directories])))
        common.mkdir(parents=True, exist_ok=True)
        transaction = common / f".transcript-publication-{secrets.token_hex(8)}"
        transaction.mkdir()
        journal_path = transaction / "journal.json"
        entries: list[dict[str, Any]] = []

        try:
            _fsync_directory(common)
            for index, ((_, rendered, owned), directory) in enumerate(
                    zip(publications, directories)):
                directory.mkdir(parents=True, exist_ok=True)
                owned_names = list(dict.fromkeys(owned))
                if any(Path(name).name != name for name in owned_names):
                    raise ValueError("published file names must not contain paths")
                if not set(rendered).issubset(owned_names):
                    raise ValueError("rendered publication contains an unowned file")

                stage = transaction / f"stage-{index}"
                backup = transaction / f"backup-{index}"
                stage.mkdir()
                backup.mkdir()
                _fsync_directory(transaction)
                present: list[str] = []
                for name in owned_names:
                    target = directory / name
                    if target.exists():
                        _durable_copy(target, backup / name)
                        present.append(name)
                for name, text in rendered.items():
                    _write_staged_file(stage / name, text)
                _fsync_directory(backup)
                _fsync_directory(stage)
                entries.append({
                    "directory": str(directory),
                    "owned": owned_names,
                    "present": present,
                    "rendered": list(rendered),
                    "stage": stage.name,
                    "backup": backup.name,
                })

            journal = {"version": 1, "state": "prepared", "entries": entries}
            _write_transaction_json(journal_path, journal)
            marker_text = json.dumps({"transaction": str(transaction)})
            for directory in directories:
                _write_transaction_json(directory / PUBLICATION_MARKER,
                                         json.loads(marker_text))

            # At this point every byte needed either to finish or to roll back is
            # on durable storage, as are the directory entries that locate it.
            _fsync_directory(transaction)
            for directory in directories:
                _fsync_directory(directory)

            for entry in entries:
                directory = Path(entry["directory"])
                stage = transaction / entry["stage"]
                # The manifest is the generation's commit record and is replaced
                # last. Readers that encounter the marker reconcile first.
                ordered = [name for name in entry["owned"]
                           if name != "exports.json"]
                if "exports.json" in entry["owned"]:
                    ordered.append("exports.json")
                for name in ordered:
                    target = directory / name
                    if name in entry["rendered"]:
                        _replace_published_file(stage / name, target)
                    elif target.exists():
                        _retire_published_file(target)

            journal["state"] = "committed"
            _write_transaction_json(journal_path, journal)
        except Exception:
            restored = False
            try:
                _restore_transaction(transaction, entries)
                restored = True
            finally:
                if restored:
                    _cleanup_transaction(transaction, entries)
            raise
        else:
            try:
                _cleanup_transaction(transaction, entries)
            except OSError as exc:
                # The committed journal makes leftover debris unambiguous and a
                # later read will remove it without rolling the generation back.
                LOG.warning("could not clean committed transcript transaction: %s",
                            exc)


def reconcile_publication(directory: Path) -> None:
    """Resolve an interrupted generation before a canonical file is read."""
    directory = Path(directory).resolve()
    marker = directory / PUBLICATION_MARKER
    if not marker.exists():
        return
    # Reading the marker to discover a shared seam transaction is harmless. No
    # canonical mutation occurs until every directory named by its journal is
    # locked in deterministic order.
    with _publication_locks([directory]):
        _reconcile_publication_locked(directory)


@contextmanager
def _publication_locks(directories: Sequence[Path]) -> Iterator[None]:
    """Hold all publication resources, expanding through shared journals."""
    wanted = {Path(directory).resolve() for directory in directories}
    with _PUBLISH_LOCK:
        while True:
            for directory in list(wanted):
                wanted.update(_marker_directories(directory))
            ordered = sorted(wanted, key=lambda path: os.path.normcase(str(path)))
            held: list[ResourceLock] = []
            retry = False
            try:
                for directory in ordered:
                    held.append(ResourceLock(
                        directory / PUBLICATION_LOCK_NAME,
                        timeout=PUBLICATION_LOCK_TIMEOUT,
                    ).acquire())

                expanded = set(wanted)
                for directory in list(wanted):
                    expanded.update(_marker_directories(directory))
                if expanded != wanted:
                    wanted = expanded
                    retry = True
                else:
                    yield
                    return
            finally:
                for lock in reversed(held):
                    lock.release()
            if not retry:
                return


def _is_bare_name(value: Any) -> bool:
    """A single path component: no separators, no traversal, not absolute."""
    return (isinstance(value, str) and value not in ("", ".", "..")
            and "/" not in value and "\\" not in value
            and not os.path.isabs(value)
            and Path(value).name == value)


def _validated_transaction(directory: Path, transaction: Path,
                           entries: Any) -> tuple[Path, list[dict[str, Any]]]:
    """Prove a journal only names files inside its own transcript directories.

    The journal and its marker are read back off disk, so a corrupt or crafted one
    could otherwise steer `_restore_transaction`/`_cleanup_transaction` into
    writing to or deleting paths outside the transcript tree -- an absolute or
    `..` `directory`, or an `owned`/`backup` name carrying a separator. Everything
    is required to resolve strictly inside the transaction's own parent, and every
    file name to be a bare component, before a single canonical file is moved.
    (P5/P6.)
    """
    directory = Path(directory).resolve()
    transaction = Path(transaction).resolve()
    root = transaction.parent
    if not transaction.name.startswith(".transcript-publication-"):
        raise PublicationRecoveryError(
            f"{transaction} is not a recognised transcript transaction directory")
    if not isinstance(entries, list) or not entries:
        raise PublicationRecoveryError("publication entries are invalid")
    seen_directory = False
    validated: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise PublicationRecoveryError("publication entry is not an object")
        raw = entry.get("directory")
        if not isinstance(raw, str):
            raise PublicationRecoveryError("publication entry has no directory")
        entry_dir = Path(raw).resolve()
        if entry_dir != root and root not in entry_dir.parents:
            raise PublicationRecoveryError(
                f"publication entry directory {entry_dir} is outside its "
                f"transaction {root}")
        if entry_dir == directory:
            seen_directory = True
        for key in ("owned", "present", "rendered"):
            names = entry.get(key, [])
            if not isinstance(names, list) or any(
                    not _is_bare_name(name) for name in names):
                raise PublicationRecoveryError(
                    f"publication entry {key} contains a non-plain file name")
        for key in ("stage", "backup"):
            if not _is_bare_name(entry.get(key)):
                raise PublicationRecoveryError(
                    f"publication entry {key} is not a plain component")
        validated.append(entry)
    if not seen_directory:
        raise PublicationRecoveryError(
            f"publication marker in {directory} is not named by its own journal")
    return transaction, validated


def _marker_directories(directory: Path) -> set[Path]:
    marker = Path(directory) / PUBLICATION_MARKER
    if not marker.exists():
        return set()
    try:
        marker_payload = json.loads(marker.read_text(encoding="utf-8"))
        transaction = Path(marker_payload["transaction"])
        journal = json.loads(
            (transaction / "journal.json").read_text(encoding="utf-8"))
        _, entries = _validated_transaction(
            directory, transaction, journal["entries"])
        return {Path(entry["directory"]).resolve() for entry in entries}
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError,
            PublicationRecoveryError):
        # A crafted or corrupt journal must not expand the lock set to arbitrary
        # directories; the per-directory reconcile below still refuses the read.
        return set()


def _reconcile_publication_locked(directory: Path) -> None:
    marker = Path(directory) / PUBLICATION_MARKER
    if not marker.exists():
        return
    try:
        marker_payload = json.loads(marker.read_text(encoding="utf-8"))
        transaction = Path(marker_payload["transaction"])
        journal_path = transaction / "journal.json"
        journal = json.loads(journal_path.read_text(encoding="utf-8"))
        entries = journal["entries"]
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        # A marker can cover a partially replaced generation. Without a usable
        # journal there is no proof that the canonical siblings agree, so leave
        # the marker for operator/recovery retry and fail every canonical read.
        raise PublicationRecoveryError(
            f"transcript publication marker in {directory} cannot be "
            f"reconciled: {exc}") from exc

    # P5/P6: refuse a journal that names anything outside its own transaction
    # before restore/cleanup can touch a canonical file or delete a marker.
    transaction, entries = _validated_transaction(directory, transaction, entries)

    try:
        if journal.get("state") != "committed":
            _restore_transaction(transaction, entries)
        _cleanup_transaction(transaction, entries)
    except OSError as exc:
        # In particular, ENOSPC during a restore must not turn into a direct
        # overwrite of a canonical file. The marker and journal stay in place,
        # making the mixed set unreadable until a later retry succeeds.
        raise PublicationRecoveryError(
            f"transcript publication recovery in {directory} failed: {exc}") \
            from exc


def _write_transaction_json(path: Path, payload: dict[str, Any]) -> None:
    temp = path.with_name(path.name + ".tmp")
    try:
        _write_durable_text(temp, json.dumps(payload, ensure_ascii=False))
        os.replace(temp, path)
        _fsync_directory(path.parent)
    finally:
        try:
            os.remove(temp)
        except FileNotFoundError:
            pass


def _write_staged_file(path: Path, text: str) -> None:
    _write_durable_text(path, text)


def _replace_published_file(staged: Path, target: Path) -> None:
    os.replace(staged, target)
    _fsync_directory(target.parent)


def _retire_published_file(target: Path) -> None:
    os.remove(target)
    _fsync_directory(target.parent)


def _restore_transaction(transaction: Path,
                         entries: Sequence[dict[str, Any]]) -> None:
    for entry in entries:
        directory = Path(entry["directory"])
        backup = transaction / entry["backup"]
        present = set(entry["present"])
        for name in entry["owned"]:
            target = directory / name
            if name in present:
                # Never copy onto the canonical inode: ENOSPC or interruption in
                # copyfile() would truncate the only individually valid version.
                # A fully fsynced sibling is atomically exchanged instead.
                _restore_backup(backup / name, target)
            else:
                try:
                    os.remove(target)
                except FileNotFoundError:
                    pass
                else:
                    _fsync_directory(directory)


def _cleanup_transaction(transaction: Path,
                         entries: Sequence[dict[str, Any]]) -> None:
    for entry in entries:
        marker = Path(entry["directory"]) / PUBLICATION_MARKER
        try:
            os.remove(marker)
        except FileNotFoundError:
            pass
        else:
            _fsync_directory(marker.parent)
    parent = transaction.parent
    shutil.rmtree(transaction)
    _fsync_directory(parent)


def _write_durable_text(path: Path, text: str) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())


def _durable_copy(source: Path, destination: Path) -> None:
    shutil.copyfile(source, destination)
    _fsync_file(destination)


def _fsync_file(path: Path) -> None:
    # Windows' CRT rejects fsync on a read-only descriptor (EBADF), even though
    # no write is performed by fsync itself. These are transaction-owned copies,
    # so opening read/write is safe and portable.
    with path.open("r+b") as handle:
        os.fsync(handle.fileno())


def _fsync_directory(path: Path) -> None:
    """Persist directory entries where the host filesystem supports it."""
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        if _unsupported_directory_fsync(exc):
            return
        raise
    try:
        try:
            os.fsync(descriptor)
        except OSError as exc:
            if not _unsupported_directory_fsync(exc):
                raise
    finally:
        os.close(descriptor)


def _unsupported_directory_fsync(exc: OSError) -> bool:
    unsupported = {
        errno.EINVAL,
        getattr(errno, "ENOTSUP", errno.EINVAL),
        getattr(errno, "EOPNOTSUPP", errno.EINVAL),
    }
    if os.name == "nt":
        unsupported.update({errno.EACCES, errno.EBADF, errno.EPERM})
    return exc.errno in unsupported


def _restore_backup(backup: Path, target: Path) -> None:
    temp = target.with_name(
        f".{target.name}.restore-{secrets.token_hex(8)}.tmp")
    try:
        _durable_copy(backup, temp)
        os.replace(temp, target)
        _fsync_directory(target.parent)
    finally:
        try:
            os.remove(temp)
        except FileNotFoundError:
            pass


def _validate_words_metadata(meta: dict[str, Any]) -> None:
    missing = [key for key in ("complete", "covered_seconds", "expected_seconds")
               if key not in meta]
    if missing:
        raise CorruptWordsFile(
            f"words metadata is missing {', '.join(missing)}")
    if not isinstance(meta["complete"], bool):
        raise CorruptWordsFile("words metadata complete is not a boolean")

    numeric = ("covered_seconds", "expected_seconds", "session_offset",
               "updated_at")
    for key in numeric:
        if key not in meta:
            continue
        value = meta[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise CorruptWordsFile(f"words metadata {key} is not a number")
        if not math.isfinite(float(value)) or float(value) < 0.0:
            raise CorruptWordsFile(
                f"words metadata {key} is negative or not finite")

    for key in ("channel", "session_id", "chunk", "language", "source"):
        if key in meta and (not isinstance(meta[key], str) or not meta[key].strip()):
            raise CorruptWordsFile(f"words metadata {key} is blank or invalid")

    generation = meta.get("generation")
    if generation is not None and (
            not isinstance(generation, str)
            or re.fullmatch(r"[0-9a-f]{16}", generation) is None):
        raise CorruptWordsFile("words metadata generation is invalid")

    identity = meta.get("asr_identity")
    if identity is not None:
        if not isinstance(identity, dict):
            raise CorruptWordsFile("words metadata asr_identity is not an object")
        required = {"provider", "model", "language", "filler_words"}
        if set(identity) not in (required, required | {"audio_stream"}):
            raise CorruptWordsFile(
                "words metadata asr_identity has missing or unknown fields")
        for key in ("provider", "model", "language"):
            if not isinstance(identity[key], str) or not identity[key].strip():
                raise CorruptWordsFile(
                    f"words metadata asr_identity.{key} is blank or invalid")
        if not isinstance(identity["filler_words"], bool):
            raise CorruptWordsFile(
                "words metadata asr_identity.filler_words is not a boolean")
        if "audio_stream" in identity:
            audio = identity["audio_stream"]
            expected_audio = {
                "ordinal", "codec", "language", "channels", "layout", "default",
            }
            if not isinstance(audio, dict) or set(audio) != expected_audio:
                raise CorruptWordsFile(
                    "words metadata asr_identity.audio_stream is invalid")
            if (isinstance(audio["ordinal"], bool)
                    or not isinstance(audio["ordinal"], int)
                    or audio["ordinal"] < 0):
                raise CorruptWordsFile(
                    "words metadata asr_identity.audio_stream.ordinal is invalid")
            if (isinstance(audio["channels"], bool)
                    or not isinstance(audio["channels"], int)
                    or audio["channels"] <= 0):
                raise CorruptWordsFile(
                    "words metadata asr_identity.audio_stream.channels is invalid")
            for key in ("codec", "language", "layout"):
                if not isinstance(audio[key], str) or not audio[key].strip():
                    raise CorruptWordsFile(
                        f"words metadata asr_identity.audio_stream.{key} is invalid")
            if not isinstance(audio["default"], bool):
                raise CorruptWordsFile(
                    "words metadata asr_identity.audio_stream.default is invalid")


def load_words(path: Path) -> tuple[list[Word], dict[str, Any]]:
    """Load the accumulated word stream. Raises if the file exists but is broken.

    Returning `([], {})` for a corrupt file the way it does for a missing one was
    silently destructive: the caller pairs these words with a persisted
    `transcribed_through` cursor, so a truncated file read as "no words yet" made
    the next pass resume at the *later* cursor and publish a transcript with its
    beginning permanently missing. A nonzero cursor with no readable words means
    recovery or an explicit rebuild from zero, not a fresh start.
    """
    reconcile_publication(path.parent)
    if not path.exists():
        return [], {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise CorruptWordsFile(f"could not read {path.name}: {exc}") from exc
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise CorruptWordsFile(
            f"{path.name} exists but is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise CorruptWordsFile(f"{path.name} is not a JSON object")
    if "words" not in payload:
        raise CorruptWordsFile(f"{path.name} has no words list")
    try:
        words = words_from_json(payload["words"])
    except CorruptWordsFile as exc:
        raise CorruptWordsFile(f"{path.name}: {exc}") from exc
    meta = {key: value for key, value in payload.items() if key != "words"}
    try:
        _validate_words_metadata(meta)
    except CorruptWordsFile as exc:
        raise CorruptWordsFile(f"{path.name}: {exc}") from exc
    return words, meta


# -------------------------------------------------------------------- profanity

_PUNCT = "\"'.,!?;:()[]{}-—…"


def strip_punctuation(text: str) -> str:
    return text.strip().strip(_PUNCT).strip()


# Ported from the C# tool. Only stems with no innocent host word belong here --
# "cunt" and "nigg" are deliberately absent because they hide inside Scunthorpe
# and niggardly, and the master list already carries those as exact terms.
PROFANE_ROOTS = (
    "bitch", "faggot", "fuck", "piss", "shit", "slut", "twat", "wank", "whore",
)

# Largest gap allowed between two words of the same censored phrase. Words are
# matched by position in the stream, and a stream has no gaps in it -- so without
# a time bound, "dead" at 00:04 and "body" at 00:14 with silence in between were
# read as the phrase "dead body" and both were flagged. A speaker pausing longer
# than this between two words is not saying them as one phrase.
PHRASE_MAX_GAP_SECONDS = 1.0


class CensorList:
    """The user's curated master list, plus a small ported set of profane stems.

    The master list is the source of truth and is matched exactly, including its
    multi-word phrases. The stems exist only to catch inflections and compounds
    ("shitting", "clusterfuck") that no hand-written list can enumerate, and they
    match by prefix or suffix rather than anywhere inside the word so that an
    innocent host word cannot trip them.
    """

    def __init__(self, terms: Iterable[str],
                 roots: Sequence[str] = PROFANE_ROOTS,
                 phrase_max_gap: float = PHRASE_MAX_GAP_SECONDS) -> None:
        self.exact: set[str] = set()
        self.phrases: list[tuple[str, ...]] = []
        for term in terms:
            cleaned = strip_punctuation(term.strip().lower())
            if not cleaned or term.lstrip().startswith("#"):
                continue
            if " " in cleaned:
                self.phrases.append(tuple(cleaned.split()))
            else:
                self.exact.add(cleaned)
        # Longest first so "jerking off" wins over a shorter overlapping phrase.
        self.phrases.sort(key=len, reverse=True)
        self.roots = tuple(roots)
        self.phrase_max_gap = float(phrase_max_gap)
        self.max_phrase = max((len(phrase) for phrase in self.phrases), default=0)

    @classmethod
    def load(cls, path: Path) -> "CensorList":
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            lines = []
        return cls(lines)

    def __bool__(self) -> bool:
        """True if this list can flag anything at all -- roots included.

        The roots alone catch inflections and compounds, so a list built with no
        user terms is still a working censor. Ignoring them here made a roots-only
        list read as empty, and the caller then skipped writing `censor-words.txt`
        for a transcript that did contain profanity.
        """
        return bool(self.exact or self.phrases or self.roots)

    def matches(self, text: str) -> str | None:
        """The term this single word triggers, or None."""
        surface = strip_punctuation(text).lower()
        if not surface:
            return None
        if surface in self.exact:
            return surface
        for root in self.roots:
            if surface.startswith(root) or surface.endswith(root):
                return root
        return None

    def flag(self, words: Sequence[Word]) -> list[bool]:
        """Per-word censor flags, including words that only match as part of a phrase."""
        flags = [self.matches(word.text) is not None for word in words]
        if not self.phrases:
            return flags

        surfaces = [strip_punctuation(word.text).lower() for word in words]
        for index in range(len(words)):
            for length in range(min(self.max_phrase, len(words) - index), 1, -1):
                window = tuple(surfaces[index:index + length])
                if window in self._phrase_set and self._spoken_together(words, index,
                                                                        length):
                    for offset in range(length):
                        flags[index + offset] = True
                    break
        return flags

    def _spoken_together(self, words: Sequence[Word], index: int,
                         length: int) -> bool:
        """Were these words actually said as one phrase, or merely adjacent?

        Adjacency in the word list says nothing about time: two words either side
        of a long silence are neighbours in the stream. A phrase has to have been
        spoken continuously to be one.
        """
        for offset in range(1, length):
            previous = words[index + offset - 1]
            current = words[index + offset]
            if current.start - previous.end > self.phrase_max_gap:
                return False
        return True

    @property
    def _phrase_set(self) -> set[tuple[str, ...]]:
        if not hasattr(self, "_phrase_cache"):
            self._phrase_cache = set(self.phrases)
        return self._phrase_cache

    def present_in(self, words: Sequence[Word]) -> list[tuple[str, int]]:
        """Terms from this transcript that hit the list, most frequent first.

        Premiere matches against the transcript text it can see, so the export
        carries surface forms as spoken, plus any phrase that actually occurred.
        """
        counts: dict[str, int] = {}
        for word in words:
            if self.matches(word.text) is None:
                continue
            surface = strip_punctuation(word.text).lower()
            if surface:
                counts[surface] = counts.get(surface, 0) + 1

        surfaces = [strip_punctuation(word.text).lower() for word in words]
        for phrase in self.phrases:
            length = len(phrase)
            # Same temporal test as flag(), so the exported list and the tagged
            # transcript cannot disagree about what was said.
            hits = sum(1 for index in range(len(surfaces) - length + 1)
                       if tuple(surfaces[index:index + length]) == phrase
                       and self._spoken_together(words, index, length))
            if hits:
                counts[" ".join(phrase)] = counts.get(" ".join(phrase), 0) + hits

        return sorted(counts.items(), key=lambda item: (-item[1], item[0]))


_SENTENCE_END = re.compile(r"[.!?]$")


def ends_sentence(text: str) -> bool:
    return bool(_SENTENCE_END.search(text.rstrip("\"')]}")))
