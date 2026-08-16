"""Early cut: pull a range out of a broadcast while it is still being recorded.

Strictly non-destructive. The recorder's ffmpeg keeps writing its .ts untouched;
we only ever open that file for reading. A range that straddles a chunk boundary
is cut from both chunks and joined, so "the last 20 minutes" works even when the
current chunk is two minutes old.
"""

from __future__ import annotations

import secrets
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, ContextManager, Iterable, Iterator

from .config import Config
from .media import (
    cut_and_join,
    live_duration,
    validate_media_coverage,
    video_dimensions,
)
from .state import RECORDING, Chunk, Session
from .util import LOG, Tools, fmt_clock, slugify

# AUD2-018: a generous but firm bitrate ceiling for a precise (CRF-18) re-encode,
# used both to size the disk reservation and as the ffmpeg `-fs` output cap so the
# re-encode can never exceed what was reserved. 40 Mbit/s comfortably exceeds
# CRF-18 at 1080p60; it is scaled up by pixel count for higher resolutions so the
# cap stays a true upper bound. Audio is bounded separately.
PRECISE_VIDEO_BPS_1080 = 40_000_000
PRECISE_AUDIO_BPS = 256_000
_REFERENCE_PIXELS = 1920 * 1080


def precise_output_cap(tools: Tools, parts: Sequence[tuple[Path, float, float]]) -> int:
    """Upper bound in bytes for one precise re-encode covering `parts`.

    Sized for the whole requested duration at the ceiling above, so applying it as
    a per-piece `-fs` limit never truncates a legitimate cut (each piece is only a
    fraction of the duration) while still capping a pathological high-bitrate
    re-encode at the reserved bytes.
    """
    total = sum(max(0.0, end - start) for _, start, end in parts)
    width = height = 0
    if parts:
        width, height = video_dimensions(tools, parts[0][0])
    pixels = max(1, int(width) * int(height))
    scale = max(1.0, pixels / _REFERENCE_PIXELS)
    payload = total * (PRECISE_VIDEO_BPS_1080 * scale + PRECISE_AUDIO_BPS) / 8.0
    overhead = max(8 * 1024 * 1024, payload * 0.02)
    return int(payload + overhead)

# Chunk boundaries land on keyframes and durations come from the muxer, so two
# adjacent chunks never abut to the millisecond. A hole smaller than this is
# muxer arithmetic; anything larger is missing media.
GAP_TOLERANCE_SECONDS = 0.5

# A flat two-second allowance accepted almost-empty short requests. Scale the
# allowance with the request while retaining one low-frame-rate frame of probe
# slop and the old absolute ceiling for long cuts.
SNAPSHOT_SHORTFALL_FRACTION = 0.02
MIN_SNAPSHOT_SHORTFALL = 1.0 / 15.0
MAX_SNAPSHOT_SHORTFALL = 2.0


def allowed_snapshot_shortfall(requested_duration: float) -> float:
    if requested_duration <= 0:
        return MIN_SNAPSHOT_SHORTFALL
    return max(
        MIN_SNAPSHOT_SHORTFALL,
        min(MAX_SNAPSHOT_SHORTFALL,
            requested_duration * SNAPSHOT_SHORTFALL_FRACTION),
    )


@dataclass
class SnapshotRequest:
    session_id: str
    # Either the last N minutes of the broadcast, or an explicit session-relative
    # range in seconds.
    last_minutes: float | None = None
    start: float | None = None
    end: float | None = None
    precise: bool = False
    transcribe: bool = True
    name: str = ""


@dataclass
class SnapshotResult:
    path: Path
    start: float
    end: float
    spans: list[str] = field(default_factory=list)
    transcript_dir: Path | None = None
    # What the finished file actually holds, probed after the cut. The requested
    # range is what was asked for; these are not the same number and reporting the
    # request as though it were the result hid short exports completely.
    actual_duration: float = 0.0
    transcript_status: str = "skipped"
    transcript_error: str = ""

    @property
    def requested_duration(self) -> float:
        return max(0.0, self.end - self.start)

    def to_dict(self) -> dict[str, Any]:
        # No fallback to the requested duration. A zero probe means the cut is
        # unreadable, and substituting the request there reported a broken file
        # as a perfect one; create() now refuses to return such a result at all,
        # so this is simply the measured truth.
        return {
            "file": self.path.name,
            "path": str(self.path),
            "start": round(self.start, 3),
            "end": round(self.end, 3),
            "duration": round(self.actual_duration, 3),
            "requested_duration": round(self.requested_duration, 3),
            "clock": f"{fmt_clock(self.start)}-{fmt_clock(self.end)}",
            "spans": self.spans,
            "transcript_dir": str(self.transcript_dir) if self.transcript_dir else "",
            "transcript_status": self.transcript_status,
            "transcript_error": self.transcript_error,
        }


