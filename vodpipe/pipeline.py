"""Orchestration: recorder events in, background work out.

The recorder emits two events -- a chunk opened, a chunk closed -- and a ticker
advances rolling transcription for whatever is currently recording. Everything
expensive (remux, proxy transcode, ASR, summary) runs on the job pool so the
capture thread is never the thing waiting.
"""

from __future__ import annotations

import json
import re
import secrets
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator

from .channels import InvalidVod, parse_channel, parse_vod, vod_dir_name
from .config import Config
from .disk import DiskBudget, DiskReservation
from .exports import (
    EDIT_OWNED,
    GENERATION_FILES,
    SOURCE_DIR,
    publication_is_consistent,
    split_publication,
    write_exports,
)
from .jobs import CANCELLED as JOB_CANCELLED
from .jobs import QUEUED as JOB_QUEUED
from .jobs import RUNNING as JOB_RUNNING
from .jobs import STOPPED as POOL_STOPPED
from .jobs import Job, JobRunner
from .locks import (
    ChannelBusy,
    ChannelLock,
    ResourceBusy,
    ResourceLock,
    chunk_lock_path,
    media_lock_path,
    session_lock_path,
)
from .media import (
    allowed_shortfall,
    estimate_proxy_peak_bytes,
    live_duration,
    make_proxy,
    oauth_args,
    probe_encoder,
    proxy_args,
    remux_to_mp4,
    validate_master,
    validate_proxy,
    verify_master_readable,
    video_dimensions,
)
from .models import PROVIDER_SECRETS
from .recorder import Recorder
from .snapshot import (
    SnapshotRequest,
    SnapshotResult,
    SnapshotService,
    precise_output_cap,
)
from .state import (
    COMPLETE,
    DONE,
    ERROR,
    FAILED,
    INTERRUPTED,
    PENDING,
    RECORDING,
    REMUXING,
    RUNNING,
    SKIPPED,
    SOURCE_VOD,
    STARTING,
    Chunk,
    ControlStateStore,
    Session,
    SessionStore,
)
from .summarize import (
    build_header,
    build_model_input,
    build_summarizer,
    rundown_generation,
    write_rundown,
)
from .transcribe import SHORT_READ_TOLERANCE, RollingTranscriber
from .transcript import load_words, publish_text_sets
from .util import (
    LOG,
    Tools,
    atomic_write_json,
    atomic_write_text,
    fmt_clock,
    free_bytes,
    human_bytes,
    redact,
    resolve_tools,
    run,
    safe_name_component,
)


PIPELINE_RUNNING = "running"
DRAINING = "draining"
STOPPED = "stopped"

# Hard subprocess ceilings in media.py. A proxy job may first try a hardware
# encoder and then legally fall back to software, after probing three encoders.
# Keep lifecycle budgets here explicit because shutdown owns the worker threads,
# while media.py owns the subprocesses running on them.
MEDIA_SUBPROCESS_TIMEOUT = 14_400.0
ENCODER_PROBE_TIMEOUT = 90.0
MEDIA_OPERATION_TIMEOUT = (
    2 * MEDIA_SUBPROCESS_TIMEOUT + 3 * ENCODER_PROBE_TIMEOUT + 300.0
)

LIVE = "live"
OFFLINE = "offline"
UNKNOWN = "unknown"


@dataclass
class _RecordingRequest:
    request_id: str
    channel: str
    requested_at: float
    attempt_session_id: str = ""
    last_error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "channel": self.channel,
            "requested_at": self.requested_at,
            "attempt_session_id": self.attempt_session_id,
            "last_error": self.last_error,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "_RecordingRequest":
        return cls(
            request_id=str(data["request_id"]),
            channel=str(data["channel"]),
            requested_at=float(data["requested_at"]),
            attempt_session_id=str(data.get("attempt_session_id") or ""),
            last_error=str(data.get("last_error") or ""),
        )


@dataclass
class _RecordingResult:
    request_id: str
    channel: str
    status: str
    session_id: str = ""
    error: str = ""
    completed_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "channel": self.channel,
            "status": self.status,
            "session_id": self.session_id,
            "error": self.error,
            "completed_at": self.completed_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "_RecordingResult":
        return cls(
            request_id=str(data["request_id"]),
            channel=str(data["channel"]),
            status=str(data["status"]),
            session_id=str(data.get("session_id") or ""),
            error=str(data.get("error") or ""),
            completed_at=float(data["completed_at"]),
        )


@dataclass(frozen=True)
class _SummarySource:
    generation: str
    body: str


class _OwnershipGroup:
    """Hold one kernel chunk lock until every submitted mutation finishes."""

    def __init__(self, lock: ResourceLock,
                 released: Callable[["_OwnershipGroup"], None]) -> None:
        self.lock = lock
        self._released_callback = released
        self._guard = threading.Lock()
        self._next = 1
        # Token zero belongs to the caller constructing and populating the group.
        self._tokens: dict[int, Job | None] = {0: None}
        self._released = False

    def submit(self, pool: JobRunner, key: str, label: str, kind: str,
               work: Callable[[Job], None]) -> Job | None:
        token = self.retain()
        if token is None:
            return None

        def owned(job: Job) -> None:
            try:
                work(job)
            finally:
                self._finish(token)

        job = pool.submit(key, label, kind, owned)
        if job is None:
            self._finish(token)
            return None
        with self._guard:
            if token in self._tokens:
                self._tokens[token] = job
        return job

    def populated(self) -> None:
        self._finish(0)

    def retain(self) -> int | None:
        with self._guard:
            if self._released:
                return None
            token = self._next
            self._next += 1
            self._tokens[token] = None
            return token

    def finish(self, token: int) -> None:
        self._finish(token)

    def release_cancelled(self) -> None:
        with self._guard:
            cancelled = [token for token, job in self._tokens.items()
                         if job is not None and job.status == JOB_CANCELLED]
        for token in cancelled:
            self._finish(token)

    def _finish(self, token: int) -> None:
        release = False
        with self._guard:
            self._tokens.pop(token, None)
            if not self._tokens and not self._released:
                self._released = True
                release = True
        if release:
            self.lock.release()
            self._released_callback(self)


class _ProbeRunner(JobRunner):
    """Persistent fixed-worker runner with a hard outstanding-probe bound."""

    def __init__(self, workers: int = 4, capacity: int = 64) -> None:
        self._capacity = max(workers, capacity)
        self._admission = threading.Lock()
        super().__init__(workers=workers)

    def submit(self, key: str, label: str, kind: str,
               work: Callable[[Job], None]) -> Job | None:
        with self._admission:
            existing = self.get(key)
            if existing is not None and existing.status in (JOB_QUEUED, JOB_RUNNING):
                return None
            if self.active_count() >= self._capacity:
                LOG.warning("refusing probe %s: probe capacity %d is full",
                            key, self._capacity)
                return None
            return super().submit(key, label, kind, work)


