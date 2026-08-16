"""A small named-job worker pool with an explicit shutdown contract.

Proxy transcodes, transcription slices and summaries all run off the recorder's
thread -- the recorder must never block on them. Jobs carry a stable key so the
dashboard can show progress and so we never queue the same work twice.

Shutdown is the part that matters. Finalisation work (tail transcript, remux,
proxy, rundown) is queued at the moment a chunk closes, which is exactly when a
user is most likely to be stopping the application. An earlier version set a stop
flag that workers checked *before* taking from the queue, so they could exit with
real work still pending: a recorded chunk would sit at `remuxing` forever. The
runner therefore has three explicit states and drains by default.
"""

from __future__ import annotations

import queue
import threading
import time
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable

from .util import LOG

# Job states.
QUEUED = "queued"
RUNNING = "running"
DONE = "done"
FAILED = "failed"
CANCELLED = "cancelled"

# Runner states.
ACCEPTING = "accepting"
DRAINING = "draining"
STOPPED = "stopped"


@dataclass
class Job:
    key: str
    label: str
    kind: str
    status: str = QUEUED
    created_at: float = field(default_factory=time.time)
    started_at: float = 0.0
    finished_at: float = 0.0
    progress: str = ""
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "kind": self.kind,
            "status": self.status,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "progress": self.progress,
            "error": self.error,
        }


