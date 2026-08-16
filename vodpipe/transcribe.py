"""Rolling transcription: slice the chunk that is still recording and publish as we go.

The point of doing this during the recording rather than after it is latency. A
2-hour chunk transcribed from scratch on close would take minutes to upload and
process; transcribed rolling, everything but the final slice is already done by
the time the chunk closes, so exports land about a minute later.
"""

from __future__ import annotations

import math
import json
import secrets
import shutil
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from .asr import (
    ASRProvider,
    DeepgramProvider,
    TranscriptionError,
    build_provider,
    transcribe_audio,
)
from .config import Config
from .exports import GENERATION_FILES, write_export_sets, write_exports
from .media import (
    concat_audio,
    extract_audio_slice,
    live_duration,
    probe_asr_stream,
)
from .state import (
    DONE,
    ERROR,
    RECORDING,
    RUNNING,
    Chunk,
    Session,
    SessionStore,
)
from .transcript import (
    CensorList,
    PUBLICATION_MARKER,
    Word,
    clamp_words,
    load_words,
    merge_streams,
    publish_text_sets,
    reconcile_publication,
    stitch_seam,
)
from .util import LOG, Tools, media_duration

# Outcomes of one `advance()` call.
PROGRESSED = "progressed"   # a window was transcribed; more may remain
IDLE = "idle"               # not enough new audio yet; try again later
COMPLETE_ = "complete"      # the chunk is fully covered and published
BLOCKED = "blocked"         # cannot proceed; retrying will not help by itself

# Only absorb float representation noise, and only ever between two numbers of
# the same kind -- two cursors, two extraction measurements. Comparing a
# probe-derived duration against an extraction-derived cursor is a different
# thing and uses SHORT_READ_TOLERANCE below; getting that distinction wrong is
# what produced both halves of the 2026-08-16 coverage failure.
COVERAGE_EPSILON = 0.001

# Shortest slice worth extracting. Deliberately tiny: the previous 0.5s floor
# meant a whole chunk shorter than that was published without one ASR call.
MIN_AUDIO = COVERAGE_EPSILON

# How much shorter than requested an extracted slice may be before it is treated
# as a short read rather than measurement noise.
#
# CORRECTED 2026-08-16: this was COVERAGE_EPSILON, i.e. one millisecond, and no
# real media meets that. Two independent measurements of the same audio disagree
# for reasons that have nothing to do with missing content:
#
# * an audio frame is a quantum. AAC-LC at 48 kHz is 21.3 ms, and both the seek
#   and the cut can land on a frame boundary, so ~43 ms of disagreement between
#   what ffmpeg was asked for and what it wrote is ordinary;
# * `-ss`/`-t` are passed to ffmpeg rounded to the millisecond, so the request
#   itself is not exactly the float the caller computed;
# * the recorder's idea of a chunk's duration and ffprobe's reading of the
#   finished container are separate measurements, and a container's duration is
#   the longest stream, which is the video, not the audio track we transcribe.
#
# A live 8-hour recording hit all three: every chunk was declared incomplete and
# abandoned its last 68-96 seconds untranscribed over disagreements of 1 ms and
# 34 ms. This value is several times the physical worst case and still well under
# a spoken word (~0.3s), which is the smallest thing whose loss would matter.
#
# It applies to *every* comparison between a probe-derived duration and an
# extraction-derived cursor, which means the completion tests as well as the
# entry tests. Loosening only the entry tests -- the first attempt at this fix --
# let the missing tail finally transcribe and then failed the chunk one step
# later on "audio read made no progress", because ffmpeg cannot emit a partial
# audio frame: the last extractable sample sits up to one frame short of the
# duration the container advertises, so a cursor chasing that last frame can
# never reach it.
SHORT_READ_TOLERANCE = 0.15

# How far the persisted cursor may exceed readable audio before the state is
# treated as corrupt rather than as a rounding difference.
STALE_CURSOR_TOLERANCE = 2.0


def _differs(left: Sequence[Word], right: Sequence[Word]) -> bool:
    """Did a stitch actually change anything? Avoids pointless republishing."""
    return list(left) != list(right)


@dataclass
class AdvanceResult:
    """Why a transcription pass stopped, and how much it covered."""

    status: str
    added_words: int = 0
    covered_through: float = 0.0
    expected: float = 0.0
    detail: str = ""

    @property
    def complete(self) -> bool:
        return self.status == COMPLETE_

    @property
    def progressed(self) -> bool:
        return self.status in (PROGRESSED, COMPLETE_)


