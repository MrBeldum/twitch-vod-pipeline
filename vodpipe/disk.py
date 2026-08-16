"""Cross-thread and cross-process reservations above the hard disk reserve."""

from __future__ import annotations

import json
import os
import secrets
import threading
import time
from pathlib import Path
from typing import Callable

from .locks import ResourceBusy, ResourceLock
from .util import free_bytes, human_bytes


class DiskReservation:
    def __init__(self, manager: "DiskBudget", path: Path,
                 lock_path: Path, lock: ResourceLock) -> None:
        self.manager = manager
        self.path = path
        self.lock_path = lock_path
        self.lock = lock
        self._guard = threading.Lock()
        self._released = False

    def release(self) -> None:
        with self._guard:
            if self._released:
                return
            self._released = True
            self.lock.release()
            try:
                self.path.unlink(missing_ok=True)
            except OSError:
                pass
            try:
                self.lock_path.unlink(missing_ok=True)
            except OSError:
                pass

    def __enter__(self) -> "DiskReservation":
        return self

    def __exit__(self, *_: object) -> None:
        self.release()


class DiskBudget:
    """Atomically accounts for temporary output from every pipeline process."""

    def __init__(self, root: Path, reserve_bytes: Callable[[], int]) -> None:
        self.root = Path(root)
        self.reserve_bytes = reserve_bytes
        self.directory = self.root / ".locks" / "disk-reservations"
        self.admission = self.root / ".locks" / "disk-budget.lock"

    def reserve(self, needed_bytes: int, what: str) -> DiskReservation:
        needed = max(0, int(needed_bytes))
        with ResourceLock(self.admission, timeout=30.0):
            self.directory.mkdir(parents=True, exist_ok=True)
            committed = self._active_bytes()
            available = free_bytes(self.root)
            reserve = max(0, int(self.reserve_bytes()))
            if available - committed - needed < reserve:
                raise RuntimeError(
                    f"not enough disk for {what}: {human_bytes(available)} free, "
                    f"{human_bytes(committed)} already reserved, needs about "
                    f"{human_bytes(needed)} above the {human_bytes(reserve)} reserve")

            token = f"{os.getpid()}-{time.time_ns()}-{secrets.token_hex(3)}"
            path = self.directory / f"{token}.reservation"
            lock_path = self.directory / f"{token}.lock"
            # The payload cannot live in the byte-range-locked file on Windows:
            # reading byte zero of a peer's live lock raises a sharing violation.
            # Keep the kernel anchor and readable accounting metadata separate.
            lock = ResourceLock(lock_path).acquire()
            try:
                path.write_text(json.dumps({
                    "pid": os.getpid(),
                    "bytes": needed,
                    "what": str(what)[:300],
                    "created_at": time.time(),
                }), encoding="utf-8")
            except Exception:
                lock.release()
                path.unlink(missing_ok=True)
                lock_path.unlink(missing_ok=True)
                raise
            return DiskReservation(self, path, lock_path, lock)

    def _active_bytes(self) -> int:
        total = 0
        for path in self.directory.glob("*.reservation"):
            lock_path = path.with_suffix(".lock")
            probe = ResourceLock(lock_path)
            try:
                probe.acquire()
            except ResourceBusy:
                try:
                    payload = json.loads(path.read_text(encoding="utf-8"))
                    total += max(0, int(payload.get("bytes", 0)))
                except (OSError, ValueError, TypeError, json.JSONDecodeError):
                    # An active but unreadable claim is conservatively the whole
                    # currently free drive: no new discretionary output enters.
                    total += free_bytes(self.root)
                continue
            # The creator died or released without cleanup. The kernel says this
            # claim is stale, regardless of any recycled pid in JSON. Unlock
            # before unlinking because Windows refuses deletion of an open file.
            probe.release()
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
            try:
                lock_path.unlink(missing_ok=True)
            except OSError:
                pass
        # A crash between taking a unique lock and writing its metadata can
        # leave an unreferenced anchor. It accounts for no bytes and is safe to
        # remove once no matching reservation exists.
        for lock_path in self.directory.glob("*.lock"):
            if lock_path.with_suffix(".reservation").exists():
                continue
            probe = ResourceLock(lock_path)
            try:
                probe.acquire()
            except ResourceBusy:
                continue
            probe.release()
            try:
                lock_path.unlink(missing_ok=True)
            except OSError:
                pass
        return total