class Pipeline:
    _POOL_WORKERS = (3, 2, 2, 4)
    _PROBE_CAPACITY = 64

    def __init__(self, config: Config) -> None:
        self.config = config
        self.tools: Tools = resolve_tools({
            key: value for key, value in (config.get("tools") or {}).items() if value
        })
        self.store = SessionStore(config.masters_root)
        self.store.load_from_disk()

        # Separate persistent pools because the work has different deadlines and
        # a single FIFO let the slowest kind block the most urgent.
        # A 15-minute rundown sitting in front of a rolling transcription slice
        # meant the transcript fell behind the recording it was supposed to track.
        #
        #   jobs      capture-critical: rolling ASR, chunk finalisation, remux
        #   media     heavy and disposable: proxy transcodes, rundowns
        #   cuts      user-initiated: snapshots must come back in seconds
        #   probes    bounded network/tool checks; never capture-critical
        self._create_pools()

        self.transcriber = RollingTranscriber(config, self.tools, self.store)
        self.snapshots = SnapshotService(config, self.tools, lease=self.read_lease)

        self._recorders: dict[str, Recorder] = {}
        # VOD downloads are keyed by their lock id (vod-<id>), not by channel, so a
        # download never collides with a live recording of the same channel and the
        # live-channel machinery (arming, the watcher) never touches one.
        self._vod_recorders: dict[str, Recorder] = {}
        self._control_store = ControlStateStore(config.masters_root)
        control = self._control_store.load()
        # Explicit intent remains pending through recorder startup and is consumed
        # only by the first-media callback carrying this exact request id.
        self._armed: dict[str, _RecordingRequest] = {
            request.channel: request
            for request in (_RecordingRequest.from_dict(item)
                            for item in control["requests"])
        }
        self._request_results: dict[str, _RecordingResult] = {
            result.request_id: result
            for result in (_RecordingResult.from_dict(item)
                           for item in control["results"])
        }
        # Channels the user explicitly stopped while they were still live.
        # Without this, Stop on a watched channel with auto_record on was undone
        # by the very next watcher pass -- the channel was still broadcasting and
        # still auto-enabled, so it started straight back up and the user
        # appeared unable to stop recording at all. Cleared when the channel is
        # next seen offline (that broadcast is over) or on an explicit Record.
        # Deliberately separate from the persistent auto_record preference: this
        # is about one broadcast, not about what the user wants in general.
        self._auto_suppressed: set[str] = set(control["auto_suppressed"])
        self._chunk_locks: dict[str, threading.RLock] = {}
        self._locks_guard = threading.Lock()
        # Files a snapshot is currently reading, and .ts files whose deletion is
        # waiting on one. See read_lease() and _reclaim_ts().
        self._leases: dict[str, int] = {}
        self._lease_kernel: dict[str, ResourceLock] = {}
        self._deferred_deletes: set[str] = set()
        self._lease_guard = threading.Lock()
        self._ownership_groups: set[_OwnershipGroup] = set()
        self._ownership_guard = threading.Lock()
        self._externally_owned_sessions: set[str] = set()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._live_status: dict[str, dict[str, Any]] = {}
        self._last_retention_sweep = 0.0
        # Serialises start/stop/shutdown against each other. Without it two
        # requests, or a request racing the watcher, could both pass the
        # "already recording?" check and launch duplicate recorders.
        self._lifecycle = threading.RLock()
        # Lifecycle state is published under `_lifecycle`, but joins must happen
        # without it: a live probe can need that lock to observe `_stop` and exit.
        # This second guard serialises the physical teardown itself.
        self._shutdown_guard = threading.Lock()
        self._request_changed = threading.Condition(self._lifecycle)
        self._snapshot_index_lock = threading.Lock()
        # Serialises the "is there room?" check against the queueing that follows.
        self._snapshot_admission = threading.Lock()
        self._snapshot_reservations: dict[str, DiskReservation] = {}
        self._snapshot_reservation_guard = threading.Lock()
        self.disk_budget = DiskBudget(
            config.masters_root,
            lambda: int(float(self.config.get(
                "recording.hard_reserve_gb", 10)) * (1024 ** 3)),
        )
        self._state = STOPPED
        self._lifecycle_error = ""
        # Retained for callers from earlier builds; lifecycle_state is authority.
        self._running = False

    # -- lifecycle ---------------------------------------------------------

    def _create_pools(self) -> None:
        capture, media, snapshots, probes = self._POOL_WORKERS
        self.jobs = JobRunner(workers=capture)
        self.media_jobs = JobRunner(workers=media)
        self.snapshot_jobs = JobRunner(workers=snapshots)
        self.probe_jobs = _ProbeRunner(
            workers=probes, capacity=self._PROBE_CAPACITY)

    def start(self) -> None:
        """Bring the pipeline up, or leave it exactly as it was.

        AUD2-046: `_running` used to be set before any of the work below, and was
        never cleared if that work raised. A failed startup -- an unwritable
        masters root, a recovery error -- therefore left the flag true, so every
        later `start()` returned immediately having done nothing, and the
        application ran with no recovery and no background threads.
        """
        launched: list[threading.Thread] = []
        owns_startup = False
        try:
            with self._lifecycle:
                if self._state in (STARTING, PIPELINE_RUNNING):
                    return
                if self._state == DRAINING:
                    raise RuntimeError("the pipeline is shutting down")
                if any(not pool.accepting for pool in self.pools):
                    raise RuntimeError("the pipeline's worker pools have stopped")

                self._state = STARTING
                owns_startup = True
                self._running = False
                self._lifecycle_error = ""
                self._stop.clear()
                self.config.work_root.mkdir(parents=True, exist_ok=True)
                self.config.masters_root.mkdir(parents=True, exist_ok=True)
                self.recover()
                for target, name in ((self._tick_loop, "ticker"),
                                     (self._watch_loop, "watcher")):
                    thread = threading.Thread(target=target, name=name, daemon=True)
                    thread.start()
                    launched.append(thread)
                    self._threads.append(thread)
                self._restore_requests_after_recovery_locked()
                for request in list(self._armed.values()):
                    self._submit_forced_probe_locked(request)
                self._state = PIPELINE_RUNNING
                self._running = True
        except Exception as exc:
            if not owns_startup:
                raise
            with self._lifecycle:
                self._stop.set()
                self._state = DRAINING
                self._running = False
                self._lifecycle_error = f"startup failed: {exc}"
            with self._shutdown_guard:
                rolled_back, diagnostic = self._rollback_startup(launched)
                with self._lifecycle:
                    if rolled_back:
                        self._create_pools()
                        self._stop.clear()
                        self._state = STOPPED
                    else:
                        self._state = DRAINING
                        self._lifecycle_error = (
                            f"startup failed: {exc}; rollback incomplete: "
                            f"{diagnostic}")
                        LOG.error(self._lifecycle_error)
            raise
        LOG.info("pipeline ready -- masters at %s", self.config.masters_root)

    def _rollback_startup(
            self, launched: list[threading.Thread]) -> tuple[bool, str]:
        """Quiesce every object that could have escaped failed startup."""
        thread_timeout = self._producer_shutdown_timeout()
        deadline = time.monotonic() + thread_timeout
        for thread in launched:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            thread.join(timeout=remaining)
        self._threads = [thread for thread in self._threads if thread.is_alive()]
        if self._threads:
            return False, (
                "ticker/watcher threads are still running: "
                + ", ".join(thread.name for thread in self._threads))

        timeout = self._job_shutdown_timeout()
        self.probe_jobs.stop(timeout=timeout, drain=False)

        recorders = list(self._recorders.values())
        for recorder in recorders:
            recorder.stop("stopped: startup failed")
        deadline = time.monotonic() + self._recorder_shutdown_timeout()
        for recorder in recorders:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            recorder.join(timeout=remaining)
        alive = [recorder.channel for recorder in recorders if recorder.running]
        if alive:
            return False, "recorder threads are still running: " + ", ".join(alive)

        # Recovery finalisation can produce media work, and snapshot cuts can
        # produce snapshot transcripts. Stop each producer before its consumer.
        self.jobs.stop(timeout=timeout, drain=False)
        self._release_cancelled_ownerships()
        if self.jobs.state != POOL_STOPPED:
            return False, "capture/finalisation recovery jobs are still running"

        with self._snapshot_admission:
            self.snapshot_jobs.stop(timeout=timeout, drain=False)
            self._release_cancelled_snapshot_reservations()
        self._release_cancelled_ownerships()
        if self.snapshot_jobs.state != POOL_STOPPED:
            return False, "snapshot recovery jobs are still running"

        self.media_jobs.stop(timeout=timeout, drain=False)
        self._release_cancelled_ownerships()
        if self.media_jobs.state != POOL_STOPPED:
            return False, "media recovery jobs are still running"
        if self.probe_jobs.state != POOL_STOPPED:
            return False, "live probes are still running"
        return True, ""

    @property
    def lifecycle_state(self) -> str:
        with self._lifecycle:
            return self._state

    @property
    def draining(self) -> bool:
        return self.lifecycle_state == DRAINING

    @property
    def pools(self) -> tuple[JobRunner, ...]:
        return (self.jobs, self.media_jobs, self.snapshot_jobs, self.probe_jobs)

    def active_jobs(self) -> int:
        return sum(pool.active_count() for pool in self.pools)

    def job_snapshot(self, limit: int = 40) -> list[dict[str, Any]]:
        """One merged, newest-first view of every pool, for the dashboard."""
        merged: list[dict[str, Any]] = []
        for pool in self.pools:
            merged.extend(pool.snapshot(limit))
        merged.sort(key=lambda job: job.get("created_at", 0.0), reverse=True)
        return merged[:limit]

    # -- read leases -------------------------------------------------------

    @contextmanager
    def read_lease(self, paths: Iterable[Path]) -> Iterator[None]:
        """Hold files open against reclamation for the duration of the block.

        A snapshot reads the same `.ts` the recorder is writing, and `_remux()`
        deletes that file the instant its master validates. Without a lease those
        two could overlap, and the snapshot failed part way through with an ffmpeg
        error about a file that no longer existed. Deletions raised during a lease
        are deferred, not cancelled.
        """
        keys = sorted({str(Path(path).resolve(strict=False)) for path in paths})
        acquired: list[tuple[str, ResourceLock]] = []
        incremented: list[str] = []
        with self._lease_guard:
            try:
                for key in keys:
                    if not self._leases.get(key):
                        lock = ResourceLock(
                            media_lock_path(
                                self.config.masters_root, Path(key)),
                            shared=True, timeout=60.0).acquire()
                        self._lease_kernel[key] = lock
                        acquired.append((key, lock))
                    self._leases[key] = self._leases.get(key, 0) + 1
                    incremented.append(key)
            except Exception:
                for key in incremented:
                    if self._leases.get(key):
                        self._leases[key] -= 1
                        if self._leases[key] <= 0:
                            self._leases.pop(key, None)
                for key, lock in reversed(acquired):
                    self._lease_kernel.pop(key, None)
                    lock.release()
                raise
        try:
            yield
        finally:
            release: list[ResourceLock] = []
            with self._lease_guard:
                for key in keys:
                    remaining = self._leases.get(key, 1) - 1
                    if remaining > 0:
                        self._leases[key] = remaining
                        continue
                    self._leases.pop(key, None)
                    lock = self._lease_kernel.pop(key, None)
                    if lock is not None:
                        release.append(lock)
            for lock in reversed(release):
                lock.release()
            self._drain_deferred_deletes()

    # -- recovery ----------------------------------------------------------

    def recover(self) -> list[str]:
        """Finish work a previous run left behind. Idempotent.

        A crash, a power cut, or a forced shutdown leaves recorded .ts files with
        no master, chunks stuck at `remuxing`, and half-written `.partial` output.
        Relabelling that state was not enough -- the video still had to be
        remuxed. This walks what is actually on disk and re-queues the work.

        Running it twice must do nothing the second time, which is what makes it
        safe to call on every start.
        """
        actions: list[str] = []
        actions.extend(self._adopt_quarantined_sessions())
        for session in self.store.all():
            if session.status in (STARTING, RECORDING) and not self._owns(session):
                self._externally_owned_sessions.add(session.session_id)
                actions.append(
                    f"{session.channel}/{session.session_id}: left alone, "
                    f"another process is recording it")
                continue
            self._externally_owned_sessions.discard(session.session_id)

            try:
                session_lock = ResourceLock(
                    session_lock_path(session.path)).acquire()
            except ResourceBusy:
                actions.append(
                    f"{session.session_id}: left alone, another process is "
                    "recovering it")
                continue
            session_ownership = self._ownership(session_lock)
            try:
                # Discover and measure every canonical recording before any job
                # consumes offsets or durations from the manifest.
                actions.extend(self._scan_session_media(session))

                # Loading is read-only now, so this is where a crashed session's
                # state is corrected -- and only after ownership is settled above.
                if self.store.reconcile_after_crash(session):
                    actions.append(f"{session.session_id}: reset interrupted state")

                # Both the remux staging file and the proxy's. The proxy folder
                # was missed, so killing the app mid-encode -- which is exactly
                # what someone does after a failed run -- orphaned a multi-
                # gigabyte `.partial.mp4` per chunk. `make_proxy` passes `-y` so
                # a rebuild overwrote it eventually, but nothing removed it if
                # proxies were later turned off or the chunk went away.
                folder = safe_name_component(
                    self.config.get("proxies.folder_name", "Proxies"),
                    what="proxies.folder_name")
                for pattern in ("master/*.partial.mp4",
                                f"master/{folder}/*.partial.mp4"):
                    for path in session.path.glob(pattern):
                        try:
                            path.unlink()
                            actions.append(f"removed stale {path.name}")
                        except OSError:
                            pass
                actions.extend(self._recover_snapshot_tasks(
                    session, session_ownership))

                for chunk in sorted(session.chunks, key=lambda item: item.index):
                    try:
                        lock = ResourceLock(
                            chunk_lock_path(session.path, chunk.label)).acquire()
                    except ResourceBusy:
                        actions.append(
                            f"{session.session_id}/{chunk.label}: left alone, "
                            "another process owns finalisation")
                        continue
                    ownership = self._ownership(
                        lock, parent=session_ownership)
                    try:
                        action = self._recover_chunk(session, chunk, ownership)
                        if action:
                            actions.append(
                                f"{session.session_id}/{chunk.label}: {action}")
                    finally:
                        ownership.populated()
            finally:
                session_ownership.populated()

        if actions:
            LOG.info("startup recovery: %d action(s)", len(actions))
            for line in actions:
                LOG.info("  %s", line)
        return actions

    @staticmethod
    def _canonical_media_index(channel: str, session_id: str,
                               path: Path, suffix: str) -> int | None:
        patterns = (
            re.compile(
                rf"^{re.escape(channel)}_{re.escape(session_id)}_"
                rf"c(?P<index>\d{{3,}}){re.escape(suffix)}$"),
            re.compile(
                rf"^{re.escape(channel)}_c(?P<index>\d{{3,}})"
                rf"{re.escape(suffix)}$"),
        )
        match = next((candidate.fullmatch(path.name) for candidate in patterns
                      if candidate.fullmatch(path.name)), None)
        if match is None:
            return None
        digits = match.group("index")
        index = int(digits)
        return index if digits == f"{index:03d}" else None

    def _adopt_quarantined_sessions(self) -> list[str]:
        """Recover canonical media without trusting a poisoned manifest."""
        actions: list[str] = []
        root = self.config.masters_root.resolve()
        for diagnostic in self.store.diagnostics():
            try:
                original = Path(str(diagnostic.get("original_path", ""))).resolve()
                relative = original.relative_to(root)
            except (OSError, ValueError):
                continue
            if len(relative.parts) != 3 or relative.name != "session.json":
                continue
            channel_raw, session_id, _ = relative.parts
            try:
                channel = parse_channel(channel_raw)
                if channel != channel_raw:
                    continue
                safe_name_component(session_id, what="recovered session id")
            except (ValueError, TypeError):
                continue
            if self.store.get(session_id) is not None or original.exists():
                # A copied rather than renamed invalid manifest remains evidence;
                # never replace it with inferred state.
                continue

            # AUD2-002/064: adoption reclaims and remuxes the recovered `.ts`, and
            # a live recorder in another process holds this channel's lock for its
            # whole life. Never adopt (and so never delete) media for a channel
            # something else is recording -- the quarantined manifest may belong to
            # a capture that is still running.
            if not self._channel_free(channel):
                actions.append(
                    f"{session_id}: left alone, {channel} is being recorded by "
                    "another process")
                continue

            directory = original.parent
            live = directory / "live"
            master = directory / "master"
            media: list[Path] = []
            for folder, suffix in ((live, ".ts"), (master, ".mp4")):
                if not folder.is_dir():
                    continue
                for path in folder.glob(f"*{suffix}"):
                    try:
                        if (self._canonical_media_index(
                                channel, session_id, path, suffix) is not None
                                and path.stat().st_size > 0):
                            media.append(path)
                    except OSError:
                        continue
            if not media:
                continue
            try:
                started_at = min(path.stat().st_mtime for path in media)
            except OSError:
                started_at = time.time()
            session = Session(
                session_id=session_id,
                channel=channel,
                started_at=started_at,
                directory=str(directory),
                status=INTERRUPTED,
                ended_at=time.time(),
                error=("session manifest was invalid and was quarantined; "
                       "canonical recorded media was recovered from disk"),
            )
            try:
                self.store.add(session)
            except Exception as exc:
                LOG.warning("could not create recovery manifest for %s: %s",
                            directory, exc)
                continue
            actions.append(
                f"{session_id}: adopted media after quarantining an invalid manifest")
        return actions

    def _scan_session_media(self, session: Session) -> list[str]:
        """Adopt canonical orphan media, then rebuild the session timeline."""
        actions: list[str] = []
        live_dir = session.path / "live"
        master_dir = session.path / "master"
        discovered: dict[int, dict[str, Path]] = {}
        for folder, suffix, kind in (
                (live_dir, ".ts", "ts"),
                (master_dir, ".mp4", "master")):
            if not folder.is_dir():
                continue
            for path in sorted(folder.glob(f"*{suffix}")):
                try:
                    if path.stat().st_size <= 0:
                        continue
                except OSError:
                    continue
                index = self._canonical_media_index(
                    session.channel, session.session_id, path, suffix)
                if index is not None:
                    discovered.setdefault(index, {})[kind] = path

        for index, inventory in sorted(discovered.items()):
            existing = session.chunk(index)
            changed_existing = False
            if existing is not None:
                if not existing.ts_name and inventory.get("ts") is not None:
                    existing.ts_name = inventory["ts"].name
                    changed_existing = True
                if (not existing.master_name
                        and inventory.get("master") is not None):
                    existing.master_name = inventory["master"].name
                    changed_existing = True
                if changed_existing:
                    self.store.flush(session)
                    actions.append(
                        f"{session.session_id}: adopted canonical media for "
                        f"{existing.label}")
                continue

            ts_path = inventory.get("ts")
            master_path = inventory.get("master")
            paths = [path for path in (ts_path, master_path) if path is not None]
            try:
                stats = [path.stat() for path in paths]
            except OSError:
                continue
            started_at = min(item.st_mtime for item in stats)
            chunk = Chunk(
                index=index,
                session_id=session.session_id,
                channel=session.channel,
                started_at=started_at,
                ended_at=max(item.st_mtime for item in stats),
                ts_name=ts_path.name if ts_path is not None else "",
                master_name=(master_path.name if master_path is not None else
                             f"{ts_path.stem}.mp4"),
                size_bytes=(ts_path.stat().st_size if ts_path is not None else
                            master_path.stat().st_size),
                status=REMUXING,
            )
            self.store.add_chunk(session, chunk)
            actions.append(
                f"{session.session_id}: adopted unregistered "
                + ", ".join(path.name for path in paths))

        offset = 0.0
        changed = False
        for chunk in sorted(session.chunks, key=lambda item: item.index):
            if not chunk.master_name and chunk.ts_name:
                chunk.master_name = f"{Path(chunk.ts_name).stem}.mp4"
                changed = True
            candidates: list[tuple[str, Path]] = []
            if chunk.ts_name:
                candidates.append(("ts", live_dir / chunk.ts_name))
            if chunk.master_name:
                candidates.append(("master", master_dir / chunk.master_name))
            durations: dict[str, float] = {}
            sizes: dict[str, int] = {}
            for kind, candidate in candidates:
                try:
                    if not candidate.is_file() or candidate.stat().st_size <= 0:
                        continue
                    sizes[kind] = candidate.stat().st_size
                    durations[kind] = live_duration(
                        self.tools, candidate, allow_scan=True)
                except OSError:
                    continue
            # The TS remains timeline authority while it is readable. If its
            # summary is zero or unreadable, a valid master is better evidence
            # than a stale nonzero manifest duration.
            measured = (durations.get("ts", 0.0)
                        or durations.get("master", 0.0))
            authority = ("ts" if durations.get("ts", 0.0) > 0 else
                         "master" if durations.get("master", 0.0) > 0 else "")
            measured_size = sizes.get(authority, 0) if authority else (
                sizes.get("ts", 0) or sizes.get("master", 0))
            correction_floor = (
                0.001 if authority == "ts" or chunk.duration <= 0 else
                allowed_shortfall(max(chunk.duration, measured)))
            if (measured > 0
                    and abs(chunk.duration - measured) - correction_floor > 1e-9):
                chunk.duration = round(measured, 3)
                changed = True
            if measured_size and chunk.size_bytes != measured_size:
                chunk.size_bytes = measured_size
                changed = True
            rounded = round(offset, 3)
            if chunk.session_offset != rounded:
                chunk.session_offset = rounded
                changed = True
            offset += max(0.0, chunk.duration)
        if changed:
            self.store.flush(session)
            actions.append(f"{session.session_id}: recomputed chunk timeline")
        return actions

    def _owns(self, session: Session) -> bool:
        """Is this `recording` session really ownerless, and so ours to recover?

        A session on disk marked `recording` is one of two very different things:
        a previous run that crashed, or a live capture in another process right
        now. They look identical in `session.json`, and treating the second as
        the first is how a recording gets remuxed and its `.ts` deleted out from
        under the process still writing it.

        The channel lock is the only thing that can tell them apart. Recovery
        runs before this process starts any recorder, so a lock we can take is by
        definition nobody's. The lock is released again immediately: what follows
        works on a session whose recorder is provably gone, and any new recording
        on this channel claims its own session id and its own files.
        """
        if not self._channel_free(session.channel):
            LOG.info("%s is being recorded by another process; leaving %s alone",
                     session.channel, session.session_id)
            return False
        return True

    def _channel_free(self, channel: str) -> bool:
        """True only if this channel's cross-process record lock is takeable.

        A live recorder in another process holds the channel lock for its whole
        life, so a lock we can take is by definition nobody's. Released again at
        once -- the caller only needs the yes/no. Any error is reported as "not
        free": a wrong adoption of media a live recorder is still writing is not
        recoverable, whereas retrying recovery on the next start is.
        """
        try:
            lock = ChannelLock(self.config.masters_root, channel).acquire()
        except ChannelBusy:
            return False
        except Exception as exc:
            LOG.warning("could not check the channel lock for %s (%s); "
                        "leaving it alone", channel, exc)
            return False
        lock.release()
        return True

    def _recover_chunk(self, session: Session, chunk: Chunk,
                       ownership: _OwnershipGroup) -> str:
        master = (session.path / "master" / chunk.master_name
                  if chunk.master_name else None)
        live = (session.path / "live" / chunk.ts_name
                if chunk.ts_name else None)
        changes: list[str] = []

        if master is not None and master.is_file():
            try:
                validate_master(
                    self.tools, master, chunk.duration,
                    source=(live if live is not None and live.is_file() else None))
                self._verify_before_reclaim(
                    session, chunk, master,
                    live if live is not None and live.is_file() else None)
            except Exception as exc:
                rebuildable = bool(
                    live is not None and live.is_file() and live.stat().st_size > 0)
                if not rebuildable:
                    # AUD2-003, recovery half: same trap as _remux(). Unlinking
                    # here and only then discovering there is no .ts turned a
                    # possibly-transient ffprobe failure into a certainly-lost
                    # VOD. Keep the file and say why.
                    LOG.error("%s/%s: master failed validation on recovery and "
                              "there is no .ts to rebuild it from; keeping it "
                              "(%s)", session.channel, chunk.label, exc)
                    self.store.update_chunk(
                        session, chunk, status=FAILED,
                        master_error=f"master did not validate and could not be "
                                     f"rebuilt (no .ts remains): {exc}")
                    return "kept an unvalidated master: nothing to rebuild from"
                LOG.warning("%s/%s: master failed validation on recovery (%s); "
                            "rebuilding from %s",
                            session.channel, chunk.label, exc, live.name)
                # Left in place: remux_to_mp4() stages a .partial and replaces
                # this only once the replacement validates.
                # It is no longer complete, whatever the state file said. Clear
                # that first or the branch below sees `complete` and skips the
                # rebuild the discarded master just made necessary.
                self.store.update_chunk(
                    session, chunk, status=REMUXING,
                    master_error=f"previous master was unusable: {exc}")
                changes.append("will rebuild an invalid master")
            else:
                width, height = video_dimensions(self.tools, master)
                was_complete = chunk.status == COMPLETE and not chunk.master_error
                self.store.update_chunk(
                    session, chunk, status=COMPLETE, master_error="",
                    size_bytes=master.stat().st_size, width=width, height=height)
                if not was_complete:
                    changes.append("adopted an existing valid master")
                if live is not None and live.is_file():
                    self._reclaim_ts(session, chunk, live)
                    if not live.exists():
                        changes.append("reclaimed duplicate TS")
                changes.extend(self._recover_artifacts(
                    session, chunk, ownership))
                return "; ".join(changes)

        if live is not None and live.is_file() and live.stat().st_size > 0:
            # Real recorded video with no master. This is the case that used to
            # sit on disk forever.
            self.store.update_chunk(session, chunk, status=REMUXING)
            job = ownership.submit(
                self.jobs,
                f"finalize:{session.session_id}:{chunk.label}",
                f"{session.channel} {chunk.label}: finish",
                "finalize",
                lambda item: self._finalize_chunk(
                    item, session, chunk, ownership=ownership),
            )
            if job is None and not self._job_active(
                    self.jobs, f"finalize:{session.session_id}:{chunk.label}"):
                self.store.update_chunk(
                    session, chunk, status=FAILED,
                    master_error="recovery finalisation could not be queued")
                return "finalisation queue refused recovered media"
            return "; ".join(changes + [
                "re-queued finalisation for recovered media"])

        if chunk.status in (STARTING, RECORDING, REMUXING, INTERRUPTED):
            self.store.update_chunk(session, chunk, status=FAILED,
                                    master_error="no media survived the interruption")
            return "marked failed: no media on disk"
        return ""

    def _recover_artifacts(self, session: Session, chunk: Chunk,
                           ownership: _OwnershipGroup) -> list[str]:
        """Reconcile each derivative independently from files on disk."""
        actions: list[str] = []
        actions.extend(self._recover_words_stash(session, chunk))

        master = session.path / "master" / chunk.master_name
        folder = self.config.get("proxies.folder_name", "Proxies")
        suffix = self.config.get("proxies.suffix", "_Proxy")
        proxy = master.parent / folder / f"{master.stem}{suffix}.mp4"
        if not self.config.get("proxies.enabled", True):
            if chunk.proxy_status != SKIPPED:
                self.store.update_chunk(session, chunk, proxy_status=SKIPPED,
                                        proxy_error="")
        else:
            valid_proxy = False
            if proxy.is_file():
                try:
                    validate_proxy(
                        self.tools, master, proxy,
                        height=int(self.config.get("proxies.height", 540)))
                except Exception as exc:
                    LOG.warning("%s/%s: existing proxy is unusable (%s); "
                                "rebuilding it", session.channel, chunk.label, exc)
                    actions.append("will rebuild an invalid proxy")
                else:
                    valid_proxy = True
            if valid_proxy:
                if chunk.proxy_status != DONE or chunk.proxy_name != proxy.name:
                    self.store.update_chunk(
                        session, chunk, proxy_status=DONE,
                        proxy_name=proxy.name, proxy_error="")
                    actions.append("adopted existing proxy")
            else:
                self.store.update_chunk(session, chunk, proxy_status=PENDING,
                                        proxy_error="", proxy_name="")
                if self._queue_proxy(
                        session, chunk, ownership=ownership) is not None:
                    actions.append("re-queued proxy")

        words_path = self.transcriber.words_path(session, chunk)
        complete = False
        words: list[Any] = []
        meta: dict[str, Any] = {}
        publication_error = ""
        try:
            words, meta = load_words(words_path)
        except Exception as exc:
            if words_path.exists():
                quarantine = words_path.with_name(
                    f"words.json.corrupt-{int(time.time())}")
                try:
                    words_path.replace(quarantine)
                    actions.append(f"quarantined corrupt {words_path.name}")
                except OSError:
                    pass
            self.store.update_chunk(
                session, chunk, transcribed_through=0.0, word_count=0,
                transcript_status=ERROR, transcript_error=str(exc))
        else:
            if meta:
                try:
                    if not publication_is_consistent(
                            self.transcriber.output_dir(session, chunk),
                            words, meta):
                        self.transcriber.republish(session, chunk)
                        actions.append("repaired transcript export generation")
                except Exception as exc:
                    publication_error = (
                        f"could not repair transcript exports: {exc}")
                    self.store.update_chunk(
                        session, chunk, transcript_status=ERROR,
                        transcript_error=publication_error)
            covered = float(meta.get("covered_seconds") or 0.0)
            # SHORT_READ_TOLERANCE, not COVERAGE_EPSILON: this compares the
            # recorder's manifest figure against audio ffprobe measured, which
            # are two different measurements and never agree to the millisecond.
            # At 1 ms a chunk whose audio track ends 34 ms before its video --
            # ordinary, and what the 2026-08-16 recording produced -- was judged
            # incomplete on every startup, so recovery re-ran finalisation and
            # republished its exports, seam and rundown at every boot forever.
            complete = not publication_error and bool(meta.get("complete")) and (
                chunk.duration <= 0
                or chunk.duration - covered <= SHORT_READ_TOLERANCE)
            if complete:
                if (chunk.transcript_status != DONE
                        or chunk.transcribed_through != covered
                        or chunk.word_count != len(words)
                        or chunk.transcript_error):
                    self.store.update_chunk(
                        session, chunk, transcript_status=DONE,
                        transcript_error="", transcribed_through=covered,
                        word_count=len(words))
                    actions.append("adopted complete transcript")
            elif meta and not publication_error:
                self.store.update_chunk(
                    session, chunk, transcript_status=PENDING,
                    transcript_error="",
                    transcribed_through=covered, word_count=len(words))
            elif not meta and (chunk.transcribed_through or chunk.word_count):
                # P2: the words file is gone but the manifest still carries a
                # nonzero cursor. Resuming would start transcription past the
                # beginning and publish a transcript missing its opening, so reset
                # to zero and re-transcribe from the start. A legitimate fresh
                # chunk (cursor already 0, no file) is left untouched.
                self.store.update_chunk(
                    session, chunk, transcript_status=PENDING,
                    transcript_error="", transcribed_through=0.0, word_count=0)
                actions.append(
                    "reset transcript cursor to 0: words file was missing")

        if publication_error:
            # A rundown beside an unreadable export set is not evidence that the
            # transcript generation is healthy. Keep the strict words for the
            # next retry, but do not advertise or summarise this generation.
            self._recover_summary_state(session, chunk, complete=False)
            return actions

        if not self.config.get("transcription.enabled", True):
            if not complete:
                self.store.update_chunk(session, chunk, transcript_status=SKIPPED,
                                        transcript_error="")
            needs_summary = self._recover_summary_state(
                session, chunk, complete=complete)
            if complete and needs_summary:
                job = ownership.submit(
                    self.jobs,
                    f"recover-post:{session.session_id}:{chunk.label}",
                    f"{session.channel} {chunk.label}: recover transcript outputs",
                    "recover",
                    lambda item: self._recover_post_transcript(
                        session, chunk, ownership, needs_summary),
                )
                if job is not None:
                    actions.append("re-queued transcript outputs")
            return actions
        if not self.config.secret("deepgram_api_key"):
            if not complete:
                self.store.update_chunk(
                    session, chunk, transcript_status=SKIPPED,
                    transcript_error="no Deepgram API key configured")
            needs_summary = self._recover_summary_state(
                session, chunk, complete=complete)
            if complete and needs_summary:
                job = ownership.submit(
                    self.jobs,
                    f"recover-post:{session.session_id}:{chunk.label}",
                    f"{session.channel} {chunk.label}: recover transcript outputs",
                    "recover",
                    lambda item: self._recover_post_transcript(
                        session, chunk, ownership, needs_summary),
                )
                if job is not None:
                    actions.append("re-queued transcript outputs")
            return actions

        if not complete:
            self.store.update_chunk(session, chunk, transcript_status=PENDING,
                                    transcript_error="")
            self._recover_summary_state(session, chunk, complete=False)
            key = f"recover-transcript:{session.session_id}:{chunk.label}"
            job = ownership.submit(
                self.jobs, key,
                f"{session.channel} {chunk.label}: recover transcript",
                "transcribe",
                lambda item: self._recover_transcript(
                    item, session, chunk, ownership),
            )
            if job is None and not self._job_active(self.jobs, key):
                self.store.update_chunk(
                    session, chunk, transcript_status=ERROR,
                    transcript_error="transcript recovery could not be queued")
            elif job is not None:
                actions.append("re-queued transcript")
            return actions

        needs_summary = self._recover_summary_state(
            session, chunk, complete=True)
        seam = self._seam_recovery_needed(session, chunk)
        if seam or needs_summary:
            key = f"recover-post:{session.session_id}:{chunk.label}"
            job = ownership.submit(
                self.jobs, key,
                f"{session.channel} {chunk.label}: recover transcript outputs",
                "recover",
                lambda item: self._recover_post_transcript(
                    session, chunk, ownership, needs_summary),
            )
            if job is not None:
                names = []
                if seam:
                    names.append("seam")
                if needs_summary:
                    names.append("rundown")
                actions.append("re-queued " + ", ".join(names))
        return actions

    def _recover_words_stash(self, session: Session,
                             chunk: Chunk) -> list[str]:
        path = self.transcriber.words_path(session, chunk)
        stash = path.with_name("words.json.previous")
        if not stash.exists():
            return []
        backup = stash.with_name("generation.previous")
        try:
            _, current_meta = load_words(path)
        except Exception:
            current_meta = {}
        try:
            _, previous_meta = load_words(stash)
        except Exception as exc:
            self._quarantine_stash(stash, backup)
            self.store.update_chunk(
                session, chunk, transcript_status=ERROR,
                transcript_error=f"previous transcript stash is corrupt: {exc}")
            return ["quarantined an unreadable previous transcript"]

        if current_meta.get("complete"):
            self.transcriber.discard_stash(stash)
            return ["discarded obsolete previous transcript stash"]
        if previous_meta.get("complete") and backup.is_dir():
            self.transcriber.restore_words(session, chunk, stash)
            self.store.update_chunk(session, chunk, transcript_error="")
            return ["restored previous transcript generation"]

        self._quarantine_stash(stash, backup)
        self.store.update_chunk(
            session, chunk, transcript_status=ERROR,
            transcript_error="previous transcript stash was incomplete")
        return ["quarantined an incomplete previous transcript"]

    @staticmethod
    def _quarantine_stash(stash: Path, backup: Path) -> None:
        stamp = f"unrecoverable-{int(time.time())}"
        try:
            stash.replace(stash.with_name(f"{stash.name}.{stamp}"))
        except OSError:
            pass
        if backup.exists():
            try:
                backup.replace(backup.with_name(f"{backup.name}.{stamp}"))
            except OSError:
                pass

    def _recover_transcript(self, job: Job, session: Session, chunk: Chunk,
                            ownership: _OwnershipGroup) -> None:
        job.progress = "transcribing"
        result = self._finalize_transcript(session, chunk)
        if not result.complete:
            self._recover_summary_state(session, chunk, complete=False)
            raise RuntimeError(
                result.detail or "transcript recovery did not complete")
        self._stitch_boundary(session, chunk, already_owned=(chunk,))
        if self._recover_summary_state(session, chunk, complete=True):
            self._queue_summary(session, chunk, ownership=ownership)
        job.progress = ""

    def _recover_post_transcript(self, session: Session, chunk: Chunk,
                                 ownership: _OwnershipGroup,
                                 needs_summary: bool) -> None:
        self._stitch_boundary(session, chunk, already_owned=(chunk,))
        if needs_summary:
            self._queue_summary(session, chunk, ownership=ownership)

    def _summary_enabled(self) -> bool:
        return bool(self.config.get("summary.enabled", True)) and str(
            self.config.get("summary.provider") or "").lower() != "none"

    def _summary_capability(self) -> tuple[bool, str]:
        """Can the configured engine be asked at all? One answer, five callers.

        Driven by `models.PROVIDER_SECRETS` rather than a chain of provider
        names, so adding an engine does not mean remembering to teach this
        function about it -- the dashboard, recovery, the API and the job all
        read their verdict from here.
        """
        if not self._summary_enabled():
            return False, "rundowns are disabled"
        provider = str(self.config.get("summary.provider") or "claude-cli").lower()
        secret = PROVIDER_SECRETS.get(provider)
        if secret:
            if self.config.secret(secret):
                return True, ""
            return False, (f"no API key is configured for {provider} "
                           f"(secrets.{secret})")
        if provider == "cli":
            if self._summary_cli_command():
                return True, ""
            return False, ("summary.cli_command is not set, so there is no "
                           "command to run")
        if self.tools.claude:
            return True, ""
        return False, "the claude executable is unavailable"

    def _summary_cli_command(self) -> list[str]:
        return [str(part) for part in (self.config.get("summary.cli_command") or [])
                if str(part).strip()]

    def _summary_source(self, session: Session,
                        chunk: Chunk) -> tuple[_SummarySource | None, str]:
        """The one eligibility check used by jobs, recovery, API, and UI."""
        if chunk.transcript_status != DONE:
            return None, "the transcript is not complete"

        path = self.transcriber.words_path(session, chunk)
        try:
            words, meta = load_words(path)
        except Exception as exc:
            return None, f"words.json is unavailable or invalid: {exc}"
        if meta.get("complete") is not True:
            return None, "words.json does not mark this transcript complete"
        if not words:
            return None, "no speech was transcribed"

        minimum = int(self.config.get("summary.min_words", 25))
        if len(words) < minimum:
            return None, f"the transcript has {len(words)} words; {minimum} are required"
        try:
            consistent = publication_is_consistent(
                self.transcriber.output_dir(session, chunk), words, meta)
        except Exception as exc:
            return None, f"the transcript export generation is unreadable: {exc}"
        if not consistent:
            return None, "the transcript export generation is incomplete or inconsistent"

        generation = meta.get("generation")
        if not isinstance(generation, str):
            return None, "words.json has no transcript generation"
        return _SummarySource(
            generation=generation,
            body=build_model_input(words, chunk.session_offset),
        ), ""

    def _current_export_generation(self, session: Session, chunk: Chunk) -> str:
        """Current durable export identity, independent of artifact state."""
        path = self.transcriber.words_path(session, chunk)
        try:
            words, meta = load_words(path)
            if not publication_is_consistent(
                    self.transcriber.output_dir(session, chunk), words, meta):
                return ""
        except Exception:
            return ""
        generation = meta.get("generation")
        return generation if isinstance(generation, str) else ""

    def _reconcile_generation_changes(
        self,
        session: Session,
        chunks: Iterable[Chunk],
        before: dict[int, str],
        *,
        queue: bool,
    ) -> list[Chunk]:
        """Retire derived files for every transcript generation that changed."""
        changed: list[Chunk] = []
        for chunk in sorted(chunks, key=lambda item: item.index):
            generation = self._current_export_generation(session, chunk)
            if generation == before.get(chunk.index, ""):
                continue
            changed.append(chunk)
            try:
                self._retire_obsolete_rundown(
                    session, chunk, generation,
                    "its transcript generation changed")
            except Exception as exc:
                self._summary_retirement_error(session, chunk, exc)
                continue

            source, reason = self._summary_source(session, chunk)
            if not self._summary_enabled():
                self.store.update_chunk(
                    session, chunk, summary_status=SKIPPED,
                    summary_error="")
            elif source is None:
                self.store.update_chunk(
                    session, chunk, summary_status=SKIPPED,
                    summary_error=f"no rundown: {reason}")
            elif queue:
                self._queue_summary(session, chunk)
            else:
                self.store.update_chunk(
                    session, chunk, summary_status=PENDING,
                    summary_error="")
            self._refresh_session_index(session)
        return changed

    def _retire_obsolete_rundown(self, session: Session, chunk: Chunk,
                                 generation: str, why: str) -> None:
        rundown = self.transcriber.output_dir(session, chunk) / "rundown.md"
        if rundown.is_file() and rundown_generation(rundown) != generation:
            try:
                self._retire_rundown(rundown, why)
            finally:
                self._refresh_session_index(session)

    def _summary_retirement_error(self, session: Session, chunk: Chunk,
                                  exc: Exception) -> None:
        self.store.update_chunk(
            session, chunk, summary_status=ERROR,
            summary_error=f"could not remove obsolete rundown: {exc}")
        self._refresh_session_index(session)
        LOG.error("%s/%s: could not retire obsolete rundown: %s",
                  session.channel, chunk.label, exc)

    def _recover_summary_state(self, session: Session, chunk: Chunk, *,
                               complete: bool) -> bool:
        rundown = self.transcriber.output_dir(session, chunk) / "rundown.md"
        if not self._summary_enabled():
            # A configuration switch is not a deletion request. Generation
            # changes retire stale files at the transcript publication boundary.
            self.store.update_chunk(session, chunk, summary_status=SKIPPED,
                                    summary_error="")
            self._refresh_session_index(session)
            return False

        source, reason = self._summary_source(session, chunk)
        if not complete:
            source, reason = None, "the transcript is incomplete or unavailable"
        if source is None:
            try:
                self._retire_rundown(rundown, reason)
            except Exception as exc:
                self._summary_retirement_error(session, chunk, exc)
                return False
            self.store.update_chunk(
                session, chunk, summary_status=SKIPPED,
                summary_error=f"no rundown: {reason}")
            self._refresh_session_index(session)
            return False

        if rundown.is_file():
            try:
                generation = rundown_generation(rundown)
            except Exception as exc:
                self._summary_retirement_error(session, chunk, exc)
                return False
            if generation == source.generation:
                self.store.update_chunk(session, chunk, summary_status=DONE,
                                        summary_error="")
                self._refresh_session_index(session)
                return False
            try:
                self._retire_rundown(
                    rundown, "it belongs to an older transcript generation")
            except Exception as exc:
                self._summary_retirement_error(session, chunk, exc)
                return False
        self.store.update_chunk(session, chunk, summary_status=PENDING,
                                summary_error="")
        self._refresh_session_index(session)
        return True

    def _seam_recovery_needed(self, session: Session, chunk: Chunk) -> bool:
        if (chunk.index <= 0
                or not self.config.get(
                    "transcription.stitch_chunk_boundaries", True)):
            return False
        previous = session.chunk(chunk.index - 1)
        if previous is None:
            return False
        try:
            _, meta = load_words(self.transcriber.words_path(session, previous))
        except Exception:
            return False
        return bool(meta.get("complete"))

    def _ownership(self, lock: ResourceLock, *,
                   parent: _OwnershipGroup | None = None) -> _OwnershipGroup:
        parent_token = parent.retain() if parent is not None else None

        def released(group: _OwnershipGroup) -> None:
            self._ownership_released(group)
            if parent is not None and parent_token is not None:
                parent.finish(parent_token)

        group = _OwnershipGroup(lock, released)
        with self._ownership_guard:
            self._ownership_groups.add(group)
        return group

    def _ownership_released(self, group: _OwnershipGroup) -> None:
        with self._ownership_guard:
            self._ownership_groups.discard(group)

    def _release_cancelled_ownerships(self) -> None:
        with self._ownership_guard:
            groups = list(self._ownership_groups)
        for group in groups:
            group.release_cancelled()

    def _release_cancelled_snapshot_reservations(self) -> None:
        with self._snapshot_reservation_guard:
            keys = list(self._snapshot_reservations)
        for key in keys:
            job = self.snapshot_jobs.get(key)
            if job is not None and job.status != JOB_CANCELLED:
                continue
            self._release_snapshot_reservation(key)

    def _release_snapshot_reservation(self, key: str) -> None:
        """Transfer and release one reservation exactly once."""
        with self._snapshot_reservation_guard:
            reservation = self._snapshot_reservations.pop(key, None)
        if reservation is not None:
            reservation.release()

    @staticmethod
    def _job_active(pool: JobRunner, key: str) -> bool:
        job = pool.get(key)
        return bool(job and job.status in (JOB_QUEUED, JOB_RUNNING))

    def _job_shutdown_timeout(self) -> float:
        """Budget one longest legal worker operation, including fallback."""
        deepgram = float(self.config.get(
            "transcription.request_timeout_seconds", 600))
        rundown = float(self.config.get("summary.timeout_seconds", 900))
        probe = float(self.config.get("watcher.probe_timeout_seconds", 25))
        return max(deepgram, rundown, probe,
                   MEDIA_OPERATION_TIMEOUT) + 60.0

    def _producer_shutdown_timeout(self) -> float:
        """Join budget for producer threads and their dedicated probe pool."""
        probe = float(self.config.get("watcher.probe_timeout_seconds", 25))
        return max(90.0, probe + 30.0)

    def _recorder_shutdown_timeout(self) -> float:
        # ffmpeg grace, streamlink reap, four recorder watcher joins, and margin
        # for final state persistence/callbacks that enqueue the last chunk.
        grace = float(self.config.get("recording.ffmpeg_grace_seconds", 120))
        return grace + 20.0 + 15.0 + 4 * 15.0 + 30.0

    def _shutdown_ultimate_timeout(self) -> float:
        # Pools are drained in dependency order. In the worst case each phase
        # begins its longest operation only when the preceding producer closes.
        return (self._producer_shutdown_timeout()
                + self._recorder_shutdown_timeout()
                + 4 * self._job_shutdown_timeout() + 60.0)

    def shutdown(self, *, job_timeout: float | None = None,
                 thread_timeout: float | None = None,
                 recorder_timeout: float | None = None) -> None:
        job_budget = (self._job_shutdown_timeout()
                      if job_timeout is None else max(0.0, job_timeout))
        thread_budget = (self._producer_shutdown_timeout()
                         if thread_timeout is None else max(0.0, thread_timeout))
        recorder_budget = (self._recorder_shutdown_timeout()
                           if recorder_timeout is None
                           else max(0.0, recorder_timeout))
        with self._shutdown_guard:
            self._shutdown_once(
                job_timeout=job_budget,
                thread_timeout=thread_budget,
                recorder_timeout=recorder_budget,
            )

    def _shutdown_once(self, *, job_timeout: float,
                       thread_timeout: float,
                       recorder_timeout: float) -> None:
        """Stop everything in dependency order. Idempotent.

        Order matters. Background threads are stopped and joined first so a live
        probe that is mid-flight cannot start a new recorder behind our back.
        Recorders then finish, which queues their final chunk work. Only then do
        the job workers drain, so that final work actually runs instead of being
        abandoned in the queue.
        """
        with self._lifecycle:
            if (self._state == STOPPED
                    and all(pool.state == POOL_STOPPED for pool in self.pools)):
                return
            self._state = DRAINING
            self._running = False
            self._stop.set()

        deadline = time.monotonic() + max(0.0, thread_timeout)
        for thread in list(self._threads):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            thread.join(timeout=remaining)
        self._threads = [thread for thread in self._threads if thread.is_alive()]
        if self._threads:
            self._lifecycle_error = (
                "shutdown timed out waiting for ticker/watcher threads: "
                + ", ".join(thread.name for thread in self._threads))
            LOG.error(self._lifecycle_error)
            return

        # Probes can create recorders, so their runner stops accepting before any
        # recorder is snapshotted or stopped. A timed-out running probe remains
        # visible as draining, but its post-probe lifecycle check sees `_stop` and
        # cannot launch capture behind shutdown.
        self.probe_jobs.stop(timeout=job_timeout)

        recorders = list(self._recorders.values()) + list(
            self._vod_recorders.values())
        for recorder in recorders:
            recorder.stop("stopped: shutting down")
        deadline = time.monotonic() + max(0.0, recorder_timeout)
        for recorder in recorders:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            recorder.join(timeout=remaining)
        alive = [recorder for recorder in recorders if recorder.running]
        if alive:
            names = [recorder.channel for recorder in alive]
            self._lifecycle_error = (
                "shutdown timed out waiting for recorder threads: "
                + ", ".join(names))
            for recorder in alive:
                if recorder.session is not None:
                    self.store.update(
                        recorder.session,
                        error=self._lifecycle_error,
                    )
            LOG.error(self._lifecycle_error)
            # Recorder callbacks are still producers. Stopping any pool here
            # would guarantee their eventual final chunk submission is refused.
            return

        # Capture-critical first: finalisation is what queues proxy and rundown
        # work. Snapshot cuts are producers too: each can queue a transcript onto
        # the media pool, so they drain before that final consumer.
        self.jobs.stop(timeout=job_timeout)
        self._release_cancelled_ownerships()
        if self.jobs.state != POOL_STOPPED:
            self._lifecycle_error = "shutdown is still draining capture/finalisation"
            return
        # Admission owns the reservation registration and JobRunner submission as
        # one transaction. Waiting for it closes the gap where shutdown could
        # cancel/clean a job while its caller was still publishing ownership.
        with self._snapshot_admission:
            self.snapshot_jobs.stop(timeout=job_timeout)
            self._release_cancelled_snapshot_reservations()
        self._release_cancelled_ownerships()
        if self.snapshot_jobs.state != POOL_STOPPED:
            self._lifecycle_error = "shutdown is still draining snapshot producers"
            return
        self.media_jobs.stop(timeout=job_timeout)
        self._release_cancelled_ownerships()
        if self.media_jobs.state != POOL_STOPPED:
            self._lifecycle_error = "shutdown is still draining media consumers"
            return
        if self.probe_jobs.state != POOL_STOPPED:
            self._lifecycle_error = "shutdown is still draining live probes"
            return

        with self._lifecycle:
            self._state = STOPPED
            self._running = False
            self._lifecycle_error = ""

    def shutdown_until_stopped(
            self, *, progress_interval: float = 30.0,
            ultimate_timeout: float | None = None) -> None:
        """Production shutdown: report progress and never return `DRAINING`.

        All operations underneath have hard deadlines. The ultimate deadline is
        their dependency-ordered sum; crossing it is an error worth reporting,
        not permission to abandon a mutating worker. A second Ctrl+C is the one
        explicit escape hatch and logs the recoverable work before propagating.
        """
        interval = max(0.1, float(progress_interval))
        ultimate = (self._shutdown_ultimate_timeout()
                    if ultimate_timeout is None
                    else max(0.0, float(ultimate_timeout)))
        deadline = time.monotonic() + ultimate
        finished = threading.Event()

        def report_progress() -> None:
            while not finished.wait(interval):
                self._log_shutdown_progress(deadline)

        reporter = threading.Thread(
            target=report_progress, name="shutdown-progress", daemon=True)
        reporter.start()
        try:
            self.shutdown()
            while self.lifecycle_state == DRAINING:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    LOG.error(
                        "shutdown exceeded its %.0fs operation deadline; "
                        "continuing to wait because workers still own state",
                        ultimate)
                    retry = interval
                else:
                    retry = min(interval, remaining)
                # The first graceful pass has already closed admission and sent
                # any necessary sentinels. Repeated short joins now only observe
                # actual worker/producer completion; they cannot accept new work.
                self.shutdown(job_timeout=retry, thread_timeout=retry,
                              recorder_timeout=retry)
        except KeyboardInterrupt:
            self._log_remaining_recoverable_work()
            raise
        finally:
            finished.set()
            reporter.join(timeout=max(1.0, interval + 1.0))

    def _log_shutdown_progress(self, deadline: float) -> None:
        active = self.active_jobs()
        remaining = max(0.0, deadline - time.monotonic())
        detail = self._lifecycle_error or "waiting for lifecycle owners"
        LOG.info("shutdown still draining: %d active job(s), %.0fs budget "
                 "remaining (%s)", active, remaining, detail)

    def _log_remaining_recoverable_work(self) -> None:
        active = [job for job in self.job_snapshot(limit=100)
                  if job.get("status") in (JOB_QUEUED, JOB_RUNNING)]
        producers = [thread.name for thread in self._threads if thread.is_alive()]
        recorders = [recorder.channel for recorder in self._recorders.values()
                     if recorder.running]
        labels = [str(job.get("label") or job.get("key")) for job in active]
        LOG.warning(
            "shutdown interrupted again; exiting with recoverable work still "
            "owned: %d job(s)%s%s%s",
            len(active),
            f" [{', '.join(labels[:10])}]" if labels else "",
            f"; producer threads: {', '.join(producers)}" if producers else "",
            f"; recorders: {', '.join(recorders)}" if recorders else "",
        )

    # -- channels ----------------------------------------------------------

    def channels(self) -> list[str]:
        return [str(item) for item in (self.config.get("channels") or [])]

    def _control_payload_locked(self) -> dict[str, Any]:
        return {
            "version": 1,
            "requests": [request.to_dict() for request in sorted(
                self._armed.values(), key=lambda item: item.requested_at)],
            "results": [result.to_dict() for result in sorted(
                self._request_results.values(),
                key=lambda item: item.completed_at)],
            "auto_suppressed": sorted(self._auto_suppressed),
        }

    def _persist_control_locked(self) -> None:
        merged = self._control_store.save(self._control_payload_locked())
        self._armed = {
            request.channel: request
            for request in (_RecordingRequest.from_dict(item)
                            for item in merged["requests"])
        }
        self._request_results = {
            result.request_id: result
            for result in (_RecordingResult.from_dict(item)
                           for item in merged["results"])
        }
        self._auto_suppressed = set(merged["auto_suppressed"])

    @staticmethod
    def _session_has_media(session: Session) -> bool:
        for chunk in session.chunks:
            if chunk.size_bytes > 0:
                return True
            for folder, name in (("live", chunk.ts_name),
                                 ("master", chunk.master_name)):
                if not name:
                    continue
                try:
                    if (session.path / folder / name).stat().st_size > 0:
                        return True
                except OSError:
                    continue
        return False

    def _restore_requests_after_recovery_locked(self) -> None:
        """Resolve or re-arm attempts that were live when the process died."""
        changed = False
        for request in list(self._armed.values()):
            if not request.attempt_session_id:
                continue
            session = self.store.get(request.attempt_session_id)
            if session is not None and self._session_has_media(session):
                self._finish_request_locked(
                    request, "complete", session_id=session.session_id,
                    persist=False)
            else:
                request.attempt_session_id = ""
                request.last_error = (
                    "the previous recording attempt ended before media arrived")
            changed = True
        if changed:
            self._persist_control_locked()
            self._request_changed.notify_all()

    def _finish_request_locked(
            self, request: _RecordingRequest, status: str, *,
            session_id: str = "", error: str = "", persist: bool = True) -> None:
        current = self._armed.get(request.channel)
        if current is None or current.request_id != request.request_id:
            return
        self._armed.pop(request.channel, None)
        self._request_results[request.request_id] = _RecordingResult(
            request_id=request.request_id,
            channel=request.channel,
            status=status,
            session_id=session_id,
            error=redact(error),
            completed_at=time.time(),
        )
        if persist:
            self._persist_control_locked()
        self._request_changed.notify_all()

    def request_result(self, request_id: str) -> dict[str, Any] | None:
        with self._lifecycle:
            result = self._request_results.get(str(request_id))
            if result is not None:
                return result.to_dict()
            for request in self._armed.values():
                if request.request_id == request_id:
                    return {
                        **request.to_dict(),
                        "status": (STARTING if request.attempt_session_id
                                   else "pending"),
                        "session_id": request.attempt_session_id,
                        "error": request.last_error,
                        "completed_at": 0.0,
                    }
            return None

    def wait_for_request(self, request_id: str,
                         timeout: float | None = None) -> dict[str, Any] | None:
        """Wait on the request's durable terminal record, not session polling."""
        deadline = (None if timeout is None
                    else time.monotonic() + max(0.0, timeout))
        with self._request_changed:
            while True:
                result = self._request_results.get(request_id)
                if result is not None:
                    return result.to_dict()
                pending = any(request.request_id == request_id
                              for request in self._armed.values())
                if not pending:
                    return None
                if deadline is None:
                    self._request_changed.wait(0.5)
                    continue
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._request_changed.wait(min(0.5, remaining))

    def _submit_forced_probe_locked(
            self, request: _RecordingRequest) -> Job | None:
        key = f"request-probe:{request.request_id}"
        active = self.probe_jobs.get(key)
        if active is not None and active.status in (JOB_QUEUED, JOB_RUNNING):
            return active
        return self.probe_jobs.submit(
            key,
            f"{request.channel}: checking if live",
            "probe",
            lambda job, channel=request.channel, token=request.request_id:
                self._check_channel(channel, force=True, request_id=token),
        )

    def add_channel(self, name: str) -> str:
        # One parser for every entry point, so a name that reaches the filesystem
        # has always been through the same validation.
        channel = parse_channel(name)
        def add(data: dict[str, Any]) -> None:
            channels = data.setdefault("channels", [])
            if channel not in channels:
                channels.append(channel)
                channels.sort()

        self.config.mutate_and_save(add)
        return channel

    def remove_channel(self, name: str) -> None:
        channel = parse_channel(name)
        # Keep removal and request invalidation under the same barrier used by a
        # post-probe auto-start. The probe must see either the old membership or
        # the completed removal, never the gap between two unlocked operations.
        with self._lifecycle:
            def remove(data: dict[str, Any]) -> None:
                data["channels"] = [item for item in data.get("channels", [])
                                    if item != channel]

            # Persist membership first. If disk rejects the update, neither the
            # live watch list nor a pending recording request is changed.
            self.config.mutate_and_save(remove)
            request = self._armed.get(channel)
            if request is not None:
                self._finish_request_locked(
                    request, "cancelled", error="channel was removed",
                    persist=False)
            self._persist_control_locked()

    def start_recording(self, channel: str, *, request_id: str = "") -> Session:
        channel = parse_channel(channel)
        # The whole check-then-launch sequence is inside the lock, so a second
        # caller sees the new recorder rather than the gap before it was stored.
        with self._lifecycle:
            if self._stop.is_set():
                raise RuntimeError("the pipeline is shutting down")
            existing = self._recorders.get(channel)
            if existing and existing.running:
                raise RuntimeError(f"{channel} is already recording")

            recorder = Recorder(
                self.config, self.tools, self.store, channel,
                on_chunk_finalized=self._on_chunk_finalized,
                on_session_ended=(
                    lambda session, token=request_id:
                        self._on_session_ended(session, token)),
                on_first_media=self._on_first_media,
                request_token=request_id,
            )
            # Publish the recorder object before its thread can report media. The
            # request callback then has an identity to compare even if bytes land
            # before start() returns to this thread.
            self._recorders[channel] = recorder
            try:
                session = recorder.start()
            except Exception:
                if self._recorders.get(channel) is recorder:
                    self._recorders.pop(channel, None)
                raise

        LOG.info("recording %s -> %s", channel, session.directory)
        return session

    def request_recording(self, channel: str) -> dict[str, Any]:
        """Record this channel: now if it is live, otherwise the moment it is.

        Pressing Record used to launch streamlink regardless, and streamlink is
        configured to retry forever -- so an offline channel produced a session
        that sat at `recording` with an empty chunk, indefinitely, having captured
        nothing. The dashboard said it was recording because, as far as the
        pipeline was concerned, it was.

        So a channel that is not known to be live is *armed* instead. Nothing is
        spawned, no session directory is claimed, and the watcher starts the real
        recording on the pass that first sees it live.
        """
        channel = parse_channel(channel)
        request = _RecordingRequest(
            request_id=secrets.token_hex(16),
            channel=channel,
            requested_at=time.time(),
        )
        with self._lifecycle:
            if self._stop.is_set() or self._state == DRAINING:
                raise RuntimeError("the pipeline is shutting down")
            if not self.probe_jobs.accepting:
                raise RuntimeError("live probes are not accepting recording requests")
            existing = self._recorders.get(channel)
            if existing and existing.running:
                raise RuntimeError(f"{channel} is already recording")

            previous = self._armed.get(channel)
            previous_suppressed = channel in self._auto_suppressed
            previous_result = (self._request_results.get(previous.request_id)
                               if previous is not None else None)
            if previous is not None:
                self._finish_request_locked(
                    previous, "cancelled",
                    error="superseded by a newer Record request", persist=False)
            self._auto_suppressed.discard(channel)
            self._armed[channel] = request
            try:
                self._persist_control_locked()
                job = self._submit_forced_probe_locked(request)
                if job is None:
                    raise RuntimeError("the live-probe runner refused the request")
            except Exception as exc:
                self._armed.pop(channel, None)
                self._request_results.pop(request.request_id, None)
                if previous is not None:
                    self._armed[channel] = previous
                    if previous_result is None:
                        self._request_results.pop(previous.request_id, None)
                    else:
                        self._request_results[previous.request_id] = previous_result
                if previous_suppressed:
                    self._auto_suppressed.add(channel)
                else:
                    self._auto_suppressed.discard(channel)
                failure = _RecordingResult(
                    request_id=request.request_id,
                    channel=channel,
                    status="error",
                    error=redact(str(exc)),
                    completed_at=time.time(),
                )
                self._request_results[request.request_id] = failure
                self._persist_control_locked()
                self._request_changed.notify_all()
                raise RuntimeError(
                    f"record request {request.request_id} was not accepted: {exc}") \
                    from exc

            session = None
            if self._believed_live(channel):
                session = self._start_requested_locked(
                    channel, request.request_id,
                    str((self._live_status.get(channel) or {}).get("title") or ""))
            state = STARTING if session is not None else "armed"

        if state == "armed":
            LOG.info("%s is not confirmed live; armed -- recording will start "
                     "when it is", channel)
        return {
            "state": state,
            "channel": channel,
            "request_id": request.request_id,
            "session_id": session.session_id if session is not None else "",
        }

    def _start_requested_locked(self, channel: str, request_id: str,
                                title: str = "") -> Session | None:
        """Verify one request generation immediately before recorder launch."""
        request = self._armed.get(channel)
        if request is None or request.request_id != request_id:
            return None
        if self._stop.is_set() or self._state == DRAINING:
            return None
        existing = self._recorders.get(channel)
        if existing and existing.running:
            if existing.request_token == request_id:
                return existing.session
            return None
        try:
            session = self.start_recording(channel, request_id=request_id)
        except Exception as exc:
            request.last_error = redact(str(exc))
            request.attempt_session_id = ""
            self._persist_control_locked()
            LOG.error("could not start %s for request %s: %s",
                      channel, request_id, exc)
            return None

        current = self._armed.get(channel)
        if current is not None and current.request_id == request_id:
            current.attempt_session_id = session.session_id
            current.last_error = ""
            self._persist_control_locked()
        if title:
            self.store.update(session, title=title)
        return session

    def _believed_live(self, channel: str) -> bool:
        """Is the cached live status both positive and recent enough to act on?

        Deliberately not a fresh probe: this runs on the dashboard's request
        thread, and a probe can block for the best part of a minute. A stale
        `True` costs one recorder start that streamlink resolves anyway; a stale
        `False` costs a few seconds of arming before the forced probe lands.
        """
        status = self._live_status.get(channel) or {}
        if status.get("state") != LIVE and not (
                "state" not in status and status.get("live")):
            return False
        interval = float(self.config.get("watcher.check_seconds", 60))
        age = time.time() - float(status.get("checked_at", 0.0))
        return age <= max(120.0, interval * 2)

    def armed_channels(self) -> list[str]:
        with self._lifecycle:
            return sorted(self._armed)

    def is_armed(self, channel: str) -> bool:
        # AUD2-048: every public channel method parses, so a caller holding a URL
        # or mixed-case form cannot silently miss the canonical entry. The CLI
        # armed `someone` and then asked about `https://twitch.tv/SomeOne`.
        with self._lifecycle:
            return parse_channel(channel) in self._armed

    def disarm(self, channel: str) -> bool:
        """Drop a pending record request. True if there was one."""
        with self._lifecycle:
            request = self._armed.get(parse_channel(channel))
            if request is None:
                return False
            self._finish_request_locked(
                request, "cancelled", error="record request was cancelled")
            return True

    def cancel_request(self, request_id: str) -> bool:
        """Cancel exactly one generation without touching a newer request."""
        with self._lifecycle:
            request = next((item for item in self._armed.values()
                            if item.request_id == request_id), None)
            if request is None:
                return False
            recorder = self._recorders.get(request.channel)
            if (recorder and recorder.running
                    and recorder.request_token == request_id):
                recorder.stop("stopped by user")
                self._auto_suppressed.add(request.channel)
            self._finish_request_locked(
                request, "cancelled", error="record request was cancelled",
                persist=False)
            self._persist_control_locked()
            return True

    def is_auto_suppressed(self, channel: str) -> bool:
        """True if an explicit Stop is holding auto-record off this broadcast."""
        with self._lifecycle:
            return parse_channel(channel) in self._auto_suppressed

    def _release_auto_suppression(self, channel: str) -> bool:
        """Lift an explicit Stop. True if one was in force.

        Called on the offline edge -- the stopped broadcast has ended, so the
        next one is fair game -- and on an explicit Record, which is a newer
        instruction than the Stop that set this.
        """
        with self._lifecycle:
            had = channel in self._auto_suppressed
            self._auto_suppressed.discard(channel)
            if had:
                self._persist_control_locked()
        return had

    def stop_recording(self, channel: str) -> None:
        """Stop a recording, or cancel one that is still waiting to start."""
        channel = parse_channel(channel)
        with self._lifecycle:
            recorder = self._recorders.get(channel)
            if recorder and recorder.running:
                recorder.stop("stopped by user")
                # Stopping is also a withdrawal of the request; without this the
                # watcher would helpfully start it again on its next pass.
                request = self._armed.get(channel)
                if request is not None:
                    self._finish_request_locked(
                        request, "cancelled", error="stopped by user",
                        persist=False)
                # ...and so would auto_record, for as long as the broadcast
                # stayed live. Suppress it until this broadcast ends.
                self._auto_suppressed.add(channel)
                self._persist_control_locked()
                return
            request = self._armed.get(channel)
            if request is not None:
                self._finish_request_locked(
                    request, "cancelled", error="stopped by user")
                LOG.info("%s: cancelled -- no longer waiting for it to go live",
                         channel)
                return
        raise RuntimeError(f"{channel} is not recording")

    def recording_channels(self) -> list[str]:
        with self._lifecycle:
            return [name for name, rec in self._recorders.items() if rec.running]

    # -- VODs --------------------------------------------------------------

    def download_vod(self, url: str, *, start: float | None = None,
                     duration: float | None = None) -> Session:
        """Download a Twitch VOD through the same pipeline as a live recording.

        streamlink pipes the VOD to the ffmpeg segmenter exactly as it does a live
        stream, so the download produces keyframe-aligned chunks, masters, proxies,
        rolling word-timed transcripts, rundowns and snapshots identical to a live
        session. `start`/`duration` (seconds) optionally fetch a sub-range of a long
        VOD via streamlink's `--hls-start-offset`/`--hls-duration`.
        """
        try:
            video_id, canonical = parse_vod(url)
        except InvalidVod as exc:
            raise RuntimeError(str(exc)) from exc
        if start is not None and (start != start or start < 0):
            raise RuntimeError("VOD start offset must be zero or a positive number")
        if duration is not None and (duration != duration or duration <= 0):
            raise RuntimeError("VOD duration must be a positive number")
        lock_key = f"vod-{video_id}"

        with self._lifecycle:
            if self._stop.is_set() or self._state == DRAINING:
                raise RuntimeError("the pipeline is shutting down")
            existing = self._vod_recorders.get(lock_key)
            if existing is not None and existing.running:
                raise RuntimeError(f"VOD {video_id} is already downloading")

        # Resolve the broadcaster and title outside the lifecycle lock -- it is a
        # network round-trip that can take the probe timeout. A definite failure
        # (deleted, subscriber-only, or geo-blocked with no proxy set) raises here
        # rather than starting a download that cannot succeed.
        author, title = self._probe_vod_metadata(canonical)
        channel = vod_dir_name(author, video_id)

        with self._lifecycle:
            if self._stop.is_set() or self._state == DRAINING:
                raise RuntimeError("the pipeline is shutting down")
            existing = self._vod_recorders.get(lock_key)
            if existing is not None and existing.running:
                raise RuntimeError(f"VOD {video_id} is already downloading")
            recorder = Recorder(
                self.config, self.tools, self.store, channel,
                source_kind=SOURCE_VOD, source_url=canonical, lock_key=lock_key,
                vod_start=start, vod_duration=duration,
                on_chunk_finalized=self._on_chunk_finalized,
                on_session_ended=self._on_vod_session_ended,
            )
            self._vod_recorders[lock_key] = recorder
            try:
                session = recorder.start()
            except Exception:
                if self._vod_recorders.get(lock_key) is recorder:
                    self._vod_recorders.pop(lock_key, None)
                raise
            if title:
                try:
                    self.store.update(session, title=title)
                except Exception:
                    LOG.exception("could not record VOD title")

        LOG.info("downloading VOD %s (%s) -> %s",
                 video_id, channel, session.directory)
        return session

    def stop_vod(self, session_id: str) -> None:
        """Stop an in-progress VOD download. The chunks captured so far survive."""
        with self._lifecycle:
            for recorder in self._vod_recorders.values():
                if (recorder.running and recorder.session is not None
                        and recorder.session.session_id == session_id):
                    recorder.stop("stopped by user")
                    return
        raise RuntimeError(f"no active VOD download for session {session_id}")

    def vod_downloads(self) -> list[str]:
        """Session ids of VOD downloads currently running."""
        with self._lifecycle:
            return [rec.session.session_id
                    for rec in self._vod_recorders.values()
                    if rec.running and rec.session is not None]

    def _active_recorder_for(self, session: Session) -> Recorder | None:
        """The running recorder producing this session, live or VOD, if any."""
        with self._lifecycle:
            recorder = self._recorders.get(session.channel)
            if (recorder is not None and recorder.session is session
                    and recorder.running):
                return recorder
            for recorder in self._vod_recorders.values():
                if recorder.session is session and recorder.running:
                    return recorder
        return None

    def _probe_vod_metadata(self, url: str) -> tuple[str, str]:
        """Resolve a VOD's broadcaster and title via `streamlink --json`.

        Returns (author, title); either may be empty when the probe is merely
        inconclusive (a timeout, an unparsable answer), in which case the download
        proceeds under a `vod_<id>` directory and the startup watchdog handles a
        genuinely dead VOD. A *definite* failure -- the plugin reporting an error
        with no streams -- raises, so the user is not left with an empty session.
        """
        cmd = [self.tools.streamlink, "--json", "--loglevel", "none"]
        cmd += proxy_args(self.config.get("network.proxy", "") or "")
        cmd += oauth_args(self.config.secret("twitch_oauth_token"))
        if self.config.get("recording.streamlink_no_config", False):
            cmd.append("--no-config")
        cmd.append(url)

        timeout = float(self.config.get("watcher.probe_timeout_seconds", 25))
        try:
            result = run(cmd, timeout=timeout)
        except Exception as exc:
            LOG.warning("VOD metadata probe did not answer within %.0fs (%s)",
                        timeout, type(exc).__name__)
            return "", ""
        try:
            payload = json.loads(result.stdout or "")
        except json.JSONDecodeError:
            return "", ""
        if not isinstance(payload, dict):
            return "", ""

        streams = payload.get("streams")
        metadata = payload.get("metadata")
        metadata = metadata if isinstance(metadata, dict) else {}
        author = str(metadata.get("author") or "")
        title = str(metadata.get("title") or "")
        error = str(payload.get("error") or "")
        if not streams and error:
            raise RuntimeError(f"cannot open this VOD: {redact(error)[:300]}")
        return author, title

    # -- events ------------------------------------------------------------

    def _on_chunk_finalized(self, session: Session, chunk: Chunk) -> None:
        """Chunk closed: finish its transcript, remux it, then queue its proxy.

        Deliberately one sequential job rather than three parallel ones. The final
        transcript slice reads the .ts, and the remux is what makes it safe to
        delete that .ts, so they cannot race.
        """
        job = self.jobs.submit(
            f"finalize:{session.session_id}:{chunk.label}",
            f"{session.channel} {chunk.label}: finish",
            "finalize",
            lambda item: self._claim_finalization(item, session, chunk),
        )
        if job is None and not self._job_active(
                self.jobs, f"finalize:{session.session_id}:{chunk.label}"):
            self.store.update_chunk(
                session, chunk, status=FAILED,
                master_error="finalisation could not be queued")

    def _claim_finalization(self, job: Job, session: Session,
                            chunk: Chunk) -> None:
        with self._chunk_lock(session, chunk):
            try:
                # P4: wait briefly rather than treating the very first contention
                # as permanent external ownership. A transient overlap -- a
                # recovery pass or a manual retranscribe holding the chunk lock for
                # a moment -- would otherwise poison the session as
                # externally-owned and abandon finalisation of media this process
                # actually owns. Only a genuine timeout means another process holds
                # it for real.
                lock = ResourceLock(
                    chunk_lock_path(session.path, chunk.label),
                    timeout=30.0).acquire()
            except ResourceBusy:
                self._externally_owned_sessions.add(session.session_id)
                LOG.info("%s/%s finalisation is owned by another process",
                         session.channel, chunk.label)
                return
            ownership = self._ownership(lock)
            try:
                self._finalize_chunk(
                    job, session, chunk, ownership=ownership)
            finally:
                ownership.populated()

    def _on_first_media(self, session: Session, request_id: str) -> None:
        """Consume only the request generation that launched this recorder."""
        if not request_id:
            return
        with self._lifecycle:
            request = self._armed.get(session.channel)
            recorder = self._recorders.get(session.channel)
            if (request is None or request.request_id != request_id
                    or recorder is None or recorder.session is not session
                    or recorder.request_token != request_id):
                return
            if (request.attempt_session_id
                    and request.attempt_session_id != session.session_id):
                return
            self._finish_request_locked(
                request, "complete", session_id=session.session_id)

    def _on_session_ended(self, session: Session, request_id: str = "") -> None:
        LOG.info("%s: session ended with %d chunk(s)",
                 session.channel, len(session.chunks))
        self._refresh_session_index(session)
        with self._lifecycle:
            recorder = self._recorders.get(session.channel)
            if recorder is not None and recorder.session is session:
                self._recorders.pop(session.channel, None)
            if not request_id:
                return
            request = self._armed.get(session.channel)
            if request is None or request.request_id != request_id:
                return
            # A callback persistence failure can leave intent pending even though
            # bytes existed. Resolve from the manifest/media before considering a
            # retry so the same broadcast is not recorded twice.
            if self._session_has_media(session):
                self._finish_request_locked(
                    request, "complete", session_id=session.session_id)
                return
            request.attempt_session_id = ""
            request.last_error = redact(
                session.error or "recording ended before any media arrived")
            self._persist_control_locked()
            if self._stop.is_set() or self._state == DRAINING:
                return
            if self._submit_forced_probe_locked(request) is None:
                request.last_error = (
                    f"{request.last_error}; retry probe could not be queued")
                self._persist_control_locked()
                LOG.error("%s request %s remains armed: retry probe could not "
                          "be queued", session.channel, request_id)

    def _on_vod_session_ended(self, session: Session) -> None:
        """A VOD download finished. No arming or watcher state to reconcile -- a
        VOD is a one-shot, not a channel the pipeline keeps trying to record."""
        LOG.info("VOD %s: download ended with %d chunk(s)",
                 session.channel, len(session.chunks))
        self._refresh_session_index(session)
        with self._lifecycle:
            for key, recorder in list(self._vod_recorders.items()):
                if recorder.session is session:
                    self._vod_recorders.pop(key, None)
                    break

    # -- work --------------------------------------------------------------

    def _require_space(self, needed_bytes: int, what: str) -> None:
        """Refuse disk-hungry post-processing that would breach the reserve.

        Capture outranks everything else here. A proxy transcode or a snapshot
        that fills the drive costs the live recording, which is the one thing
        that cannot be redone.
        """
        self.disk_budget.reserve(needed_bytes, what).release()

    def _chunk_lock(self, session: Session, chunk: Chunk) -> threading.RLock:
        key = f"{session.session_id}:{chunk.label}"
        with self._locks_guard:
            return self._chunk_locks.setdefault(key, threading.RLock())

    def _finalize_chunk(self, job: Job, session: Session, chunk: Chunk, *,
                        ownership: _OwnershipGroup | None = None) -> None:
        job.progress = "transcribing tail"
        transcript = self._finalize_transcript(session, chunk)

        job.progress = "stitching the chunk boundary"
        self._stitch_boundary(
            session, chunk,
            already_owned=((chunk,) if ownership is not None else ()),
        )

        job.progress = "remuxing"
        self._remux(session, chunk)

        job.progress = "queueing proxy"
        self._queue_proxy(session, chunk, ownership=ownership)

        job.progress = "queueing rundown"
        # AUD2-011: only summarise a transcript that actually finished. The
        # rundown header states the chunk's full duration, so summarising a
        # transcript that covers only its first ten minutes produces a document
        # that is wrong rather than partial -- and nothing downstream marks it as
        # such. If the catch-up was blocked, the rundown waits for a rebuild.
        if transcript.complete:
            self._queue_summary(session, chunk, ownership=ownership)
        else:
            self.store.update_chunk(
                session, chunk, summary_status=SKIPPED,
                summary_error=f"no rundown: "
                              f"{transcript.detail or 'the transcript is incomplete'}")
            LOG.warning("%s/%s: skipping the rundown -- its transcript is "
                        "incomplete", session.channel, chunk.label)

        self._refresh_session_index(session)
        job.progress = ""

        # Only the artifacts this job is actually responsible for. The proxy and
        # the rundown now run on their own pool and report their own failures;
        # reading their state here would either race them or report a stale value.
        failures = {name: text for name, text in chunk.errors.items()
                    if name in ("master", "transcript")}
        if failures:
            raise RuntimeError("; ".join(f"{name}: {text}"
                                         for name, text in failures.items()))

    def _verify_before_reclaim(self, session: Session, chunk: Chunk,
                               master: Path, source: Path | None) -> None:
        """Prove a master reads end to end, but only when a `.ts` is at stake.

        *2026-08-18.* The deep read costs one pass over the file. It is worth
        paying exactly at the moment the recording is about to exist in one copy
        only, and not worth paying on every start for every master that has
        already outlived its source -- recovery walks every session on disk, and
        re-reading fifty gigabytes of finished masters to learn nothing would
        turn startup into a disk scrub.

        So the rule is: **a master is deep-verified precisely when its `.ts`
        would be deleted next.** Anywhere else, the header validation stands.
        """
        if source is None or not source.is_file():
            return
        if not self.config.get("recording.verify_master", True):
            return
        verify_master_readable(self.tools, master)

    def _remux(self, session: Session, chunk: Chunk) -> None:
        source = session.path / "live" / chunk.ts_name
        destination = session.path / "master" / chunk.master_name

        if destination.exists():
            # A master from an earlier attempt is only trustworthy if it still
            # validates; a half-written one from a crash must not be adopted.
            try:
                validate_master(
                    self.tools, destination, chunk.duration,
                    source=(source if source.is_file() else None))
                self._verify_before_reclaim(
                    session, chunk, destination,
                    source if source.is_file() else None)
            except Exception as exc:
                if not source.exists():
                    # Nothing to rebuild from, so deleting this would turn "did
                    # not validate" into "certainly gone". Validation can fail
                    # because the file is corrupt *or* because ffprobe timed out
                    # once, and those are indistinguishable from here -- the
                    # first costs nothing to keep, the second is a whole VOD.
                    LOG.error("%s/%s: master did not validate and there is no .ts "
                              "to rebuild it from; keeping it as-is (%s)",
                              session.channel, chunk.label, exc)
                    self.store.update_chunk(
                        session, chunk, status=FAILED,
                        master_error=f"master did not validate and could not be "
                                     f"rebuilt (no .ts remains): {exc}")
                    return
                # Left on disk deliberately. remux_to_mp4() stages a .partial and
                # only replaces this file once the replacement itself validates,
                # so unlinking first would risk ending up with neither.
                LOG.warning("%s/%s: master did not validate (%s); rebuilding "
                            "from %s", session.channel, chunk.label, exc,
                            source.name)
            else:
                width, height = video_dimensions(self.tools, destination)
                self.store.update_chunk(session, chunk, status=COMPLETE,
                                        master_error="", width=width, height=height)
                self._reclaim_ts(session, chunk, source)
                return

        if not source.exists():
            self.store.update_chunk(session, chunk, status=FAILED,
                                    master_error="recording file is missing")
            return

        try:
            self._remux_with_retries(session, chunk, source, destination)
        except Exception as exc:
            # The .ts is deliberately left in place: it is now the only copy.
            LOG.error("%s/%s: remux failed, keeping %s: %s",
                      session.channel, chunk.label, source.name, exc)
            self.store.update_chunk(session, chunk, status=FAILED,
                                    master_error=str(exc))
            return

        size = destination.stat().st_size
        width, height = video_dimensions(self.tools, destination)
        self.store.update_chunk(session, chunk, status=COMPLETE, size_bytes=size,
                                master_error="", width=width, height=height)
        LOG.info("%s/%s: master ready (%s, %s)", session.channel, chunk.label,
                 f"{width}x{height}" if height else "unknown size",
                 human_bytes(size))
        self._reclaim_ts(session, chunk, source)

    def _remux_with_retries(self, session: Session, chunk: Chunk,
                            source: Path, destination: Path) -> None:
        """Build the master, giving a failed attempt another go.

        *2026-08-18.* One remux of a clean two-hour capture died on an ffmpeg
        assertion -- ``next_dts <= 0x7fffffff`` in movenc's
        `get_cluster_duration`, which is one sample's duration overflowing a
        32-bit field and therefore a DTS delta of more than six hours inside a
        two-hour file. The recording could not contain such a jump, and re-running
        the identical command over the identical bytes afterwards produced a
        perfect master, so whatever produced the number was not in the file. The
        chunk was left as a `.ts` with `master_error` set and its proxy failed
        behind it with "master is missing".

        The precedent is `ClaudeCliModel.ask`: an attempt whose failure cannot be
        classified from out here is still worth repeating when repeating it is
        bounded and the alternative is losing the artifact for good. A remux is
        ~40s, so the whole budget is a couple of minutes on a pool of three, and
        each attempt stages its own `.partial.mp4` and cleans it up -- there is no
        state carried between them.

        A refusal from the disk guard is retried too, and costs nothing to retry:
        the reservation is taken before anything is read, so a full drive fails in
        milliseconds -- and a peer job releasing its own reservation in between is
        the one way that failure resolves itself.
        """
        attempts = max(1, int(self.config.get("recording.remux_attempts", 3)))
        verify = bool(self.config.get("recording.verify_master", True))
        last: Exception | None = None
        for attempt in range(attempts):
            try:
                # The MP4 lives beside the .ts until the remux validates, so the
                # transient requirement is roughly the chunk over again.
                with self.disk_budget.reserve(
                        source.stat().st_size, f"remuxing {chunk.label}"):
                    remux_to_mp4(self.tools, source, destination, chunk.duration,
                                 verify=verify)
                return
            except Exception as exc:
                last = exc
                if attempt == attempts - 1:
                    break
                LOG.warning("%s/%s: remux attempt %d/%d failed (%s); rebuilding "
                            "the master from %s again",
                            session.channel, chunk.label, attempt + 1, attempts,
                            exc, source.name)
        raise last or RuntimeError("remux failed")

    def _reclaim_ts(self, session: Session, chunk: Chunk, source: Path) -> None:
        """Drop the working copy. Only ever called after the master validated."""
        if self.config.get("recording.keep_ts_after_remux", False):
            return
        key = str(source.resolve(strict=False))
        with self._lease_guard:
            if self._leases.get(key):
                # A snapshot is reading it. Deleting now would fail that cut for
                # no benefit -- the master is already safe on disk.
                self._deferred_deletes.add(key)
                LOG.info("%s is leased by a snapshot; deferring its removal",
                         source.name)
                return
            try:
                lock = ResourceLock(
                    media_lock_path(self.config.masters_root, source)).acquire()
            except ResourceBusy:
                self._deferred_deletes.add(key)
                LOG.info("%s is leased by another process; deferring its removal",
                         source.name)
                return
            try:
                self._delete_ts(source)
                self._deferred_deletes.discard(key)
            finally:
                lock.release()

    def _drain_deferred_deletes(self) -> None:
        with self._lease_guard:
            for key in list(self._deferred_deletes):
                if self._leases.get(key):
                    continue
                source = Path(key)
                try:
                    lock = ResourceLock(
                        media_lock_path(self.config.masters_root, source)).acquire()
                except ResourceBusy:
                    continue
                try:
                    LOG.info("final media lease released; removing deferred %s",
                             source.name)
                    self._delete_ts(source)
                    self._deferred_deletes.discard(key)
                finally:
                    lock.release()

    def _delete_ts(self, source: Path) -> None:
        try:
            source.unlink(missing_ok=True)
        except OSError as exc:
            LOG.warning("could not remove %s: %s", source.name, exc)

    def _queue_proxy(self, session: Session, chunk: Chunk, *,
                     ownership: _OwnershipGroup | None = None) -> Job | None:
        """Proxies run off the transcript lock.

        AUD3-001: this used to inherit finalisation ownership (or take the chunk
        mutation lock itself), so a 2-hour chunk's encode held that lock for the
        ten minutes it ran. Every other holder is written around it being held
        briefly -- the rundown waits 60 seconds and then fails, which is exactly
        what happened to c003 of the 2026-08-16 recording, and a retranscribe
        would have been locked out just as long.

        A proxy derives from a finished master and mutates no transcript
        generation, so it has no business holding that lock at all. What does
        need excluding is a second encoder writing the same staged file, and
        `_make_proxy_inner` locks the proxy's own output path for that.
        `ownership` is accepted and deliberately not inherited, as in
        `_queue_summary`.
        """
        if not self.config.get("proxies.enabled", True):
            self.store.update_chunk(session, chunk, proxy_status=SKIPPED)
            self._refresh_session_index(session)
            return None
        key = f"proxy:{session.session_id}:{chunk.label}"
        job = self.media_jobs.submit(
            key,
            f"{session.channel} {chunk.label}: proxy",
            "proxy",
            lambda item: self._make_proxy(item, session, chunk),
        )
        if job is None and not self._job_active(self.media_jobs, key):
            self.store.update_chunk(
                session, chunk, proxy_status=ERROR,
                proxy_error="proxy could not be queued")
        self._refresh_session_index(session)
        return job

    def _queue_summary(self, session: Session, chunk: Chunk, *,
                       ownership: _OwnershipGroup | None = None) -> Job | None:
        """Rundowns run off the critical path.

        A `claude -p` call can take a quarter of an hour. Running it inline in
        chunk finalisation held one of three capture-critical workers for that
        whole time, and rolling transcription for a live channel queued up behind
        it.
        """
        if not self._summary_enabled():
            self.store.update_chunk(session, chunk, summary_status=SKIPPED,
                                    summary_error="")
            self._refresh_session_index(session)
            return None

        source, reason = self._summary_source(session, chunk)
        rundown = self.transcriber.output_dir(session, chunk) / "rundown.md"
        if source is None:
            try:
                self._retire_rundown(rundown, reason)
            except Exception as exc:
                self._summary_retirement_error(session, chunk, exc)
                return None
            self.store.update_chunk(
                session, chunk, summary_status=SKIPPED,
                summary_error=f"no rundown: {reason}")
            self._refresh_session_index(session)
            return None
        try:
            self._retire_obsolete_rundown(
                session, chunk, source.generation,
                "it belongs to an older transcript generation")
        except Exception as exc:
            self._summary_retirement_error(session, chunk, exc)
            return None

        # The provider call intentionally owns no transcript lock. It can run for
        # minutes; retranscription must be able to replace the source meanwhile.
        # Only the final generation recheck and rundown commit are locked.
        key = (f"summary:{session.session_id}:{chunk.label}:"
               f"{source.generation}")
        job = self.media_jobs.submit(
            key,
            f"{session.channel} {chunk.label}: rundown",
            "summary",
            lambda item: self._summarize(
                session, chunk, source.generation),
        )
        if job is None and not self._job_active(self.media_jobs, key):
            self.store.update_chunk(
                session, chunk, summary_status=ERROR,
                summary_error="rundown could not be queued")
        elif job is not None:
            self.store.update_chunk(session, chunk, summary_status=PENDING,
                                    summary_error="")
        self._refresh_session_index(session)
        return job

    def _make_proxy(self, job: Job, session: Session, chunk: Chunk) -> None:
        """AUD2-053: every failure here must reach the proxy artifact state.

        Only the encode itself used to be covered. A refusal from the disk guard,
        a `master.stat()` error or a failing encoder probe all raised before the
        `try`, so the job failed while the chunk still read `pending` with no
        proxy error -- and after job pruning or a restart the dashboard could not
        say why the proxy was missing.
        """
        try:
            self._make_proxy_inner(job, session, chunk)
        except Exception as exc:
            if chunk.proxy_status != ERROR:
                self.store.update_chunk(session, chunk, proxy_status=ERROR,
                                        proxy_error=str(exc))
            raise
        finally:
            self._refresh_session_index(session)

    def _make_proxy_inner(self, job: Job, session: Session, chunk: Chunk) -> None:
        master = session.path / "master" / chunk.master_name
        if not master.exists():
            raise RuntimeError("master is missing; cannot build a proxy")

        folder = self.config.get("proxies.folder_name", "Proxies")
        suffix = self.config.get("proxies.suffix", "_Proxy")
        destination = (master.parent / folder / f"{master.stem}{suffix}.mp4")
        height = int(self.config.get("proxies.height", 540))

        # Anchored on the output rather than the chunk: two encoders staging the
        # same `.partial.mp4` is the only conflict a proxy actually has, and this
        # keeps a ten-minute encode off the transcript mutation lock. Held across
        # the adopt-or-rebuild decision so a peer cannot commit a proxy between
        # this process validating the absent one and writing its own.
        try:
            proxy_lock = ResourceLock(
                media_lock_path(self.config.masters_root, destination)).acquire()
        except ResourceBusy as exc:
            raise RuntimeError(
                f"{destination.name} is already being built by another "
                "pipeline") from exc
        try:
            self._build_proxy(job, session, chunk, master, destination, height)
        finally:
            proxy_lock.release()

    def _master_damage(self, master: Path) -> str:
        """Why the master cannot be read end to end, or "" if it can.

        Only ever called on a failure path, so the full read it costs is paid
        once, to replace a wrong diagnosis with the right one.
        """
        try:
            verify_master_readable(self.tools, master)
        except Exception as exc:
            return str(exc)
        return ""

    def _build_proxy(self, job: Job, session: Session, chunk: Chunk,
                     master: Path, destination: Path, height: int) -> None:
        if destination.is_file():
            try:
                validate_proxy(
                    self.tools, master, destination, height=height)
            except Exception as exc:
                LOG.warning("%s/%s: existing proxy is unusable (%s); rebuilding it",
                            session.channel, chunk.label, exc)
            else:
                self.store.update_chunk(session, chunk, proxy_status=DONE,
                                        proxy_name=destination.name, proxy_error="")
                return

        options = dict(
            height=height,
            quality=int(self.config.get("proxies.quality", 24)),
            audio_bitrate=self.config.get("proxies.audio_bitrate", "128k"),
        )
        reservation = self.disk_budget.reserve(
            estimate_proxy_peak_bytes(self.tools, master, **options),
            f"proxy for {chunk.label}")
        try:
            self.store.update_chunk(session, chunk, proxy_status=RUNNING)
            job.progress = "encoding"
            preference = self.config.get("proxies.encoder", "auto")
            encoder = probe_encoder(self.tools, preference)
            try:
                make_proxy(self.tools, master, destination,
                           encoder=encoder, **options)
            except Exception as exc:
                # Before blaming the encoder, ask whether the master is readable
                # at all. *2026-08-18:* two proxies were built from masters whose
                # index stopped a third of the way in, so both encoders produced
                # the same short output and the pipeline reported
                # "h264_amf failed on real media", fell back, and spent another
                # five minutes proving libx264 could not read the file either.
                # An encoder cannot encode frames its input will not hand over.
                damage = self._master_damage(master)
                if damage:
                    detail = (f"the master is damaged, so no encoder can build a "
                              f"proxy from it ({damage}); the encode stopped "
                              f"early: {exc}")
                    self.store.update_chunk(session, chunk, proxy_status=ERROR,
                                            proxy_error=detail)
                    LOG.error("%s/%s: %s", session.channel, chunk.label, detail)
                    raise RuntimeError(detail) from exc
                # The two-second probe only proves the encoder initialises. Real
                # source media can still defeat a hardware encoder, and a missing
                # proxy is worse than a slow one -- so in `auto` mode, fall back.
                if preference != "auto" or encoder == "libx264":
                    self.store.update_chunk(session, chunk, proxy_status=ERROR,
                                            proxy_error=str(exc))
                    raise
                LOG.warning("%s/%s: %s failed on real media (%s); retrying with libx264",
                            session.channel, chunk.label, encoder, exc)
                job.progress = "encoding (software fallback)"
                try:
                    make_proxy(self.tools, master, destination,
                               encoder="libx264", **options)
                except Exception as fallback_exc:
                    self.store.update_chunk(session, chunk, proxy_status=ERROR,
                                            proxy_error=str(fallback_exc))
                    raise

            self.store.update_chunk(session, chunk, proxy_status=DONE,
                                    proxy_name=destination.name, proxy_error="")
            LOG.info("%s/%s: proxy ready (%s)", session.channel, chunk.label,
                     human_bytes(destination.stat().st_size))
        finally:
            reservation.release()

    def _transcription_ready(self, session: Session, chunk: Chunk) -> bool:
        """Preconditions checked before queueing, so failures are immediate."""
        if not self.config.get("transcription.enabled", True):
            self.store.update_chunk(session, chunk, transcript_status=SKIPPED)
            return False
        if not self.config.secret("deepgram_api_key"):
            self.store.update_chunk(session, chunk, transcript_status=SKIPPED,
                                    transcript_error="no Deepgram API key configured")
            return False
        return True

    def _advance_transcript(self, session: Session, chunk: Chunk):
        """One rolling pass. Returns an AdvanceResult, never raises."""
        from .transcribe import BLOCKED, AdvanceResult

        if not self._transcription_ready(session, chunk):
            return AdvanceResult(BLOCKED, detail="transcription unavailable")

        before = {chunk.index: self._current_export_generation(session, chunk)}
        with self._chunk_lock(session, chunk):
            try:
                mutation = ResourceLock(
                    chunk_lock_path(session.path, chunk.label)).acquire()
            except ResourceBusy:
                self._externally_owned_sessions.add(session.session_id)
                return AdvanceResult(BLOCKED, detail="chunk is owned externally")
            try:
                try:
                    result = self.transcriber.advance(session, chunk)
                except Exception as exc:
                    LOG.error("%s/%s: transcription failed: %s",
                              session.channel, chunk.label, exc)
                    self.store.update_chunk(
                        session, chunk, transcript_status=ERROR,
                        transcript_error=str(exc))
                    result = AdvanceResult(BLOCKED, detail=str(exc))
                self._reconcile_generation_changes(
                    session, [chunk], before, queue=False)
            finally:
                mutation.release()
        return result

    def _finalize_transcript(self, session: Session, chunk: Chunk):
        """Catch the chunk up to its full duration before anything reads it."""
        from .transcribe import BLOCKED, AdvanceResult

        if not self._transcription_ready(session, chunk):
            return AdvanceResult(BLOCKED, detail="transcription unavailable")

        before = {chunk.index: self._current_export_generation(session, chunk)}
        # The lock is held across the whole catch-up so a rolling pass cannot
        # interleave and rewrite the cursor underneath it.
        with self._chunk_lock(session, chunk):
            try:
                result = self.transcriber.finalize(session, chunk)
            except Exception as exc:
                LOG.error("%s/%s: transcription failed: %s",
                          session.channel, chunk.label, exc)
                self.store.update_chunk(session, chunk, transcript_status=ERROR,
                                        transcript_error=str(exc))
                return AdvanceResult(BLOCKED, detail=str(exc))
            self._reconcile_generation_changes(
                session, [chunk], before, queue=False)

        if not result.complete:
            LOG.warning("%s/%s: transcript is NOT complete (%.0fs of %.0fs): %s",
                        session.channel, chunk.label, result.covered_through,
                        result.expected, result.detail or result.status)
        return result

    def _stitch_boundary(self, session: Session, chunk: Chunk, *,
                         already_owned: Iterable[Chunk] = ()) -> None:
        """Repair words spoken across the join with the previous chunk.

        Runs after this chunk's transcript is complete, and rewrites the tail of
        the previous chunk's transcript as well as this one's head. Any generation
        that changes has its rundown retired and regenerated from the new words.
        """
        if not self.config.get("transcription.enabled", True):
            return
        if not self.config.secret("deepgram_api_key"):
            return
        previous = session.chunk(chunk.index - 1)
        if previous is None:
            return

        tracked = [previous, chunk]
        # Every words read and both possible writes sit under kernel mutation
        # locks. Finalisation already owns the current chunk, so do not try to
        # acquire that non-reentrant cross-process lock a second time.
        with self._transcript_mutation_locks(
                session, tracked, already_owned=already_owned):
            before = {item.index: self._current_export_generation(session, item)
                      for item in tracked}
            try:
                self.transcriber.stitch_with_previous(session, chunk)
            except Exception as exc:
                # Never fails the chunk: the transcripts are already published and
                # usable, and this only improves one word at the boundary.
                LOG.warning("%s/%s: boundary stitch skipped: %s",
                            session.channel, chunk.label, exc)
            self._reconcile_generation_changes(
                session, tracked, before, queue=True)

    def _summarize(self, session: Session, chunk: Chunk,
                   generation: str | None = None) -> None:
        """AUD2-052: the whole operation is one artifact-state boundary.

        Only summarizer construction and the provider call used to be covered.
        Reading the transcript after the existence check, and writing the rundown
        after generation, both sat outside -- so a disk-full or locked-file
        failure at the commit left `summary_status='running'` with no error,
        forever, beside a stale rundown describing an older transcript.
        """
        if generation is None and not self._summary_enabled():
            self.store.update_chunk(session, chunk, summary_status=SKIPPED,
                                    summary_error="")
            self._refresh_session_index(session)
            return
        if generation is None:
            source, reason = self._summary_source(session, chunk)
            if source is None:
                # Direct callers still go through the same eligibility contract.
                try:
                    self._retire_rundown(
                        self.transcriber.output_dir(session, chunk) / "rundown.md",
                        reason)
                except Exception as exc:
                    self._summary_retirement_error(session, chunk, exc)
                    raise
                self.store.update_chunk(
                    session, chunk, summary_status=SKIPPED,
                    summary_error=f"no rundown: {reason}")
                self._refresh_session_index(session)
                return
            generation = source.generation
        try:
            self._summarize_inner(session, chunk, generation)
        except Exception as exc:
            current, _ = self._summary_source(session, chunk)
            if current is None or current.generation != generation:
                LOG.info("%s/%s: ignoring failure from stale rundown generation %s",
                         session.channel, chunk.label, generation)
                return
            LOG.error("%s/%s: rundown failed: %s",
                      session.channel, chunk.label, exc)
            self.store.update_chunk(session, chunk, summary_status=ERROR,
                                    summary_error=str(exc))
            raise
        finally:
            self._refresh_session_index(session)

    def _summarize_inner(self, session: Session, chunk: Chunk,
                         generation: str) -> None:
        rundown = self.transcriber.output_dir(session, chunk) / "rundown.md"

        # "none" is how a user turns rundowns off; treating it as a failure
        # produced an error on every chunk for a working configuration. An
        # existing rundown is deliberately left alone here: turning the feature
        # off is not a request to delete work already done.
        if (not self.config.get("summary.enabled", True)
                or (self.config.get("summary.provider") or "").lower() == "none"):
            self.store.update_chunk(session, chunk, summary_status=SKIPPED,
                                    summary_error="")
            return

        source, reason = self._summary_source(session, chunk)
        if source is None:
            # AUD2-052: the transcript is no longer eligible (retranscribed to
            # empty, or gone). Reach a terminal state and retire any stale rundown
            # rather than returning silently and leaving the chunk stuck at
            # pending/running forever. This is "nothing to summarise", not a fault,
            # so it is SKIPPED with a reason -- consistent with the direct path.
            try:
                self._retire_rundown(rundown, reason)
            except Exception as exc:
                self._summary_retirement_error(session, chunk, exc)
                raise
            self.store.update_chunk(session, chunk, summary_status=SKIPPED,
                                    summary_error=f"no rundown: {reason}")
            return
        if source.generation != generation:
            # A newer transcript generation exists; its own summary job owns it.
            return
        self._retire_obsolete_rundown(
            session, chunk, generation,
            "it belongs to an older transcript generation")

        self.store.update_chunk(session, chunk, summary_status=RUNNING)
        header = build_header(session.channel, session.session_id, chunk.label,
                              chunk.session_offset, chunk.duration, session.started_at)
        summarizer = build_summarizer(self.config, self.tools.claude)
        summary_text = summarizer.summarize(source.body, header)

        # Recheck under the same short mutation lock as the commit. A replacement
        # cannot land between this check and the atomic rundown write.
        with self._chunk_lock(session, chunk):
            mutation = ResourceLock(
                chunk_lock_path(session.path, chunk.label), timeout=60.0).acquire()
            try:
                current, _ = self._summary_source(session, chunk)
                if current is None or current.generation != generation:
                    return
                if not self._summary_enabled():
                    self.store.update_chunk(
                        session, chunk, summary_status=SKIPPED,
                        summary_error="")
                    return
                write_rundown(rundown, summary_text, header, generation)
                self.store.update_chunk(session, chunk, summary_status=DONE,
                                        summary_error="")
            finally:
                mutation.release()

    def _retire_rundown(self, rundown: Path, why: str) -> None:
        """Remove a rundown that no longer describes the transcript beside it."""
        if not rundown.exists():
            return
        rundown.unlink()
        LOG.info("removed %s: %s", rundown, why)

    def _require_manual_mutation_owner(self, session: Session,
                                       operation: str) -> None:
        if session.session_id in self._externally_owned_sessions:
            raise RuntimeError(
                f"cannot {operation} {session.channel}/{session.session_id}: "
                "the live session is owned by another pipeline")

    def resummarize(self, session: Session, chunk: Chunk) -> Job | None:
        if self.draining:
            raise RuntimeError("the pipeline is shutting down")
        self._require_manual_mutation_owner(session, "re-summarize")
        capable, reason = self._summary_capability()
        if not capable:
            raise RuntimeError(reason)
        source, reason = self._summary_source(session, chunk)
        if source is None:
            raise RuntimeError(f"cannot write a rundown: {reason}")
        return self._queue_summary(session, chunk)

    @contextmanager
    def _transcript_mutation_locks(
        self, session: Session, chunks: Iterable[Chunk], *,
        already_owned: Iterable[Chunk] = (),
    ) -> Iterator[None]:
        """Own several transcript generations in deterministic chunk order."""
        ordered = sorted({item.index: item for item in chunks}.values(),
                         key=lambda item: item.index)
        owned = {item.index for item in already_owned}
        if not owned.issubset({item.index for item in ordered}):
            raise ValueError("already-owned chunks must be part of the lock set")
        process_locks = [self._chunk_lock(session, item) for item in ordered]
        kernel_locks: list[ResourceLock] = []
        try:
            for lock in process_locks:
                lock.acquire()
            for item in ordered:
                if item.index in owned:
                    continue
                try:
                    kernel_locks.append(ResourceLock(
                        chunk_lock_path(session.path, item.label),
                        timeout=60.0).acquire())
                except ResourceBusy as exc:
                    raise RuntimeError(
                        f"{session.channel}/{item.label} is owned by another "
                        "pipeline") from exc
            yield
        finally:
            for lock in reversed(kernel_locks):
                lock.release()
            for lock in reversed(process_locks):
                lock.release()

    @staticmethod
    def _transcript_artifact_state(chunk: Chunk) -> dict[str, Any]:
        return {
            "transcribed_through": chunk.transcribed_through,
            "word_count": chunk.word_count,
            "transcript_status": chunk.transcript_status,
            "transcript_error": chunk.transcript_error,
            "summary_status": chunk.summary_status,
            "summary_error": chunk.summary_error,
        }

    def _snapshot_transcript_artifacts(
        self, session: Session, chunks: Iterable[Chunk],
    ) -> tuple[list[tuple[Path, dict[str, str], tuple[str, ...]]],
               dict[int, dict[str, Any]]]:
        """Capture the exact rollback set, including generation-bound rundowns.

        A generation spans the chunk folder and its `source/`, so the rollback
        set does too -- both halves in one transaction, or a failed seam could
        put back a `premiere.json` describing words that `source/words.json` no
        longer holds. The rundown is owned here and nowhere else: it is derived
        after the generation commits, but it *describes* that generation, so a
        seam that rolls back must take it with the words it was written from.
        """
        publications: list[tuple[Path, dict[str, str], tuple[str, ...]]] = []
        states: dict[int, dict[str, Any]] = {}
        for item in sorted(chunks, key=lambda target: target.index):
            directory = self.transcriber.output_dir(session, item)
            # Reconcile a preceding interrupted publication before taking bytes
            # that may later become rollback authority.
            load_words(self.transcriber.words_path(session, item))
            rendered = {
                name: (directory / name).read_text(encoding="utf-8")
                for name in (*GENERATION_FILES, "rundown.md")
                if (directory / name).is_file()
            }
            edit, source = split_publication(directory, rendered)
            publications.append(
                (edit[0], edit[1], (*EDIT_OWNED, "rundown.md")))
            publications.append(source)
            states[item.index] = self._transcript_artifact_state(item)
        return publications, states

    def _restore_transcript_artifacts(
        self, session: Session, chunks: Iterable[Chunk],
        publications: list[tuple[Path, dict[str, str], tuple[str, ...]]],
        states: dict[int, dict[str, Any]],
    ) -> None:
        publish_text_sets(publications)
        for item in chunks:
            self.store.update_chunk(session, item, **states[item.index])

    def retranscribe(self, session: Session, chunk: Chunk) -> Job | None:
        """Discard a chunk's transcript and rebuild it from scratch.

        Preconditions are checked before queueing so a missing key or a chunk that
        is still recording fails immediately rather than occupying a worker. The
        old loop ran `while advance() >= 0`, and since every failure mode returned
        0, any of them span forever and starved the pool.
        """
        if self.draining:
            raise RuntimeError("the pipeline is shutting down")
        self._require_manual_mutation_owner(session, "re-transcribe")
        if chunk.status in (STARTING, RECORDING):
            raise RuntimeError(
                f"{chunk.label} is still recording; stop it or wait for the chunk "
                "to close before re-transcribing")
        if not self.config.get("transcription.enabled", True):
            raise RuntimeError("transcription is disabled")
        if not self.config.secret("deepgram_api_key"):
            raise RuntimeError("no Deepgram API key configured")
        if self.transcriber.source_for(session, chunk) is None:
            raise RuntimeError(f"no media on disk for {chunk.label}")

        def work(job: Job) -> None:
            # Reset and rebuild under one lock, so a rolling pass cannot land in
            # the middle and blend an old transcript with a new one.
            #
            # The old transcript is moved aside rather than deleted. A rebuild
            # that fails half way used to leave the chunk with no words file and a
            # set of exports from a run that no longer existed; now the previous
            # transcript is put back and re-published, so a failed attempt costs
            # nothing.
            previous_chunk = session.chunk(chunk.index - 1)
            following_chunk = session.chunk(chunk.index + 1)
            tracked = [item for item in (previous_chunk, chunk, following_chunk)
                       if item is not None]
            with self._transcript_mutation_locks(session, tracked):
                before = {
                    item.index: self._current_export_generation(session, item)
                    for item in tracked
                }
                rollback, restore_states = self._snapshot_transcript_artifacts(
                    session, tracked)
                previous = self.transcriber.stash_words(session, chunk)
                # AUD2-051: the artifact state is part of what rollback restores.
                # Only the words file was stashed, so a failed rebuild put the old
                # transcript back and set the status to `done` while leaving the
                # failed attempt's `transcript_error` in place -- the chunk then
                # reported a good canonical transcript and an artifact error at
                # the same time, and the CLI called that session failed.
                self.store.update_chunk(session, chunk, transcribed_through=0.0,
                                        word_count=0, transcript_error="",
                                        transcript_status=RUNNING)
                try:
                    result = self.transcriber.finalize(session, chunk)
                    if not result.complete:
                        raise RuntimeError(
                            f"re-transcription stopped at "
                            f"{result.covered_through:.0f}s of "
                            f"{result.expected:.0f}s: "
                            f"{result.detail or result.status}")
                    if self.config.get(
                            "transcription.stitch_chunk_boundaries", True):
                        # The left and right seam passes are one success boundary.
                        # Empty/provider/build/publication failures raise in
                        # strict mode instead of silently discarding an old repair.
                        if previous_chunk is not None:
                            self.transcriber.stitch_with_previous(
                                session, chunk, strict=True)
                        if following_chunk is not None:
                            self.transcriber.stitch_with_previous(
                                session, following_chunk, strict=True)
                    changed = self._reconcile_generation_changes(
                        session, tracked, before, queue=False)
                except Exception:
                    restored = False
                    try:
                        self._restore_transcript_artifacts(
                            session, tracked, rollback, restore_states)
                        restored = True
                    finally:
                        if restored:
                            self.transcriber.discard_stash(previous)
                    raise
                else:
                    self.transcriber.discard_stash(previous)

            regenerate = {item.index: item for item in changed}
            # An identical re-transcription keeps the same generation, but a
            # manual request should still be allowed to refresh its rundown.
            regenerate[chunk.index] = chunk
            for target in (regenerate[index] for index in sorted(regenerate)):
                self._queue_summary(session, target)

        return self.jobs.submit(
            f"retranscribe:{session.session_id}:{chunk.label}",
            f"{session.channel} {chunk.label}: re-transcribe",
            "transcribe",
            work,
        )

    # -- snapshots ---------------------------------------------------------

    def snapshot(self, request: SnapshotRequest) -> SnapshotResult:
        """Cut a range out of a session, synchronously. Used by the CLI.

        The dashboard uses `queue_snapshot()` instead: ffprobe and ffmpeg on an
        HTTP thread meant the browser waited on an encode, and nothing bounded how
        many of those a user could start at once.
        """
        if self.draining:
            raise RuntimeError("the pipeline is shutting down")
        session = self._snapshot_session(request)
        start, end = self.snapshots.resolve_range(session, request)
        parts, _ = self.snapshots.plan(session, start, end)
        with self.disk_budget.reserve(
                self._snapshot_peak_bytes(
                    session, parts, precise=request.precise), "snapshot"):
            result = self.snapshots.create(session, request)
        self._after_snapshot(session, request, result)
        return result

    def queue_snapshot(self, request: SnapshotRequest) -> Job:
        """Validate now, cut on the snapshot pool. Returns the queued job.

        Everything that can be checked cheaply -- the session exists, the range is
        real, the caps are not breached -- happens here, so the caller gets a clear
        error rather than a job that fails a minute later.
        """
        session = self._snapshot_session(request)
        # Proves the range resolves and is covered end to end before queueing.
        start, end = self.snapshots.resolve_range(session, request)
        parts, _ = self.snapshots.plan(session, start, end)

        # AUD2-066: queue the *resolved* range, not the relative request. The
        # worker calls create(), which resolves again -- so "the last 10 minutes"
        # sitting behind another job meant the last 10 minutes as of whenever it
        # eventually ran. An accepted 40-100s range came out as 70-130s after a
        # 30-second wait, and the job label described a range the file did not
        # contain. Where the media is readable can still change by then, so the
        # plan is redone under a lease at execution; the user's intent is not.
        frozen = replace(request, last_minutes=None, start=start, end=end)

        key = (f"snapshot:{session.session_id}:"
               f"{int(start * 1000)}-{int(end * 1000)}:{secrets.token_hex(8)}")
        return self._admit_snapshot(
            session, frozen, parts, key, persist_intent=True)

    def _admit_snapshot(
        self,
        session: Session,
        request: SnapshotRequest,
        parts: list[tuple[Path, float, float]],
        key: str,
        *,
        persist_intent: bool,
    ) -> Job:
        start = float(request.start or 0.0)
        end = float(request.end or 0.0)
        # Checking the cap and queueing under one lock. Separately, two requests
        # arriving together could both see room and both be admitted.
        with self._snapshot_admission:
            if self.draining:
                raise RuntimeError("the pipeline is shutting down")
            self._check_snapshot_capacity(session)
            reservation = self.disk_budget.reserve(
                self._snapshot_peak_bytes(
                    session, parts, precise=request.precise),
                f"snapshot {fmt_clock(start)}-{fmt_clock(end)}")
            # Ownership is visible before submit(). A worker may start before
            # submit returns, and shutdown may be waiting immediately outside
            # this admission barrier; both can now transfer this same claim.
            with self._snapshot_reservation_guard:
                self._snapshot_reservations[key] = reservation
            try:
                if persist_intent:
                    self._record_snapshot_intent(session, request, key)
                job = self.snapshot_jobs.submit(
                    key,
                    (f"{session.channel} snapshot "
                     f"{fmt_clock(start)}-{fmt_clock(end)}"),
                    "snapshot",
                    lambda item: self._run_snapshot(
                        item, session, request, key),
                )
                if job is None:
                    raise RuntimeError(
                        "snapshots are not being accepted right now")
            except Exception:
                self._release_snapshot_reservation(key)
                if persist_intent:
                    self._remove_snapshot_entry(session, key)
                raise
            return job

    def _run_snapshot(self, job: Job, session: Session,
                      request: SnapshotRequest, key: str) -> None:
        try:
            job.progress = "cutting"
            self._update_snapshot_entry(
                session, snapshot_id=key, cut_status=RUNNING, cut_error="")
            result = self.snapshots.create(session, request)
            job.progress = ""
            self._after_snapshot(
                session, request, result, snapshot_id=key)
        except Exception as exc:
            changes = {
                "cut_status": ERROR,
                "cut_error": str(exc),
            }
            if request.transcribe:
                changes.update(
                    transcript_status=ERROR,
                    transcript_error=(
                        "snapshot transcript was not started because the cut "
                        f"failed: {exc}"),
                )
            self._update_snapshot_entry(session, snapshot_id=key, **changes)
            raise
        finally:
            self._release_snapshot_reservation(key)

    def _snapshot_peak_bytes(
        self, session: Session,
        parts: list[tuple[Path, float, float]],
        *, precise: bool = False,
    ) -> int:
        if precise:
            # AUD2-018: a precise cut re-encodes, so its size is not bounded by the
            # source. Reserve the same firm ceiling ffmpeg is given as an `-fs`
            # cap. Peak disk for a multi-part join is the re-encoded pieces (<= one
            # cap across the whole duration) plus the joined copy (<= one more), so
            # reserve two caps for a join and one for a single-piece cut.
            cap = precise_output_cap(self.tools, parts)
            return cap * (2 if len(parts) > 1 else 1)
        estimated = 0.0
        for source, start, end in parts:
            try:
                size = source.stat().st_size
            except OSError:
                continue
            duration = 0.0
            for chunk in session.chunks:
                candidate, _, span = self.snapshots.chunk_span(session, chunk)
                if candidate is not None and candidate.resolve(strict=False) == \
                        source.resolve(strict=False):
                    duration = span
                    break
            if duration <= 0:
                duration = max(end, end - start)
            estimated += size * min(1.0, max(0.0, end - start) / duration)
        # Multi-part cuts hold pieces and the joined partial concurrently. A
        # small floor covers MP4 metadata and unexpectedly high precise bitrate.
        multiplier = 2.25 if len(parts) > 1 else 1.25
        return max(8 * 1024 * 1024, int(estimated * multiplier))

    def _snapshot_session(self, request: SnapshotRequest) -> Session:
        session = self.store.get(request.session_id)
        if session is None:
            raise RuntimeError(f"unknown session {request.session_id}")
        return session

    def _check_snapshot_capacity(self, session: Session) -> None:
        """Bound how much cutting one user can start at once.

        Each cut is an ffmpeg run against the same drive the recorder is writing
        to. Unbounded, a row of quick-cut buttons is a way to starve the capture.
        """
        overall = int(self.config.get("snapshots.max_concurrent", 2))
        per_session = int(self.config.get("snapshots.max_per_session", 1))
        active = [job for job in self.snapshot_jobs.snapshot(200)
                  if job["kind"] == "snapshot"
                  and job["status"] in (JOB_QUEUED, JOB_RUNNING)]
        if len(active) >= overall:
            raise RuntimeError(
                f"{len(active)} snapshot(s) already in progress; wait for one to "
                "finish")
        here = [job for job in active
                if job["key"].startswith(f"snapshot:{session.session_id}:")]
        if len(here) >= per_session:
            raise RuntimeError(
                f"a snapshot of {session.channel} is already being cut; wait for "
                "it to finish")

    def _after_snapshot(self, session: Session, request: SnapshotRequest,
                        result: SnapshotResult, *, snapshot_id: str = "") -> None:
        result.transcript_status = PENDING if request.transcribe else SKIPPED
        result.transcript_error = ""
        if request.transcribe and self.config.get("transcription.enabled", True) \
                and self.config.secret("deepgram_api_key"):
            output = session.path / "snapshots" / f"{result.path.stem}_transcript"
            result.transcript_dir = output
            self._record_snapshot(
                session, result, snapshot_id=snapshot_id,
                transcript_requested=True)
            # On the media pool, not the cut pool. Transcribing a twenty-minute
            # snapshot takes minutes, and holding a cut worker for that would make
            # the next cut queue behind it -- the one thing cuts must not do.
            job = self._queue_snapshot_transcript(
                session, result.path, output)
            if job is None:
                result.transcript_status = ERROR
                result.transcript_error = (
                    "snapshot transcript could not be queued; it will be retried "
                    "on restart")
                self._update_snapshot_entry(
                    session, result.path.name,
                    transcript_status=ERROR,
                    transcript_error=result.transcript_error)
            return

        result.transcript_status = SKIPPED
        if request.transcribe:
            result.transcript_error = (
                "snapshot transcription is unavailable in the current configuration")
        self._record_snapshot(
            session, result, snapshot_id=snapshot_id,
            transcript_requested=request.transcribe)

    def _queue_snapshot_transcript(self, session: Session, source: Path,
                                   output: Path, *,
                                   transcribe: bool = True) -> Job | None:
        return self.media_jobs.submit(
            f"snapshot-transcript:{source.name}",
            (f"{session.channel} snapshot: "
             f"{'transcript' if transcribe else 'repair transcript exports'}"),
            "transcribe",
            lambda job: self._run_snapshot_transcript(
                session, source, output, transcribe=transcribe),
        )

    def _run_snapshot_transcript(self, session: Session, source: Path,
                                 output: Path, *, transcribe: bool = True) -> None:
        self._update_snapshot_entry(
            session, source.name, transcript_status=RUNNING,
            transcript_error="")
        try:
            if transcribe:
                self.transcriber.transcribe_file(source, output)
            self._ensure_snapshot_publication(source, output)
        except Exception as exc:
            self._update_snapshot_entry(
                session, source.name, transcript_status=ERROR,
                transcript_error=str(exc))
            raise
        self._update_snapshot_entry(
            session, source.name, transcript_status=DONE,
            transcript_error="")

    def _snapshot_publication_is_consistent(
            self, output: Path, words: list[Any], meta: dict[str, Any]) -> bool:
        try:
            if not publication_is_consistent(output, words, meta):
                return False
            # publication_is_consistent verifies generation membership. Also
            # parse each declared JSON artifact so a truncated Premiere file is
            # not accepted merely because it exists in the manifest.
            manifest = json.loads(
                (output / SOURCE_DIR / "exports.json").read_text(encoding="utf-8"))
            names = manifest.get("files")
            if not isinstance(names, list):
                return False
            for name in names:
                if isinstance(name, str) and name.endswith(".json"):
                    json.loads((output / name).read_text(encoding="utf-8"))
        except Exception:
            # Reconciliation can itself fail on a damaged/locked transaction.
            # Treat that as inconsistent so the repair worker owns the failure
            # transition and persists ERROR rather than aborting all recovery.
            return False
        return True

    def _ensure_snapshot_publication(self, source: Path, output: Path) -> None:
        words, meta = load_words(output / SOURCE_DIR / "words.json")
        if not meta.get("complete"):
            raise RuntimeError("snapshot transcript did not reach complete coverage")
        if self._snapshot_publication_is_consistent(output, words, meta):
            return

        language = str(meta.get("language") or "en")
        write_exports(
            output,
            words,
            language=language,
            censor=self.transcriber.censor(),
            meta={"source": source.name, "complete": True},
            words_meta=meta,
        )
        words, meta = load_words(output / SOURCE_DIR / "words.json")
        if (not meta.get("complete")
                or not self._snapshot_publication_is_consistent(
                    output, words, meta)):
            raise RuntimeError(
                "snapshot transcript exports remain incomplete after repair")

    @staticmethod
    def _read_snapshot_entries(index: Path) -> list[dict[str, Any]]:
        try:
            entries = json.loads(index.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        if not isinstance(entries, list):
            return []
        return [entry for entry in entries if isinstance(entry, dict)]

    def _record_snapshot_intent(
            self, session: Session, request: SnapshotRequest, key: str) -> None:
        start = float(request.start or 0.0)
        end = float(request.end or 0.0)
        entry = {
            "id": key,
            "job_key": key,
            "file": "",
            "path": "",
            "start": round(start, 3),
            "end": round(end, 3),
            "duration": 0.0,
            "requested_duration": round(max(0.0, end - start), 3),
            "clock": f"{fmt_clock(start)}-{fmt_clock(end)}",
            "spans": [],
            "cut_status": PENDING,
            "cut_error": "",
            "transcript_requested": bool(request.transcribe),
            "transcript_dir": "",
            "transcript_status": PENDING if request.transcribe else SKIPPED,
            "transcript_error": "",
            "precise": bool(request.precise),
            "name": request.name,
            "created_at": time.time(),
        }
        index = session.path / "snapshots" / "snapshots.json"
        with self._snapshot_index_guard(session):
            entries = self._read_snapshot_entries(index)
            entries.append(entry)
            atomic_write_json(index, entries)

    def _record_snapshot(
            self, session: Session, result: SnapshotResult, *,
            snapshot_id: str = "", transcript_requested: bool = False) -> None:
        """Append to the snapshot index under a lock, atomically.

        Two concurrent cuts previously read-modify-wrote this file and one of
        them lost its entry.
        """
        index = session.path / "snapshots" / "snapshots.json"
        with self._snapshot_index_guard(session):
            entries = self._read_snapshot_entries(index)
            payload = {
                **result.to_dict(),
                "id": snapshot_id,
                "job_key": snapshot_id,
                "cut_status": DONE,
                "cut_error": "",
                "transcript_requested": transcript_requested,
                "created_at": time.time(),
            }
            replaced = False
            if snapshot_id:
                for position, entry in enumerate(entries):
                    if entry.get("id") == snapshot_id:
                        payload["created_at"] = entry.get(
                            "created_at", payload["created_at"])
                        entries[position] = payload
                        replaced = True
                        break
            if not replaced:
                entries.append(payload)
            atomic_write_json(index, entries)

    def _remove_snapshot_entry(self, session: Session, snapshot_id: str) -> None:
        index = session.path / "snapshots" / "snapshots.json"
        with self._snapshot_index_guard(session):
            entries = self._read_snapshot_entries(index)
            retained = [entry for entry in entries
                        if entry.get("id") != snapshot_id]
            if len(retained) != len(entries):
                atomic_write_json(index, retained)

    @contextmanager
    def _snapshot_index_guard(self, session: Session) -> Iterator[None]:
        with self._snapshot_index_lock:
            with ResourceLock(
                    session.path / ".locks" / "snapshot-index.lock",
                    timeout=30.0):
                yield

    def _update_snapshot_entry(self, session: Session, filename: str = "", *,
                               snapshot_id: str = "", **changes: Any) -> bool:
        index = session.path / "snapshots" / "snapshots.json"
        with self._snapshot_index_guard(session):
            entries = self._read_snapshot_entries(index)
            if not entries:
                return False
            changed = False
            for entry in entries:
                if ((snapshot_id and entry.get("id") == snapshot_id)
                        or (filename and entry.get("file") == filename)):
                    entry.update(changes)
                    changed = True
                    break
            if changed:
                atomic_write_json(index, entries)
            return changed

    def snapshot_status(self, result_path: Path) -> dict[str, Any] | None:
        """Return one durable snapshot-index entry, keyed by its result path."""
        target = Path(result_path).resolve(strict=False)
        session = next((item for item in self.store.all()
                        if (item.path / "snapshots").resolve(strict=False)
                        == target.parent), None)
        if session is None:
            return None
        index = target.parent / "snapshots.json"
        with self._snapshot_index_guard(session):
            for entry in self._read_snapshot_entries(index):
                if entry.get("file") == target.name:
                    return dict(entry)
        return None

    def wait_for_snapshot(
            self, result_path: Path, *, require_transcript: bool,
            timeout: float | None = None) -> dict[str, Any] | None:
        """Wait for the durable cut and optional transcript package outcome."""
        path = Path(result_path)
        deadline = (None if timeout is None else
                    time.monotonic() + max(0.0, timeout))
        while True:
            entry = self.snapshot_status(path)
            if entry is None:
                return None

            cut_status = entry.get("cut_status")
            if not cut_status:
                cut_status = DONE if path.is_file() else ERROR
            if cut_status in (ERROR, SKIPPED):
                return entry
            if cut_status == DONE:
                if not path.is_file():
                    message = "snapshot media is missing after the cut completed"
                    self._update_snapshot_entry(
                        self._snapshot_session_for_result(path), path.name,
                        cut_status=ERROR, cut_error=message)
                    return {**entry, "cut_status": ERROR,
                            "cut_error": message}
                if not require_transcript:
                    return entry

                transcript_status = entry.get("transcript_status")
                if transcript_status == DONE:
                    output = path.parent / f"{path.stem}_transcript"
                    try:
                        words, meta = load_words(output / SOURCE_DIR / "words.json")
                        consistent = bool(meta.get("complete")) and \
                            self._snapshot_publication_is_consistent(
                                output, words, meta)
                    except Exception:
                        consistent = False
                    if consistent:
                        return entry
                    message = (
                        "snapshot transcript package is incomplete or inconsistent")
                    self._update_snapshot_entry(
                        self._snapshot_session_for_result(path), path.name,
                        transcript_status=ERROR, transcript_error=message)
                    return {**entry, "transcript_status": ERROR,
                            "transcript_error": message}
                if transcript_status in (ERROR, SKIPPED):
                    return entry

                job = self.media_jobs.get(
                    f"snapshot-transcript:{path.name}")
                if job is None or job.status not in (JOB_QUEUED, JOB_RUNNING):
                    message = (job.error if job is not None and job.error else
                               "snapshot transcript did not complete")
                    self._update_snapshot_entry(
                        self._snapshot_session_for_result(path), path.name,
                        transcript_status=ERROR, transcript_error=message)
                    return {**entry, "transcript_status": ERROR,
                            "transcript_error": message}

            if deadline is not None and time.monotonic() >= deadline:
                return entry
            time.sleep(0.05)

    def _snapshot_session_for_result(self, result_path: Path) -> Session:
        parent = Path(result_path).resolve(strict=False).parent
        session = next((item for item in self.store.all()
                        if (item.path / "snapshots").resolve(strict=False)
                        == parent), None)
        if session is None:
            raise RuntimeError(f"snapshot result is outside a known session: {result_path}")
        return session

    def _retry_snapshot_cut(
            self, session: Session, entry: dict[str, Any]) -> Job:
        snapshot_id = str(entry.get("id") or "")
        if not snapshot_id.startswith(f"snapshot:{session.session_id}:"):
            raise RuntimeError("pending snapshot has no valid admission id")
        request = SnapshotRequest(
            session_id=session.session_id,
            start=float(entry["start"]),
            end=float(entry["end"]),
            precise=bool(entry.get("precise")),
            transcribe=bool(entry.get("transcript_requested")),
            name=str(entry.get("name") or ""),
        )
        start, end = self.snapshots.resolve_range(session, request)
        frozen = replace(request, start=start, end=end)
        parts, _ = self.snapshots.plan(session, start, end)
        return self._admit_snapshot(
            session, frozen, parts, snapshot_id, persist_intent=False)

    def _recover_snapshot_tasks(
            self, session: Session,
            ownership: _OwnershipGroup | None = None) -> list[str]:
        index = session.path / "snapshots" / "snapshots.json"
        with self._snapshot_index_guard(session):
            entries = self._read_snapshot_entries(index)
            if not entries:
                return []

            cut_retry: list[dict[str, Any]] = []
            transcript_retry: list[tuple[Path, Path, bool]] = []
            changed = False
            actions: list[str] = []
            for entry in entries:
                cut_status = entry.get("cut_status")
                if cut_status in (PENDING, RUNNING):
                    cut_retry.append(dict(entry))
                    entry.update(cut_status=PENDING)
                    changed = True
                    continue

                filename = entry.get("file")
                if (not isinstance(filename, str)
                        or Path(filename).name != filename
                        or Path(filename).suffix.lower() != ".mp4"):
                    continue
                source = index.parent / filename
                if not source.is_file():
                    entry.update(
                        cut_status=ERROR,
                        cut_error="snapshot media is missing after the cut completed")
                    if entry.get("transcript_requested"):
                        entry.update(
                            transcript_status=ERROR,
                            transcript_error=(
                                "snapshot media is missing; cannot recover its "
                                "transcript"))
                    changed = True
                    continue
                if not cut_status:
                    entry.update(cut_status=DONE, cut_error="")
                    changed = True

                requested = bool(entry.get("transcript_requested")) or \
                    bool(entry.get("transcript_dir")) or entry.get(
                        "transcript_status") in (PENDING, RUNNING, ERROR)
                if not requested:
                    continue
                output = index.parent / f"{Path(filename).stem}_transcript"
                try:
                    words, meta = load_words(output / SOURCE_DIR / "words.json")
                except Exception:
                    words = []
                    meta = {}
                if meta.get("complete"):
                    if self._snapshot_publication_is_consistent(
                            output, words, meta):
                        entry.update(transcript_status=DONE, transcript_error="",
                                     transcript_dir=str(output))
                    else:
                        entry.update(
                            transcript_status=PENDING,
                            transcript_error="",
                            transcript_dir=str(output))
                        transcript_retry.append((source, output, False))
                    changed = True
                    continue
                if (not self.config.get("transcription.enabled", True)
                        or not self.config.secret("deepgram_api_key")):
                    entry.update(
                        transcript_status=ERROR,
                        transcript_error=(
                            "snapshot transcript is pending configuration and "
                            "will be retried on restart"),
                        transcript_dir=str(output))
                    changed = True
                    continue
                entry.update(transcript_status=PENDING, transcript_error="",
                             transcript_dir=str(output))
                transcript_retry.append((source, output, True))
                changed = True
            if changed:
                atomic_write_json(index, entries)

        for entry in cut_retry:
            snapshot_id = str(entry.get("id") or "")
            try:
                self._retry_snapshot_cut(session, entry)
            except Exception as exc:
                self._update_snapshot_entry(
                    session, snapshot_id=snapshot_id, cut_status=PENDING,
                    cut_error=f"snapshot recovery is pending: {exc}")
            else:
                actions.append(
                    f"{session.session_id}: re-queued pending snapshot "
                    f"{entry.get('clock') or snapshot_id}")

        for source, output, transcribe in transcript_retry:
            if ownership is None:
                job = self._queue_snapshot_transcript(
                    session, source, output, transcribe=transcribe)
            else:
                job = ownership.submit(
                    self.media_jobs,
                    f"snapshot-transcript:{source.name}",
                    (f"{session.channel} snapshot: "
                     f"{'transcript' if transcribe else 'repair transcript exports'}"),
                    "transcribe",
                    lambda item, source=source, output=output,
                    transcribe=transcribe: self._run_snapshot_transcript(
                        session, source, output, transcribe=transcribe),
                )
            if job is None:
                self._update_snapshot_entry(
                    session, source.name, transcript_status=ERROR,
                    transcript_error=(
                        "snapshot transcript recovery could not be queued"))
            else:
                actions.append(
                    f"{session.session_id}: re-queued snapshot "
                    f"{'transcript' if transcribe else 'transcript repair'} "
                    f"{source.name}")
        return actions

    # -- background loops --------------------------------------------------

    def _tick_loop(self) -> None:
        interval = 15.0
        while not self._stop.wait(interval):
            try:
                self._tick()
            except Exception:
                LOG.exception("ticker iteration failed")

    def _tick(self) -> None:
        for session in self.store.active():
            if session.session_id in self._externally_owned_sessions:
                continue
            chunk = session.active_chunk()
            if chunk is None:
                continue
            key = f"transcribe:{session.session_id}:{chunk.label}"
            self.jobs.submit(
                key,
                f"{session.channel} {chunk.label}: transcript",
                "transcribe",
                lambda job, s=session, c=chunk: self._advance_transcript(s, c),
            )

        for pool in self.pools:
            pool.prune()
        self._drain_deferred_deletes()
        self._sweep_proxies()

    def _sweep_proxies(self) -> None:
        """Proxies are disposable; masters are not. Delete proxies past retention."""
        days = float(self.config.get("proxies.retention_days", 1))
        if days <= 0 or time.time() - self._last_retention_sweep < 3600:
            return
        self._last_retention_sweep = time.time()

        # Validated on the way in, so it cannot turn the glob into a wildcard
        # that reaches outside the proxies folder.
        folder = safe_name_component(
            self.config.get("proxies.folder_name", "Proxies"),
            what="proxies.folder_name")
        cutoff = time.time() - days * 86400
        removed = 0
        expired: set[str] = set()
        for path in self.config.masters_root.glob(f"*/*/master/{folder}/*.mp4"):
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
                    expired.add(path.name)
                    removed += 1
            except OSError:
                pass

        # The dashboard used to claim an expired proxy was still available,
        # because deleting the file never touched the state describing it.
        if expired:
            changed_sessions: set[str] = set()
            for session in self.store.all():
                for chunk in session.chunks:
                    if chunk.proxy_name and chunk.proxy_name in expired:
                        self.store.update_chunk(session, chunk,
                                                proxy_status="expired",
                                                proxy_name="")
                        changed_sessions.add(session.session_id)
            for session in self.store.all():
                if session.session_id in changed_sessions:
                    self._refresh_session_index(session)
        if removed:
            LOG.info("proxy retention: removed %d file(s) older than %.0f day(s)",
                     removed, days)

    def _watch_loop(self) -> None:
        while not self._stop.wait(5.0):
            interval = float(self.config.get("watcher.check_seconds", 60))
            self._check_channels(self._channels_to_probe())
            self._stop.wait(max(10.0, interval))

    def _channels_to_probe(self) -> list[str]:
        """The watch list, plus anything armed.

        Armed channels are probed even with the watcher switched off, and even if
        they were never added to the watch list. Pressing Record is a direct
        instruction about one channel; a global "don't go looking for streams"
        setting is not a reason to ignore it.
        """
        watching = self.channels() if self.config.get("watcher.enabled", True) else []
        return sorted(set(watching) | set(self.armed_channels()))

    def _check_channels(self, channels: list[str]) -> None:
        """Queue regular checks on the bounded persistent probe runner."""
        if not channels or self._stop.is_set():
            return
        for channel in channels:
            self.probe_jobs.submit(
                f"watch-probe:{channel}",
                f"{channel}: checking if live",
                "probe",
                lambda job, name=channel: self._check_channel(name),
            )

    def _check_channel(self, channel: str, *, force: bool = False,
                       request_id: str = "") -> None:
        status = self._live_status.setdefault(channel, {})
        now = time.time()
        interval = float(self.config.get("watcher.check_seconds", 60))
        if not force and now - status.get("checked_at", 0.0) < interval:
            return

        probe_state, title = self._probe_live(channel)
        if probe_state not in (LIVE, OFFLINE, UNKNOWN):
            LOG.warning("live check for %s returned an invalid state %r",
                        channel, probe_state)
            probe_state, title = UNKNOWN, ""
        status.update({
            "state": probe_state,
            "live": probe_state == LIVE,
            "title": title,
            "checked_at": now,
        })

        if probe_state == UNKNOWN:
            return
        if probe_state == OFFLINE:
            # The broadcast the user stopped is over, so an explicit Stop has
            # served its purpose and auto-record applies again from here.
            if self._release_auto_suppression(channel):
                LOG.info("%s is offline; auto-record applies again", channel)
            return

        # Verification and launch share the lifecycle barrier. A cancellation,
        # removal, or shutdown that won the lock first invalidates this result;
        # once this block wins, none can interleave between the final check and
        # Recorder.start().
        with self._lifecycle:
            if self._stop.is_set() or self._state == DRAINING:
                LOG.info("%s is live but the pipeline is shutting down", channel)
                return
            recorder = self._recorders.get(channel)
            if recorder and recorder.running:
                return

            request = self._armed.get(channel)
            if request_id:
                if request is None or request.request_id != request_id:
                    return
            if request is not None:
                LOG.info("%s went live -- starting recording (request %s)",
                         channel, request.request_id)
                self._start_requested_locked(
                    channel, request.request_id, title)
                return

            # A forced probe belongs only to its request generation. It must not
            # degrade into an auto-start after that request was cancelled.
            if request_id:
                return
            # Membership is the authority at the final race barrier. The global
            # watcher switch controls whether regular probes are scheduled; it
            # does not invalidate a probe that was already explicitly invoked.
            watching = channel in self.channels()
            if not watching:
                return
            if channel in self._auto_suppressed:
                return
            if not self._channel_setting(channel, "auto_record", True):
                return

            LOG.info("%s went live -- starting recording", channel)
            try:
                session = self.start_recording(channel)
            except Exception as exc:
                LOG.error("could not start %s: %s", channel, exc)
                return
            if title:
                self.store.update(session, title=title)

    def _probe_live(self, channel: str) -> tuple[str, str]:
        """Ask streamlink whether the channel is broadcasting.

        Using streamlink rather than the Twitch API keeps the channel list truly
        arbitrary: no client id, no app registration, nothing to configure before
        adding a name.
        """
        cmd = [self.tools.streamlink, "--json", "--loglevel", "none"]
        # The proxy must reach the live-status probe too, or from a region Twitch
        # has left the watcher would never see a channel go live and auto-record
        # would never fire, even with network.proxy set for capture.
        cmd += proxy_args(self.config.get("network.proxy", "") or "")
        cmd += oauth_args(self.config.secret("twitch_oauth_token"))
        if self.config.get("recording.streamlink_no_config", False):
            cmd.append("--no-config")
        cmd.append(f"https://twitch.tv/{channel}")

        timeout = float(self.config.get("watcher.probe_timeout_seconds", 25))
        try:
            result = run(cmd, timeout=timeout)
        except Exception as exc:
            # A hung probe is not a channel that is offline; say so and try again
            # on the next pass rather than recording a false negative.
            LOG.warning("live check for %s did not answer within %.0fs (%s)",
                        channel, timeout, type(exc).__name__)
            return UNKNOWN, ""
        try:
            payload = json.loads(result.stdout or "")
        except json.JSONDecodeError:
            return UNKNOWN, ""
        if not isinstance(payload, dict):
            return UNKNOWN, ""

        error = str(payload.get("error") or "").lower()
        confirmed_offline = any(marker in error for marker in (
            "no playable streams", "no streams found", "channel is offline",
        ))
        if result.returncode != 0:
            return (OFFLINE, "") if confirmed_offline else (UNKNOWN, "")

        streams = payload.get("streams")
        if not isinstance(streams, dict):
            return (OFFLINE, "") if confirmed_offline else (UNKNOWN, "")
        if not streams:
            return OFFLINE, ""
        metadata = payload.get("metadata") or {}
        title = str(metadata.get("title") or "") if isinstance(metadata, dict) else ""
        return LIVE, title

    def _channel_setting(self, channel: str, key: str, default: Any) -> Any:
        overrides = self.config.get(f"channel_settings.{channel}") or {}
        return overrides.get(key, default)

    def set_channel_setting(self, channel: str, key: str, value: Any) -> None:
        channel = parse_channel(channel)
        if key != "auto_record":
            raise ValueError(f"unknown channel setting {key!r}")

        def update(data: dict[str, Any]) -> None:
            settings = data.setdefault("channel_settings", {})
            settings.setdefault(channel, {})[key] = value

        self.config.mutate_and_save(update)

    # -- reporting ---------------------------------------------------------

    def live_status(self) -> dict[str, dict[str, Any]]:
        return dict(self._live_status)

    def _write_session_index(self, session: Session) -> None:
        """A human-readable map of what this session produced."""
        lines = [
            f"# {session.channel} — {time.strftime('%Y-%m-%d %H:%M', time.localtime(session.started_at))}",
            "",
            f"Status: {session.status}",
            f"Chunks: {len(session.chunks)}",
        ]
        if session.quality_selected:
            lines.append(f"Quality: {session.quality_selected}"
                         + (f" (offered: {', '.join(session.quality_available)})"
                            if session.quality_available else ""))
        if session.quality_warning:
            # Deliberately loud and near the top. The whole reason this field
            # exists is that four two-hour masters were written at 720p and
            # nothing said so until they were opened in Premiere.
            lines.append("")
            lines.append(f"> **Quality warning.** {session.quality_warning}")
        # What to open, in the order you open it. This is the first file anyone
        # looks at, and without it the answer lives only in the README.
        lines += [
            "",
            "## Start here",
            "",
            "1. Import the `master/` folder into Premiere, then select the clips "
            "and **Proxy → Attach Proxies…** — the `Proxies/` names match Adobe's "
            "own convention, so the dialog finds them.",
            "2. Read `transcripts/<chunk>/rundown.md` to find what is worth "
            "keeping.",
            "3. Load the master in the **Source Monitor** (not a sequence), then "
            "`Window > Text` → Transcript → `…` → **Import Static Transcript** "
            "and choose that chunk's `premiere.json`.",
            "",
            "Each chunk folder holds only those four things you open — the "
            "rundown, `premiere.json`, `transcript.srt` and `censor-words.txt`. "
            "`source/` beside them keeps what the pipeline reads: the word "
            "stream every export is rebuilt from, the verbatim Deepgram "
            "responses, and the export manifest.",
            "",
            "| Chunk | Starts at | Duration | Size | Resolution | Master | Proxy | Transcript | Rundown |",
            "|---|---|---|---|---|---|---|---|---|",
        ]
        for chunk in sorted(session.chunks, key=lambda item: item.index):
            transcript_dir = session.path / "transcripts" / chunk.label
            lines.append(
                f"| {chunk.label} | {fmt_clock(chunk.session_offset)} | "
                f"{fmt_clock(chunk.duration)} | "
                f"{human_bytes(chunk.size_bytes) if chunk.size_bytes else '—'} | "
                f"{f'{chunk.width}x{chunk.height}' if chunk.height else '—'} | "
                f"{chunk.master_name or '—'} | "
                f"{chunk.proxy_name or chunk.proxy_status} | "
                f"{'yes' if (transcript_dir / 'premiere.json').exists() else chunk.transcript_status} | "
                f"{'yes' if (transcript_dir / 'rundown.md').exists() else chunk.summary_status} |"
            )

        if session.ad_events:
            lines += [
                "", "## Ad events noted", "",
                "_Informational only. streamlink filters ad segments out before "
                "they reach the recording, so nothing was cut from the media "
                "below and no transcript content was excluded._", "",
            ]
            for event in session.ad_events:
                approx = fmt_clock(float(event.get("approx_session_seconds", 0.0)))
                lines.append(f"- ~{approx} — {event.get('kind', 'ad')}: "
                             f"{event.get('detail', '')}")

        atomic_write_text(session.path / "index.md", "\n".join(lines) + "\n")

    def _refresh_session_index(self, session: Session) -> None:
        """Refresh the convenience index without changing artifact outcomes."""
        try:
            self._write_session_index(session)
        except Exception:
            LOG.exception("%s: could not write the session index", session.channel)

    def state_payload(self) -> dict[str, Any]:
        live = self.live_status()
        recording = set(self.recording_channels())
        with self._lifecycle:
            requests = dict(self._armed)
        armed = set(requests)
        channels = []
        # Armed channels appear even if they were never added to the watch list --
        # a pending request the user cannot see is a request they cannot cancel.
        for channel in sorted(set(self.channels()) | armed):
            status = live.get(channel, {})
            session = self.store.active_for_channel(channel)
            channels.append({
                "name": channel,
                "live": bool(status.get("live")),
                "live_state": status.get("state", UNKNOWN),
                "title": status.get("title", ""),
                "checked_at": status.get("checked_at", 0),
                "recording": channel in recording,
                "starting": bool(session and session.status == STARTING),
                "armed": channel in armed,
                "request_id": (requests[channel].request_id
                               if channel in requests else ""),
                "watched": channel in set(self.channels()),
                "session_id": session.session_id if session else "",
                "auto_record": self._channel_setting(channel, "auto_record", True),
                "externally_owned": bool(
                    session and session.session_id
                    in self._externally_owned_sessions),
            })

        sessions = []
        for session in self.store.all()[:40]:
            payload = session.to_dict()
            payload["externally_owned"] = (
                session.session_id in self._externally_owned_sessions)
            for item, chunk_payload in zip(session.chunks, payload["chunks"]):
                source, reason = self._summary_source(session, item)
                chunk_payload["summary_eligible"] = source is not None
                chunk_payload["summary_eligibility_reason"] = reason
            recorder = self._active_recorder_for(session)
            if recorder is not None:
                active = session.active_chunk()
                measured = recorder.measured_head_position()
                payload["live_chunk_seconds"] = measured
                payload["recorded_extent"] = (
                    (active.session_offset if active is not None else 0.0)
                    + measured)
            else:
                payload["live_chunk_seconds"] = 0.0
                payload["recorded_extent"] = max(
                    (chunk.session_offset + chunk.duration
                     for chunk in session.chunks), default=0.0)
            sessions.append(payload)
        summary_available, summary_reason = self._summary_capability()
        summary_provider = str(
            self.config.get("summary.provider") or "claude-cli").lower()
        summary_secret = PROVIDER_SECRETS.get(summary_provider, "")
        return {
            "now": time.time(),
            "lifecycle": {
                "state": self.lifecycle_state,
                "error": self._lifecycle_error,
            },
            "channels": channels,
            "sessions": sessions,
            "manifest_diagnostics": self.store.diagnostics(),
            "jobs": self.job_snapshot(),
            "disk": {
                "free_bytes": free_bytes(self.config.masters_root),
                "floor_bytes": int(float(
                    self.config.get("recording.free_space_floor_gb", 50)) * 1024 ** 3),
                "masters_root": str(self.config.masters_root),
            },
            "capabilities": {
                "deepgram": bool(self.config.secret("deepgram_api_key")),
                "twitch_token": bool(self.config.secret("twitch_oauth_token")),
                "claude_cli": bool(self.tools.claude),
                "anthropic_api": bool(
                    self.config.secret("anthropic_api_key")),
                # Whichever key the *selected* engine needs, so the dashboard
                # badge does not have to know which providers take keys.
                "summary_key": bool(summary_secret
                                    and self.config.secret(summary_secret)),
                "summary_key_name": summary_secret,
                "summary_provider": summary_provider,
                "summary_available": summary_available,
                "summary_unavailable_reason": summary_reason,
            },
        }