class RollingTranscriber:
    """Advances a chunk's transcript by whatever audio has become available."""

    def __init__(self, config: Config, tools: Tools, store: SessionStore) -> None:
        self.config = config
        self.tools = tools
        self.store = store
        self._provider: ASRProvider | None = None
        self._provider_key: tuple | None = None
        self._provider_lock = threading.Lock()
        self._censor: CensorList | None = None
        # (resolved path, mtime_ns, size) of the list currently loaded.
        self._censor_key: tuple | None = None
        # Set to bypass provider construction entirely (tests, or an engine wired
        # up by something other than config).
        self.provider_override: ASRProvider | None = None
        # Retranscription keeps the prior generation published until the first
        # replacement commits. This identifies the one call that must begin from
        # an empty word stream even though the old words remain readable.
        self._fresh_rebuilds: set[Path] = set()
        self._reconcile_interrupted_publications()

    # -- collaborators -----------------------------------------------------

    def _reconcile_interrupted_publications(self) -> None:
        """Finish publication recovery before dashboard/file readers can run."""
        root = self.config.masters_root
        if not root.exists():
            return
        for marker in root.rglob(PUBLICATION_MARKER):
            reconcile_publication(marker.parent)

    def _semantic_identity(
        self,
        language: str | None = None,
        audio_stream: dict[str, object] | None = None,
    ) -> dict[str, object]:
        """Recognition settings that must never vary within one word generation."""
        identity: dict[str, object] = {
            "provider": str(self.config.get("transcription.provider", "deepgram")),
            "model": str(self.config.get("transcription.model", "nova-3")),
            "language": str(self.config.get("transcription.language", "en")
                            if language is None else language),
            "filler_words": bool(self.config.get(
                "transcription.filler_words", True)),
        }
        if audio_stream is not None:
            identity["audio_stream"] = dict(audio_stream)
        return identity

    @staticmethod
    def _identity_model(identity: dict[str, object]) -> tuple[str, object | None]:
        direct = identity.get("audio_stream")
        if isinstance(direct, dict):
            return str(identity.get("model") or ""), direct
        value = str(identity.get("model") or "")
        try:
            decoded = json.loads(value)
        except (TypeError, json.JSONDecodeError):
            return value, None
        if (isinstance(decoded, dict)
                and isinstance(decoded.get("model"), str)
                and isinstance(decoded.get("audio_stream"), dict)):
            return decoded["model"], decoded["audio_stream"]
        return value, None

    @classmethod
    def _normalise_identity(cls, identity: dict[str, object]) -> dict[str, object]:
        """Read both current identities and the pre-audio_stream legacy form."""
        model, audio_stream = cls._identity_model(identity)
        normalised: dict[str, object] = {
            "provider": str(identity.get("provider") or ""),
            "model": model,
            "language": str(identity.get("language") or ""),
            "filler_words": bool(identity.get("filler_words")),
        }
        if isinstance(audio_stream, dict):
            normalised["audio_stream"] = dict(audio_stream)
        return normalised

    def _source_semantic(self, source: Path,
                          language: str | None = None) -> dict[str, object]:
        selector = self.config.get("transcription.audio_stream", "auto")
        _, audio_stream = probe_asr_stream(self.tools, source, selector)
        return self._semantic_identity(language, audio_stream)

    def _frozen_source_semantic(
        self, source: Path, identity: dict[str, object],
    ) -> dict[str, object]:
        """Resolve a persisted logical track without consulting new settings."""
        frozen = self._normalise_identity(identity)
        wanted = frozen.get("audio_stream")
        if not isinstance(wanted, dict):
            # Old four-field generations did not always record a track. Preserve
            # their ASR semantics and freeze the currently selected logical track
            # on the next successful publication.
            _, resolved = probe_asr_stream(
                self.tools, source,
                self.config.get("transcription.audio_stream", "auto"))
            frozen["audio_stream"] = resolved
            return frozen

        ordinal = wanted.get("ordinal")
        if isinstance(ordinal, bool) or not isinstance(ordinal, int):
            raise RuntimeError("persisted ASR audio track has no valid ordinal")
        try:
            _, resolved = probe_asr_stream(self.tools, source, ordinal)
        except RuntimeError as exc:
            raise RuntimeError(
                f"the frozen ASR audio track (ordinal {ordinal}) is no longer "
                f"present in {source.name}: {exc}") from exc
        if resolved != wanted:
            raise RuntimeError(
                f"the frozen ASR audio track disappeared or changed in "
                f"{source.name}; expected {wanted!r}, found {resolved!r}")
        frozen["audio_stream"] = resolved
        return frozen

    def _audio_selector(self, semantic: dict[str, object]) -> str | int:
        _, audio_stream = self._identity_model(semantic)
        if isinstance(audio_stream, dict):
            ordinal = audio_stream.get("ordinal")
            if isinstance(ordinal, int) and not isinstance(ordinal, bool):
                return ordinal
        return self.config.get("transcription.audio_stream", "auto")

    def _provider_identity(self, semantic: dict[str, object]) -> tuple:
        """Semantic identity plus credentials and refreshable transport settings."""
        model, _ = self._identity_model(semantic)
        return (
            self.config.secret("deepgram_api_key"),
            semantic["provider"],
            model,
            semantic["language"],
            semantic["filler_words"],
            int(self.config.get("transcription.max_retries", 4)),
            float(self.config.get("transcription.request_timeout_seconds", 600)),
        )

    def provider(self, semantic: dict[str, object] | None = None) -> ASRProvider:
        if self.provider_override is not None:
            return self.provider_override
        semantic = self._normalise_identity(
            semantic or self._semantic_identity())
        identity = self._provider_identity(semantic)
        with self._provider_lock:
            if self._provider is None or identity != self._provider_key:
                if semantic == self._semantic_identity():
                    provider = build_provider(self.config, str(identity[0]))
                elif str(semantic["provider"]).lower() == "deepgram":
                    # One-shot's explicit language and frozen rolling generations
                    # must reach the provider, not only their output metadata.
                    provider = DeepgramProvider(
                        str(identity[0]),
                        model=str(identity[2]),
                        language=str(semantic["language"]),
                        filler_words=bool(semantic["filler_words"]),
                        max_retries=int(identity[5]),
                        timeout=float(identity[6]),
                    )
                else:
                    raise TranscriptionError(
                        f"unknown transcription provider: {semantic['provider']}")
                self._provider = provider
                self._provider_key = identity
            return self._provider

    def censor(self) -> CensorList | None:
        """The censor list, reloaded when the file or its path changes.

        AUD2-062: this was loaded once per process and never rechecked, so
        editing the list, pointing at a different one, or creating a list that
        was absent at first use all had no effect until a restart -- while
        Settings reported the new value as saved. Keyed on the resolved path plus
        the file's mtime and size, which is enough to notice an edit without
        rehashing a large list on every publication.
        """
        raw = str(self.config.get("paths.censor_master_list", "") or "")
        if not raw:
            self._censor = None
            self._censor_key = None
            return None
        path = Path(raw)
        try:
            stat = path.stat()
            fingerprint = (str(path.resolve()), stat.st_mtime_ns, stat.st_size)
        except OSError:
            # Missing now; forget any list loaded from an earlier path so the
            # next publication does not censor from a file that is no longer set.
            self._censor = None
            self._censor_key = None
            return None
        if fingerprint != self._censor_key:
            self._censor = CensorList.load(path)
            self._censor_key = fingerprint
            LOG.info("censor list: %d terms, %d phrases from %s",
                     len(self._censor.exact), len(self._censor.phrases), path.name)
        return self._censor or None

    # -- paths -------------------------------------------------------------

    def output_dir(self, session: Session, chunk: Chunk) -> Path:
        return session.path / "transcripts" / chunk.label

    def words_path(self, session: Session, chunk: Chunk) -> Path:
        return self.output_dir(session, chunk) / "words.json"

    def source_for(self, session: Session, chunk: Chunk) -> Path | None:
        """The best readable media for this chunk right now.

        While recording, only the .ts is readable -- an MP4 has no index until it
        is closed. Afterwards the remuxed master is preferred because the .ts may
        already have been reclaimed.
        """
        live = session.path / "live" / chunk.ts_name
        master = session.path / "master" / chunk.master_name
        if chunk.status == RECORDING and live.exists():
            return live
        if master.exists():
            return master
        if live.exists():
            return live
        return None

    # -- the loop ----------------------------------------------------------

    def available_seconds(self, session: Session, chunk: Chunk,
                          source: Path) -> float:
        """How much audio can actually be read right now.

        For a closed chunk this is what the media holds, not what the recorder
        claimed. Trusting the claim meant a truncated file looked like it still
        had audio to come, and finalisation kept asking for windows past the end.
        """
        if chunk.status == RECORDING:
            margin = float(self.config.get("transcription.live_margin_seconds", 20))
            return max(0.0, live_duration(self.tools, source) - margin)
        probed = live_duration(self.tools, source, allow_scan=True)
        if probed <= 0:
            return max(0.0, chunk.duration)
        return min(probed, chunk.duration) if chunk.duration > 0 else probed

    def expected_seconds(self, session: Session, chunk: Chunk,
                         source: Path) -> float:
        """Total audio this chunk should eventually cover, once it has closed."""
        if chunk.duration > 0:
            return chunk.duration
        return live_duration(self.tools, source, allow_scan=True)

    def advance(self, session: Session, chunk: Chunk, *,
                final: bool = False) -> AdvanceResult:
        """Transcribe one slice of whatever new audio exists.

        Advances by at most one `slice_seconds` window, so a chunk that is several
        windows behind needs several calls -- see `finalize()`. Returns a structured
        result rather than a word count because "nothing was added" has several
        very different meanings, and treating them alike is what let a chunk be
        published as complete with most of its audio missing.
        """
        if not self.config.get("transcription.enabled", True):
            return AdvanceResult(BLOCKED, detail="transcription is disabled")

        source = self.source_for(session, chunk)
        if source is None:
            return AdvanceResult(BLOCKED, detail="no media for this chunk yet")

        slice_seconds = float(self.config.get("transcription.slice_seconds", 300))
        min_slice = float(self.config.get("transcription.min_slice_seconds", 45))
        overlap = float(self.config.get("transcription.overlap_seconds", 3.0))
        if slice_seconds <= 0:
            return AdvanceResult(BLOCKED, detail="slice_seconds must be positive")

        closed = chunk.status != RECORDING
        available = self.available_seconds(session, chunk, source)
        expected = (self.expected_seconds(session, chunk, source)
                    if final or closed else 0.0)
        cursor = float(chunk.transcribed_through)
        remaining = available - cursor
        words_path = self.words_path(session, chunk)
        if words_path in self._fresh_rebuilds:
            existing, meta = [], {}
        else:
            existing, meta = load_words(words_path)
        frozen = meta.get("asr_identity")
        try:
            semantic = (self._frozen_source_semantic(source, frozen)
                        if isinstance(frozen, dict)
                        else self._source_semantic(source))
        except RuntimeError as exc:
            detail = str(exc)
            self.store.update_chunk(session, chunk, transcript_status=ERROR,
                                    transcript_error=detail)
            return AdvanceResult(BLOCKED, covered_through=cursor,
                                 expected=expected, detail=detail)

        if cursor - available > STALE_CURSOR_TOLERANCE:
            # AUD2-030: the cursor claims more audio than the media holds. The
            # old `max(cursor, available)` believed the cursor and published the
            # chunk as complete at a position the file never reached. Something
            # is wrong -- a truncated source, or a cursor from a different
            # generation -- and neither is fixed by transcribing more.
            return AdvanceResult(
                BLOCKED,
                covered_through=cursor,
                detail=(f"transcript cursor is at {cursor:.1f}s but only "
                        f"{available:.1f}s of audio is readable; needs a rebuild"))

        if closed and expected > 0:
            source_shortfall = expected - available
            if source_shortfall > SHORT_READ_TOLERANCE:
                detail = (
                    f"closed chunk audio short read: requested {expected:.3f}s, "
                    f"measured {available:.3f}s ({source_shortfall:.3f}s short)")
                self.store.update_chunk(session, chunk, transcript_status=ERROR,
                                        transcript_error=detail)
                return AdvanceResult(BLOCKED, covered_through=cursor,
                                     expected=expected, detail=detail)
            if source_shortfall > 0:
                # The recorder's bookkeeping and the closed container's own
                # measurement disagree by frame-quantisation noise. The file is
                # the authority on how much audio exists, and coverage is
                # measured against audio that exists -- leaving `expected` at the
                # recorder's slightly larger figure made completion arithmetically
                # unreachable, so the chunk finalised as permanently incomplete
                # even once every readable sample had been transcribed.
                expected = available

        # SHORT_READ_TOLERANCE, not COVERAGE_EPSILON: `remaining` is a probed
        # duration minus an extracted cursor. At 1 ms this let a pass through to
        # request a 3.001s slice of which only 3.000s exists, which then failed
        # the no-progress check below and blocked an otherwise finished chunk.
        if remaining <= SHORT_READ_TOLERANCE and (
                cursor > 0 or expected <= COVERAGE_EPSILON):
            # Nothing further to read. When finalising, that is completion --
            # including the sub-second tail and the no-speech chunk, which
            # previously fell through without ever being marked done.
            #
            # AUD2-049: the `cursor > 0` guard is what makes this a *tail*
            # tolerance. Without it a whole chunk shorter than the old tail
            # allowance was
            # published as complete silence having never been sent to the
            # provider at all, so a real word spoken in a 0.8s clip vanished.
            if final:
                return self._finish(session, chunk, existing,
                                    max(cursor, available), expected, semantic)
            return AdvanceResult(IDLE, covered_through=cursor)

        if remaining < min_slice and not final:
            return AdvanceResult(IDLE, covered_through=cursor)

        window_end = min(available, cursor + slice_seconds)
        # Re-read a little of what we already transcribed, so a word spanning the
        # seam is captured whole by at least one of the two slices.
        window_start = max(0.0, cursor - overlap) if cursor > 0 else 0.0

        # One contiguous window. There is deliberately no ad-exclusion step:
        # streamlink filters ad segments before they reach the recording, so no
        # interval of this file is an ad, and an earlier version that carved
        # ranges out of it from log lines removed legitimate speech.
        windows = [(window_start, window_end)]

        added_total = 0

        self.store.update_chunk(session, chunk, transcript_status=RUNNING)
        work_dir = self.config.work_root / session.session_id / chunk.label
        work_dir.mkdir(parents=True, exist_ok=True)

        reached = window_start
        try:
            for index, (start, end) in enumerate(windows):
                if end - start < MIN_AUDIO:
                    continue
                audio = work_dir / f"slice_{int(start * 1000):012d}.flac"
                try:
                    extract_audio_slice(self.tools, source, audio, start,
                                        end - start,
                                        audio_stream=self._audio_selector(semantic))
                    # AUD2-006: measure what came out, not what was asked for.
                    # ffmpeg can exit 0 having written less than requested when
                    # the window runs past readable audio or the audio track is
                    # shorter than the container. Advancing by `end` then skipped
                    # that remainder permanently -- it was never sent to ASR, and
                    # the chunk could still be published as complete.
                    extracted = media_duration(self.tools.ffprobe, audio,
                                               allow_scan=True)
                    requested = end - start
                    shortfall = requested - extracted
                    measured_reach = start + max(0.0, extracted)
                    if shortfall > SHORT_READ_TOLERANCE:
                        detail = (
                            f"audio short read at {start:.3f}s: requested "
                            f"{requested:.3f}s, measured {extracted:.3f}s "
                            f"({shortfall:.3f}s short)")
                        if closed:
                            self.store.update_chunk(
                                session, chunk, transcript_status=ERROR,
                                transcript_error=detail)
                            return AdvanceResult(
                                BLOCKED, covered_through=cursor,
                                expected=expected, detail=detail)
                        LOG.warning("%s/%s: %s; live media will be retried",
                                    session.channel, chunk.label, detail)
                        return AdvanceResult(
                            IDLE, covered_through=cursor, detail=detail)
                    if measured_reach <= cursor + COVERAGE_EPSILON:
                        detail = (
                            f"audio read made no progress beyond {cursor:.3f}s: "
                            f"requested {requested:.3f}s, measured "
                            f"{extracted:.3f}s")
                        if closed:
                            self.store.update_chunk(
                                session, chunk, transcript_status=ERROR,
                                transcript_error=detail)
                            return AdvanceResult(
                                BLOCKED, covered_through=cursor,
                                expected=expected, detail=detail)
                        return AdvanceResult(
                            IDLE, covered_through=cursor, detail=detail)
                    words = transcribe_audio(
                        self.provider(semantic), audio, extracted)
                finally:
                    audio.unlink(missing_ok=True)

                if extracted > 0:
                    reached = max(reached, start + extracted)

                shifted = [word.shifted(start) for word in words]
                seam = start if index > 0 or cursor == 0 else window_start
                before = len(existing)
                existing = merge_streams(existing, shifted, seam)
                added_total += max(0, len(existing) - before)

            # Never past what was actually read, and never backwards past what an
            # earlier pass already covered.
            covered = round(max(cursor, min(window_end, reached)), 3)
            # `expected` is probed, `covered` is what ffmpeg actually emitted, so
            # this is the tolerance comparison, not the epsilon one. c000 of the
            # 2026-08-16 recording read to 7200.842s of a container advertising
            # 7200.867s -- one AAC frame short, and unreachable by any further
            # pass -- so at 1 ms it never reached completion.
            complete = bool(final) and (
                expected - covered) <= SHORT_READ_TOLERANCE
            if complete:
                existing = clamp_words(existing, covered)
            self._save(session, chunk, existing, covered, expected,
                       complete=complete, semantic=semantic)
            self.store.update_chunk(
                session, chunk,
                transcribed_through=covered,
                word_count=len(existing),
                transcript_status=DONE if complete else RUNNING,
                transcript_error="",
            )
            LOG.info("%s/%s: transcribed through %.0fs of %.0fs (%d words)",
                     session.channel, chunk.label, covered,
                     expected or covered, len(existing))
        except TranscriptionError as exc:
            self.store.update_chunk(session, chunk, transcript_status=ERROR,
                                    transcript_error=str(exc))
            raise
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)

        return AdvanceResult(
            COMPLETE_ if complete else PROGRESSED,
            added_words=added_total,
            covered_through=covered,
            expected=expected,
        )

    def _finish(self, session: Session, chunk: Chunk, words: Sequence[Word],
                covered: float, expected: float,
                semantic: dict[str, object]) -> AdvanceResult:
        """Publish and mark done when there is no more audio to read.

        Covers the cases that used to slip through unmarked: a sub-second tail, a
        chunk with no speech at all, and a chunk whose final window had already
        been consumed by an earlier call.
        """
        covered = round(max(covered, 0.0), 3)
        if expected > 0:
            covered = min(covered, expected)
        if expected > 0 and (expected - covered) > SHORT_READ_TOLERANCE:
            # Audio exists that we could not read. Do not call this complete.
            # The tolerance keeps "could not read" meaning a real gap rather
            # than the sub-frame remainder no extraction can ever reach.
            self.store.update_chunk(session, chunk, transcript_status=ERROR,
                                    transcript_error=(
                                        f"stalled at {covered:.0f}s of {expected:.0f}s"))
            return AdvanceResult(BLOCKED, covered_through=covered, expected=expected,
                                 detail="media ended early or is unreadable")

        words = clamp_words(words, covered)
        self._save(session, chunk, words, covered, expected, complete=True,
                   semantic=semantic)
        self.store.update_chunk(session, chunk, transcribed_through=covered,
                                word_count=len(words), transcript_status=DONE,
                                transcript_error="")
        return AdvanceResult(COMPLETE_, covered_through=covered, expected=expected)

    def _save(self, session: Session, chunk: Chunk, words: Sequence[Word],
              covered: float, expected: float, *, complete: bool,
              semantic: dict[str, object] | None = None) -> None:
        semantic = dict(semantic or self._semantic_identity())
        if complete:
            words = clamp_words(words, covered)
        words_meta = {
            "channel": session.channel,
            "session_id": session.session_id,
            "chunk": chunk.label,
            "session_offset": chunk.session_offset,
            "language": str(semantic["language"]),
            "asr_identity": semantic,
            "updated_at": time.time(),
            # Recorded so a reader can tell a finished transcript from one that
            # merely stopped being updated.
            "covered_seconds": round(covered, 3),
            "expected_seconds": round(expected, 3),
            "complete": complete,
        }
        self._publish(session, chunk, words, final=complete,
                      words_meta=words_meta,
                      language=str(semantic["language"]))
        self._fresh_rebuilds.discard(self.words_path(session, chunk))

    def finalize(self, session: Session, chunk: Chunk) -> AdvanceResult:
        """Catch a chunk up to its full duration, then mark it complete.

        `advance()` moves one window per call, so a chunk that fell behind -- an
        API outage, a late key, queue congestion -- needs several. The loop is
        bounded by the work actually remaining and stops the moment a call fails
        to move the cursor, so a permanent failure cannot occupy a worker forever.
        """
        source = self.source_for(session, chunk)
        if source is None:
            return AdvanceResult(BLOCKED, detail="no media for this chunk")

        slice_seconds = max(1.0, float(
            self.config.get("transcription.slice_seconds", 300)))
        expected = self.expected_seconds(session, chunk, source)
        # Enough passes to cover the chunk, plus slack for the closing call.
        budget = int(expected / slice_seconds) + 3

        result = AdvanceResult(IDLE, covered_through=chunk.transcribed_through)
        for _ in range(budget):
            previous = float(chunk.transcribed_through)
            try:
                result = self.advance(session, chunk, final=True)
            except TranscriptionError as exc:
                # A provider outage is a bounded outcome here, not an exception to
                # propagate: the caller needs the worker back and a truthful state,
                # which `advance` has already recorded on the chunk.
                LOG.error("%s/%s: transcription provider failed: %s",
                          session.channel, chunk.label, exc)
                return AdvanceResult(BLOCKED, covered_through=previous,
                                     expected=expected, detail=str(exc))
            if result.status in (COMPLETE_, BLOCKED):
                return result
            if float(chunk.transcribed_through) <= previous:
                LOG.warning("%s/%s: transcription made no progress at %.0fs",
                            session.channel, chunk.label, previous)
                return AdvanceResult(BLOCKED, covered_through=previous,
                                     expected=expected,
                                     detail="no progress")
        LOG.warning("%s/%s: finalisation hit its pass budget at %.0fs of %.0fs",
                    session.channel, chunk.label, chunk.transcribed_through, expected)
        return AdvanceResult(BLOCKED, covered_through=chunk.transcribed_through,
                             expected=expected, detail="pass budget exhausted")

    def _publish(self, session: Session, chunk: Chunk,
                  words: Sequence[Word], *, final: bool,
                  words_meta: dict | None = None,
                  language: str | None = None) -> None:
        directory = self.output_dir(session, chunk)
        language = str(language or self.config.get("transcription.language", "en"))
        write_exports(
            directory,
            words,
            language=language,
            censor=self.censor(),
            meta={
                "channel": session.channel,
                "session_id": session.session_id,
                "chunk": chunk.label,
                "session_offset": chunk.session_offset,
                "complete": final,
                "source": chunk.master_name or chunk.ts_name,
            },
            words_meta=words_meta,
        )

    # -- rollback ----------------------------------------------------------

    def stash_words(self, session: Session, chunk: Chunk) -> Path | None:
        """Snapshot the current generation while a rebuild starts from zero."""
        path = self.words_path(session, chunk)
        if not path.exists():
            return None
        # Also resolves a process that died during the preceding publication.
        load_words(path)
        stash = path.with_name(path.name + ".previous")
        stash.unlink(missing_ok=True)
        shutil.copyfile(path, stash)
        backup = stash.with_name("generation.previous")
        shutil.rmtree(backup, ignore_errors=True)
        backup.mkdir()
        for name in GENERATION_FILES:
            current = path.parent / name
            if current.exists():
                shutil.copyfile(current, backup / name)
        self._fresh_rebuilds.add(path)
        return stash

    def restore_words(self, session: Session, chunk: Chunk,
                      stash: Path | None) -> None:
        """Put a stashed transcript back and re-publish its exports.

        Both halves matter. Restoring the words file alone would leave the export
        set describing the failed rebuild, which is what Premiere actually reads.
        """
        if stash is None or not stash.exists():
            return
        path = self.words_path(session, chunk)
        backup = stash.with_name("generation.previous")
        if not backup.exists():
            raise RuntimeError("previous transcript generation backup is missing")
        rendered = {
            name: (backup / name).read_text(encoding="utf-8")
            for name in GENERATION_FILES
            if (backup / name).exists()
        }
        publish_text_sets([(path.parent, rendered, GENERATION_FILES)])
        words, meta = load_words(path)
        self.store.update_chunk(
            session, chunk,
            transcribed_through=float(meta.get("covered_seconds") or 0.0),
            word_count=len(words),
            transcript_status=DONE if meta.get("complete") else ERROR,
        )
        self._fresh_rebuilds.discard(path)
        stash.unlink(missing_ok=True)
        shutil.rmtree(backup, ignore_errors=True)
        LOG.info("%s/%s: restored the previous transcript (%d words)",
                 session.channel, chunk.label, len(words))

    def discard_stash(self, stash: Path | None) -> None:
        if stash is not None:
            self._fresh_rebuilds.discard(stash.with_name("words.json"))
            stash.unlink(missing_ok=True)
            shutil.rmtree(stash.with_name("generation.previous"),
                          ignore_errors=True)

    def republish(self, session: Session, chunk: Chunk) -> int:
        """Rewrite the export set from whatever the words file currently holds."""
        words, meta = load_words(self.words_path(session, chunk))
        self._publish(session, chunk, words, final=bool(meta.get("complete")),
                      words_meta=meta, language=str(meta.get("language") or "en"))
        return len(words)

    # -- chunk boundaries --------------------------------------------------

    def stitch_with_previous(self, session: Session, chunk: Chunk, *,
                             strict: bool = False) -> bool:
        """Repair the words spoken across the boundary into this chunk.

        Chunks are separate files transcribed independently, so a word spoken
        across the join is heard by neither side in full. This transcribes a short
        passage built from the end of the previous chunk and the start of this one
        and hands each word to whichever chunk it was mostly spoken in.

        Returns True if either transcript changed. Normal finalisation treats a
        failed optional repair as a no-op. Manual retranscription passes
        ``strict=True`` because discarding a previously repaired seam is not a
        successful rebuild; provider/build/empty/publication failures then raise.
        """
        if chunk.index <= 0:
            return False
        if not self.config.get("transcription.stitch_chunk_boundaries", True):
            return False

        previous = session.chunk(chunk.index - 1)
        if previous is None:
            return False

        previous_source = self.source_for(session, previous)
        current_source = self.source_for(session, chunk)
        if previous_source is None or current_source is None:
            if strict:
                raise RuntimeError(
                    f"cannot repair {previous.label}/{chunk.label}: boundary "
                    "media is missing")
            return False

        previous_words, previous_meta = load_words(self.words_path(session, previous))
        current_words, current_meta = load_words(self.words_path(session, chunk))
        if not previous_meta.get("complete") or not current_meta.get("complete"):
            # Stitching a transcript that is still moving would be overwritten by
            # the pass that finishes it. This is "not yet applicable", never a
            # failure -- including under strict. A manual retranscription of one
            # chunk must not report itself failed merely because its neighbour is
            # still being transcribed; the seam runs on its own once both sides are
            # complete. (Genuine strict failures below -- missing media, empty
            # audio, no seam words, mismatched semantics -- still raise.)
            return False

        lead = float(self.config.get("transcription.seam_seconds", 6.0))
        previous_duration = float(previous_meta.get("covered_seconds")
                                  or previous.duration or 0.0)
        current_duration = float(current_meta.get("covered_seconds")
                                 or chunk.duration or 0.0)
        pivot = min(lead, previous_duration)
        following_lead = min(lead, current_duration)
        if (pivot <= COVERAGE_EPSILON
                or following_lead <= COVERAGE_EPSILON):
            return False

        work_dir = (self.config.work_root / "seam" /
                    f"{session.session_id}_{chunk.label}_{secrets.token_hex(3)}")
        work_dir.mkdir(parents=True, exist_ok=True)
        # Where the seam audio begins on the previous chunk's clock. Held onto
        # rather than recomputed later: if the slice comes back shorter than was
        # asked for, this is still where it started.
        seam_start = max(0.0, previous_duration - pivot)
        try:
            previous_semantic = previous_meta.get("asr_identity")
            current_semantic = current_meta.get("asr_identity")
            previous_semantic = (
                self._frozen_source_semantic(previous_source, previous_semantic)
                if isinstance(previous_semantic, dict)
                else self._source_semantic(previous_source))
            current_semantic = (
                self._frozen_source_semantic(current_source, current_semantic)
                if isinstance(current_semantic, dict)
                else self._source_semantic(current_source))
            if previous_semantic != current_semantic:
                message = (f"{previous.label}/{chunk.label} boundary transcripts "
                           "use different frozen ASR semantics or audio tracks")
                if strict:
                    raise RuntimeError(message)
                LOG.warning("%s/%s: %s; rebuild required",
                            session.channel, chunk.label, message)
                return False

            tail = extract_audio_slice(
                self.tools, previous_source, work_dir / "tail.flac",
                seam_start, pivot,
                audio_stream=self._audio_selector(previous_semantic))
            head = extract_audio_slice(
                self.tools, current_source, work_dir / "head.flac",
                0.0, following_lead,
                audio_stream=self._audio_selector(current_semantic))

            # The pivot has to be what the tail slice actually contains, not what
            # was asked for. A chunk whose media is a little shorter than its
            # recorded duration would otherwise shift every seam word.
            actual_pivot = media_duration(self.tools.ffprobe, tail, allow_scan=True)
            actual_following = media_duration(
                self.tools.ffprobe, head, allow_scan=True)
            if actual_pivot <= 0 or actual_following <= 0:
                if strict:
                    raise RuntimeError(
                        f"{previous.label}/{chunk.label} boundary audio was empty")
                return False

            seam_audio = concat_audio(self.tools, [tail, head],
                                      work_dir / "seam.flac")
            submitted_duration = media_duration(
                self.tools.ffprobe, seam_audio, allow_scan=True)
            if submitted_duration <= 0:
                if strict:
                    raise RuntimeError(
                        f"{previous.label}/{chunk.label} joined boundary audio "
                        "was empty")
                return False
            seam_words = transcribe_audio(
                self.provider(previous_semantic), seam_audio,
                submitted_duration)
        except TranscriptionError as exc:
            if strict:
                raise
            LOG.warning("%s/%s: boundary transcription failed, leaving both "
                        "transcripts as they are: %s",
                        session.channel, chunk.label, exc)
            return False
        except Exception as exc:
            if strict:
                raise RuntimeError(
                    f"could not repair {previous.label}/{chunk.label} boundary: "
                    f"{exc}") from exc
            LOG.warning("%s/%s: could not build the boundary audio: %s",
                        session.channel, chunk.label, exc)
            return False
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)

        if not seam_words:
            if strict:
                raise RuntimeError(
                    f"{previous.label}/{chunk.label} boundary transcription "
                    "returned no words")
            return False

        new_previous, new_current = stitch_seam(
            previous_words, current_words, seam_words,
            seam_start=seam_start,
            pivot=actual_pivot,
            following_lead=actual_following,
        )

        previous_covered = float(previous_meta.get("covered_seconds")
                                 or previous_duration)
        current_covered = float(current_meta.get("covered_seconds")
                                or current_duration)
        new_previous = clamp_words(new_previous, previous_covered)
        new_current = clamp_words(new_current, current_covered)
        previous_changed = _differs(new_previous, previous_words)
        current_changed = _differs(new_current, current_words)
        changed = previous_changed or current_changed

        publications = []
        for target, revised, old_meta, did_change in (
            (previous, new_previous, previous_meta, previous_changed),
            (chunk, new_current, current_meta, current_changed),
        ):
            if not did_change:
                continue
            words_meta = {
                **old_meta,
                "updated_at": time.time(),
                "complete": True,
            }
            language = str(words_meta.get("language") or "en")
            publications.append((
                self.output_dir(session, target),
                revised,
                {
                    "language": language,
                    "censor": self.censor(),
                    "meta": {
                        "channel": session.channel,
                        "session_id": session.session_id,
                        "chunk": target.label,
                        "session_offset": target.session_offset,
                        "complete": True,
                        "source": target.master_name or target.ts_name,
                    },
                    "words_meta": words_meta,
                },
            ))

        if publications:
            try:
                # Both directories are fully rendered and staged before either
                # generation is touched. The shared journal restores both if any
                # replace or retirement fails.
                write_export_sets(publications)
            except Exception as exc:
                if strict:
                    raise
                LOG.warning("%s/%s: could not publish stitched generations; "
                            "leaving both unchanged: %s",
                            session.channel, chunk.label, exc)
                return False
            if previous_changed:
                self.store.update_chunk(session, previous,
                                        word_count=len(new_previous))
            if current_changed:
                self.store.update_chunk(session, chunk,
                                        word_count=len(new_current))

        if changed:
            LOG.info("%s: stitched the %s/%s boundary (%d seam words)",
                     session.channel, previous.label, chunk.label, len(seam_words))
        return changed

    # -- one-shot ----------------------------------------------------------

    def transcribe_file(self, source: Path, output_dir: Path, *,
                        language: str | None = None) -> list[Word]:
        """Transcribe a standalone file (a snapshot) and write its exports."""
        language = str(self.config.get("transcription.language", "en")
                       if language is None else language)
        if not source.is_file():
            raise RuntimeError(f"cannot transcribe unreadable source: {source}")
        duration = live_duration(self.tools, source, allow_scan=True)
        if duration <= 0:
            raise RuntimeError(
                f"cannot transcribe {source.name}: media is unreadable or has "
                "zero duration")
        slice_seconds = float(self.config.get("transcription.slice_seconds", 300))
        if slice_seconds <= 0:
            raise RuntimeError("slice_seconds must be positive")
        overlap = float(self.config.get("transcription.overlap_seconds", 3.0))
        semantic = self._source_semantic(source, language)

        # Second resolution collided when two one-shot jobs started together.
        work_dir = (self.config.work_root /
                    f"oneshot_{int(time.time())}_{secrets.token_hex(3)}")
        work_dir.mkdir(parents=True, exist_ok=True)
        words: list[Word] = []
        try:
            cursor = 0.0
            budget = max(1, int(math.ceil(duration / slice_seconds)) + 5)
            passes = 0
            # The same measurement noise that SHORT_READ_TOLERANCE covers applies
            # to the last slice of a snapshot: chasing the final few milliseconds
            # of a container whose audio track is fractionally shorter produces a
            # sub-frame request that can only fail the no-progress check.
            while cursor < duration - SHORT_READ_TOLERANCE:
                passes += 1
                if passes > budget:
                    raise RuntimeError(
                        f"one-shot transcription made insufficient progress at "
                        f"{cursor:.3f}s of {duration:.3f}s")
                end = min(duration, cursor + slice_seconds)
                start = max(0.0, cursor - overlap) if cursor > 0 else 0.0
                requested = end - start
                if requested <= 0:
                    raise RuntimeError(
                        f"one-shot transcription made no progress at {cursor:.3f}s")
                audio = work_dir / f"slice_{int(start * 1000):012d}.flac"
                try:
                    extract_audio_slice(self.tools, source, audio, start,
                                        requested,
                                        audio_stream=self._audio_selector(semantic))
                    extracted = media_duration(self.tools.ffprobe, audio,
                                               allow_scan=True)
                    if extracted <= 0:
                        raise RuntimeError(
                            f"audio extraction made no progress at {start:.3f}s; "
                            f"{source.name} may have no readable audio")
                    shortfall = requested - extracted
                    if shortfall > SHORT_READ_TOLERANCE:
                        raise RuntimeError(
                            f"short audio read at {start:.3f}s: requested "
                            f"{requested:.3f}s, extracted {extracted:.3f}s")
                    reached = min(duration, start + extracted)
                    if reached <= cursor + COVERAGE_EPSILON:
                        raise RuntimeError(
                            f"audio extraction made no progress beyond "
                            f"{cursor:.3f}s of {duration:.3f}s")
                    part = transcribe_audio(
                        self.provider(semantic), audio, extracted)
                finally:
                    audio.unlink(missing_ok=True)
                words = merge_streams(words, [w.shifted(start) for w in part], start)
                # Coverage advances only by audio ffprobe measured in the file
                # actually sent to ASR, never by the requested endpoint.
                cursor = reached
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)

        if duration - cursor > SHORT_READ_TOLERANCE:
            raise RuntimeError(
                f"one-shot transcription stopped at {cursor:.3f}s of "
                f"{duration:.3f}s")
        words = clamp_words(words, duration)
        words_meta = {
            "source": source.name,
            "language": language,
            "asr_identity": semantic,
            "complete": True,
            "covered_seconds": round(cursor, 3),
            "expected_seconds": round(duration, 3),
        }
        write_exports(output_dir, words, language=language, censor=self.censor(),
                       meta={"source": source.name, "complete": True},
                       words_meta=words_meta)
        return words