class JobRunner:
    """Fixed pool of worker threads consuming a FIFO queue.

    Concurrency is deliberately low: proxy transcodes are the heavy consumer and
    the machine is also recording live video at the same time. Starving the
    recorder of CPU would drop frames, which is the one unrecoverable failure here.
    """

    def __init__(self, workers: int = 3) -> None:
        self._queue: queue.Queue = queue.Queue()
        self._jobs: dict[str, Job] = {}
        self._lock = threading.RLock()
        self._threads: list[threading.Thread] = []
        self._state = ACCEPTING
        self._idle = threading.Condition(self._lock)
        self._sentinels_sent = False
        for index in range(max(1, workers)):
            thread = threading.Thread(target=self._worker, name=f"job-{index}",
                                      daemon=True)
            thread.start()
            self._threads.append(thread)

    # -- state -------------------------------------------------------------

    @property
    def state(self) -> str:
        with self._lock:
            self._refresh_state_locked()
            return self._state

    @property
    def accepting(self) -> bool:
        return self.state == ACCEPTING

    # -- submission --------------------------------------------------------

    def submit(self, key: str, label: str, kind: str,
               work: Callable[[Job], None]) -> Job | None:
        """Queue work under `key`.

        Returns None if that key is already pending, or if the runner is no longer
        accepting -- silently queueing into a stopping pool is how work gets lost.
        """
        with self._lock:
            if self._state != ACCEPTING:
                LOG.warning("refusing job %s: runner is %s", key, self._state)
                return None
            existing = self._jobs.get(key)
            if existing and existing.status in (QUEUED, RUNNING):
                return None
            job = Job(key=key, label=label, kind=kind)
            self._jobs[key] = job
            # Registration and enqueueing are one acceptance transaction. If
            # stop() could enter between them, its sentinel could overtake this
            # accepted job and leave it queued forever behind dead workers.
            self._queue.put((job, work))
            return job

    def get(self, key: str) -> Job | None:
        with self._lock:
            return self._jobs.get(key)

    def snapshot(self, limit: int = 40) -> list[dict[str, Any]]:
        with self._lock:
            jobs = sorted(self._jobs.values(),
                          key=lambda job: job.created_at, reverse=True)
        return [job.to_dict() for job in jobs[:limit]]

    def active_count(self) -> int:
        with self._lock:
            return sum(1 for job in self._jobs.values()
                       if job.status in (QUEUED, RUNNING))

    # -- shutdown ----------------------------------------------------------

    def drain(self, timeout: float = 300.0) -> bool:
        """Stop accepting, then wait for queued and running work to finish.

        Returns True if everything finished within the deadline.
        """
        with self._idle:
            self._refresh_state_locked()
            if self._state == STOPPED:
                return self.active_count() == 0
            self._state = DRAINING

        deadline = time.monotonic() + max(0.0, timeout)
        with self._idle:
            while self._pending():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    LOG.warning("drain deadline reached with %d job(s) outstanding",
                                self.active_count())
                    return False
                self._idle.wait(min(1.0, remaining))
        return True

    def _pending(self) -> bool:
        # A worker can dequeue an item just before it acquires our lock to mark
        # it running. Job state closes that queue-empty/running-zero gap.
        return any(job.status in (QUEUED, RUNNING)
                   for job in self._jobs.values())

    def stop(self, timeout: float = 300.0, *, drain: bool = True) -> None:
        """Shut the pool down. By default finishes queued work first.

        Anything still queued when the deadline passes is marked `cancelled`
        rather than left looking like it is about to run.
        """
        deadline = time.monotonic() + max(0.0, timeout)
        drained = self.drain(timeout) if drain else False

        with self._idle:
            self._refresh_state_locked()
            if self._state == STOPPED:
                return
            self._state = DRAINING

            # Once cancellation begins no real item may sit in front of a
            # sentinel. A worker that already dequeued one still sees its
            # CANCELLED status and skips it.
            if not self._sentinels_sent:
                if not drain or not drained:
                    self._cancel_queued_locked()
                for _ in self._threads:
                    self._queue.put(None)
                self._sentinels_sent = True

        current = threading.current_thread()
        for thread in self._threads:
            if thread is current:
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            thread.join(timeout=remaining)

        with self._idle:
            self._refresh_state_locked()
            if self._state != STOPPED:
                alive = sum(thread.is_alive() for thread in self._threads)
                LOG.warning("job pool shutdown timed out with %d worker(s) alive",
                            alive)

    def _refresh_state_locked(self) -> None:
        """Publish STOPPED only after every worker has actually exited."""
        if (self._state != ACCEPTING and self._sentinels_sent
                and not any(thread.is_alive() for thread in self._threads)):
            self._state = STOPPED

    def _cancel_queued_locked(self) -> None:
        cancelled = 0
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                break
            if item is None:
                self._queue.task_done()
                continue
            job, _ = item
            self._queue.task_done()
            if job.status == QUEUED:
                job.status = CANCELLED
                job.finished_at = time.time()
                job.error = "cancelled: the application shut down before this ran"
                cancelled += 1
        # A worker may have dequeued a job but still be waiting for this lock.
        # Marking it here makes the worker skip rather than start it after a
        # forced shutdown has already cancelled its peers.
        for job in self._jobs.values():
            if job.status == QUEUED:
                job.status = CANCELLED
                job.finished_at = time.time()
                job.error = "cancelled: the application shut down before this ran"
                cancelled += 1
        self._idle.notify_all()
        if cancelled:
            LOG.warning("%d job(s) cancelled at shutdown; they will be picked up "
                        "by startup recovery", cancelled)

    # -- workers -----------------------------------------------------------

    def _worker(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                self._queue.task_done()
                return

            job, work = item
            with self._idle:
                if job.status != QUEUED:
                    self._queue.task_done()
                    self._idle.notify_all()
                    continue
                job.status = RUNNING
                job.started_at = time.time()
            try:
                work(job)
                with self._lock:
                    job.status = DONE
            except Exception as exc:  # a failed job must not take the pool down
                with self._lock:
                    job.status = FAILED
                    job.error = str(exc)
                LOG.error("job %s failed: %s", job.key, exc)
                LOG.debug("%s", traceback.format_exc())
            finally:
                self._queue.task_done()
                with self._idle:
                    job.finished_at = time.time()
                    self._idle.notify_all()

    def prune(self, keep_seconds: float = 3600) -> None:
        cutoff = time.time() - keep_seconds
        with self._lock:
            stale = [
                key for key, job in self._jobs.items()
                if job.status in (DONE, FAILED, CANCELLED)
                and job.finished_at < cutoff
            ]
            for key in stale:
                del self._jobs[key]