@contextmanager
def _no_lease(paths: Iterable[Path]) -> Iterator[None]:
    yield


class SnapshotService:
    """Cuts ranges out of a session's media without disturbing the recording.

    `lease` is how the service tells the pipeline "do not delete these files yet".
    Chunk .ts working copies are reclaimed as soon as their master validates, and
    that could previously happen while a snapshot was midway through reading one.
    """

    def __init__(self, config: Config, tools: Tools,
                 lease: Callable[[Iterable[Path]], ContextManager[Any]] | None = None) -> None:
        self.config = config
        self.tools = tools
        self.lease = lease or _no_lease

    # -- geometry ----------------------------------------------------------

    def chunk_span(self, session: Session, chunk: Chunk) -> tuple[Path | None, float, float]:
        """Where this chunk sits on the session timeline, and what file holds it.

        A closed chunk is read from its master by preference. The master is
        validated and permanent; the .ts beside it is a working copy that the
        pipeline reclaims the moment the remux succeeds. Preferring the .ts meant
        a snapshot could be reading a file that was about to be deleted, for no
        benefit -- both hold the same video.
        """
        live = session.path / "live" / chunk.ts_name
        master = session.path / "master" / chunk.master_name

        if chunk.status == RECORDING:
            # Only the .ts is readable while ffmpeg is still appending to it.
            source = live if live.exists() else None
        else:
            source = master if master.exists() else (live if live.exists() else None)
        if source is None:
            return None, 0.0, 0.0

        if chunk.status == RECORDING or chunk.duration <= 0:
            duration = live_duration(self.tools, source)
        else:
            duration = chunk.duration
        return source, chunk.session_offset, duration

    def readable_sources(self, session: Session) -> list[Path]:
        """Every candidate a cut might open, including paths not present yet.

        The absent names matter: locking only files that passed ``exists()``
        leaves a check-then-create gap where remux can publish/reclaim a path
        after the snapshot chose its lease set but before it plans the cut.
        """
        sources: list[Path] = []
        for chunk in session.chunks:
            for candidate in (session.path / "live" / chunk.ts_name,
                              session.path / "master" / chunk.master_name):
                if chunk.ts_name and candidate.parent.name == "live":
                    sources.append(candidate)
                elif chunk.master_name and candidate.parent.name == "master":
                    sources.append(candidate)
        return sources

    def session_extent(self, session: Session) -> float:
        """How much broadcast time exists right now, in session-relative seconds."""
        extent = 0.0
        for chunk in session.chunks:
            source, offset, duration = self.chunk_span(session, chunk)
            if source is not None:
                extent = max(extent, offset + duration)
        return extent

    def resolve_range(self, session: Session,
                      request: SnapshotRequest) -> tuple[float, float]:
        extent = self.session_extent(session)
        if extent <= 0:
            raise RuntimeError("nothing has been recorded yet")

        if request.last_minutes is not None:
            span = max(1.0, float(request.last_minutes) * 60.0)
            # The safety margin exists because a live file's final packet may be
            # half-written. A finished session has no write head, so applying it
            # there simply threw away the last two seconds of the broadcast.
            end = max(0.0, extent - 2.0) if self._is_live(session) else extent
            return max(0.0, end - span), end

        start = max(0.0, float(request.start or 0.0))
        if request.end is None:
            # Open-ended: take everything, minus the live write-head margin.
            end = max(0.0, extent - 2.0) if self._is_live(session) else extent
        else:
            end = float(request.end)
            if self._is_live(session):
                # Never read past the half-written live edge, even for an explicit
                # end. On a live session extent only grows, so a frozen queued
                # range is unaffected by this.
                end = min(end, max(0.0, extent - 2.0))
            # AUD2-066: a finished session's explicit end is NOT clamped to the
            # current extent. Clamping silently shortened a frozen queued or
            # recovered range whose media is no longer fully present; leaving it
            # unclamped lets plan() refuse it end-to-end instead of quietly
            # returning a shorter file than was asked for.
        if end <= start:
            raise ValueError("snapshot end must be after start")
        return start, end

    def _is_live(self, session: Session) -> bool:
        return session.status == RECORDING

    # -- export ------------------------------------------------------------

    def plan(self, session: Session, start: float,
             end: float) -> tuple[list[tuple[Path, float, float]], list[str]]:
        """Which pieces of which files cover [start, end), proving there are no holes.

        Raises rather than returning a short file. Concatenating whatever happened
        to overlap meant a session missing a chunk in the middle produced a
        snapshot that silently jumped, and the only clue was a duration nobody
        checked.
        """
        parts: list[tuple[Path, float, float]] = []
        spans: list[str] = []
        cursor = start

        for chunk in sorted(session.chunks, key=lambda item: item.index):
            source, offset, duration = self.chunk_span(session, chunk)
            if source is None or duration <= 0:
                continue
            overlap_start = max(start, offset)
            overlap_end = min(end, offset + duration)
            if overlap_end - overlap_start <= 0.05:
                continue

            if overlap_start > cursor + GAP_TOLERANCE_SECONDS:
                raise RuntimeError(
                    f"no recorded media between {fmt_clock(cursor)} and "
                    f"{fmt_clock(overlap_start)}; the requested range is not "
                    "covered end to end")

            parts.append((source, overlap_start - offset, overlap_end - offset))
            spans.append(chunk.label)
            cursor = max(cursor, overlap_end)

        if not parts:
            raise RuntimeError("the requested range does not overlap any recorded media")
        if end - cursor > GAP_TOLERANCE_SECONDS:
            raise RuntimeError(
                f"the recording covers only up to {fmt_clock(cursor)}, short of "
                f"the requested {fmt_clock(end)}")
        return parts, spans

    def create(self, session: Session, request: SnapshotRequest) -> SnapshotResult:
        # The lease is taken before the geometry is measured, not just around the
        # cut: chunk_span() probes these same files, and a .ts reclaimed between
        # measuring and cutting would fail the cut with an obscure ffmpeg error.
        with self.lease(self.readable_sources(session)):
            start, end = self.resolve_range(session, request)
            parts, spans = self.plan(session, start, end)

            stamp = time.strftime("%H%M%S", time.localtime())
            token = secrets.token_hex(2)
            label = slugify(request.name) if request.name else "snap"
            # The token stops two cuts made in the same second -- or two given the
            # same name -- from overwriting one another.
            filename = (f"{session.channel}_{session.session_id}_{label}_"
                        f"{fmt_clock(start).replace(':', '')}_{stamp}_{token}.mp4")
            destination = session.path / "snapshots" / filename

            LOG.info("%s: snapshot %s-%s from %s",
                     session.channel, fmt_clock(start), fmt_clock(end), ", ".join(spans))
            cut_and_join(
                self.tools, parts, destination,
                precise=request.precise,
                # A directory per cut: the shared path meant one snapshot's cleanup
                # deleted another's parts mid-encode.
                work_dir=(self.config.work_root / "snapshot" /
                          f"{session.session_id}_{stamp}_{token}"),
                # AUD2-018: bound a precise re-encode to the same firm ceiling the
                # disk reservation was sized from, so it cannot exceed it.
                max_output_bytes=(precise_output_cap(self.tools, parts)
                                  if request.precise else None),
            )

        # AUD2-020: prove the file is real before calling the cut a success.
        # ffmpeg can exit zero having written a nonempty file with no decodable
        # video, and an unreadable output probes as zero duration -- which
        # `to_dict()` then replaced with the *requested* duration, so a failed cut
        # was reported to the dashboard as a normal snapshot of exactly the length
        # asked for.
        if not destination.exists() or destination.stat().st_size < 1024:
            raise RuntimeError(f"snapshot produced no usable file ({destination.name})")
        try:
            # Use the shortest required stream, not aggregate container duration:
            # full video cannot hide a truncated language/commentary track.
            actual = validate_media_coverage(
                self.tools, destination, label="snapshot")
        except Exception as exc:
            destination.unlink(missing_ok=True)
            raise RuntimeError(
                f"snapshot {destination.name} is not usable: {exc}") from exc
        requested = max(0.0, end - start)
        allowance = allowed_snapshot_shortfall(requested)
        if requested - actual > allowance:
            quarantine = destination.with_name(
                f"{destination.stem}.partial-shortfall.mp4")
            destination.replace(quarantine)
            raise RuntimeError(
                f"snapshot was incomplete: {actual:.1f}s of {requested:.1f}s; "
                f"shortfall exceeds the {allowance:.3f}s allowance; partial "
                f"output retained as {quarantine.name}")

        return SnapshotResult(path=destination, start=start, end=end, spans=spans,
                              actual_duration=actual)
