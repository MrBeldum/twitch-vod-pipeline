"""Cross-process channel locking.

Two copies of the dashboard, or a dashboard plus a `vodpipe record`, would
otherwise both start recording the same channel into the same directory: same
segment list, same .ts names, same session.json. The in-process lock cannot see
that, so exclusion is anchored to a file.

**Ownership is the kernel's, not ours.** The lock is a byte-range lock held on an
open descriptor for the whole life of the recording -- `msvcrt.locking` on
Windows, `fcntl.flock` elsewhere. Nothing infers ownership from the file's
contents, and nothing deletes another process's lock file.

The previous design created the file with `O_EXCL` and, on collision, read a pid
out of it and probed with `os.kill(pid, 0)` to decide whether the holder had
died. That has three problems a kernel lock simply does not have:

* it needs a liveness oracle, and pids are reused, so a crashed recorder whose
  pid has been recycled looks alive and locks the channel out forever, while a
  live one whose pid probe misbehaves gets its lock deleted underneath it;
* the create/read/unlink/recreate sequence is several separate operations, so a
  contender can delete a lock that another contender has just created but not
  yet written, and two contenders can both "clear" the same stale path;
* `release()` unlinked by path, so it could remove a *successor's* lock.

With a held descriptor, an abandoned lock is released by the OS the instant the
process exits -- crash, kill, or clean shutdown alike -- so staleness stops being
a thing that has to be detected. The file is left on disk after release; it is a
few bytes per channel and unlinking it is the one operation that reintroduces the
races above.

The pid and timestamp written into the file are diagnostics for a human reading
`.locks/` and are never read back to make a decision.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from pathlib import Path

from .util import LOG

if sys.platform == "win32":
    import msvcrt
else:  # pragma: no cover - exercised on POSIX only
    import fcntl


class ChannelBusy(RuntimeError):
    """Another process holds this channel."""


class ResourceBusy(RuntimeError):
    """Another process holds a mutation or media resource lock."""


def _try_lock(fd: int, *, shared: bool = False) -> bool:
    """Take a non-blocking kernel lock. False if someone else conflicts."""
    try:
        if sys.platform == "win32":
            os.lseek(fd, 0, os.SEEK_SET)
            mode = msvcrt.LK_NBRLCK if shared else msvcrt.LK_NBLCK
            msvcrt.locking(fd, mode, 1)
        else:  # pragma: no cover - exercised on POSIX only
            mode = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
            fcntl.flock(fd, mode | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def _unlock(fd: int) -> None:
    try:
        if sys.platform == "win32":
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:  # pragma: no cover - exercised on POSIX only
            fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass


def _process_alive(pid: int) -> bool:
    """Whether a pid is running. Diagnostics only -- never gates the lock.

    Kept because the recorder reports it when explaining who holds a channel.
    Nothing in the locking path consults it any more.
    """
    if pid <= 0:
        return False
    if sys.platform == "win32":
        # `os.kill(pid, 0)` is not a portable liveness probe on Windows; some
        # Python builds translate the zero signal into an invalid-parameter
        # error even for their own live pid. OpenProcess asks the kernel without
        # signalling the target and works for the current process as well as
        # ordinary peer processes.
        import ctypes
        query_limited_information = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(
            query_limited_information, False, int(pid))
        if not handle:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Someone else's process, but it exists.
        return True
    except OSError:
        return False
    return True


class ChannelLock:
    """A kernel-backed lock for one channel, held for a recording's lifetime."""

    def __init__(self, root: Path, channel: str) -> None:
        self.channel = channel
        self.directory = root / ".locks"
        self.path = self.directory / f"{channel}.lock"
        self._fd: int | None = None

    def acquire(self) -> "ChannelLock":
        self.directory.mkdir(parents=True, exist_ok=True)
        # No O_EXCL: the file is a handle to lock, not a token to win by creating.
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o644)
        if not _try_lock(fd):
            os.close(fd)
            raise ChannelBusy(
                f"{self.channel} is already being recorded by another process "
                f"(see {self.path})")
        self._fd = fd
        self._write_diagnostics()
        return self

    def _write_diagnostics(self) -> None:
        """Record who holds this, for a human. Never read back as authority."""
        if self._fd is None:
            return
        payload = json.dumps({
            "pid": os.getpid(),
            "channel": self.channel,
            "acquired_at": time.time(),
        }).encode("utf-8")
        try:
            # We hold the lock, so writing over the locked byte is our right.
            os.lseek(self._fd, 0, os.SEEK_SET)
            os.write(self._fd, payload)
            os.ftruncate(self._fd, len(payload))
        except OSError as exc:
            # A lock that cannot describe itself is still a valid lock.
            LOG.debug("could not annotate %s: %s", self.path, exc)
        finally:
            try:
                os.lseek(self._fd, 0, os.SEEK_SET)
            except OSError:
                pass

    def holder(self) -> int:
        """Pid recorded in the lock file, or 0. Diagnostics only."""
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            return int(payload.get("pid", 0))
        except (OSError, ValueError, json.JSONDecodeError):
            return 0

    def release(self) -> None:
        if self._fd is None:
            return
        _unlock(self._fd)
        try:
            os.close(self._fd)
        except OSError:
            pass
        self._fd = None
        # The file is deliberately left in place. Unlinking it is what allowed a
        # released lock to take a successor's lock away with it.

    def __enter__(self) -> "ChannelLock":
        return self.acquire()

    def __exit__(self, *_: object) -> None:
        self.release()


class ResourceLock:
    """A shared or exclusive kernel lock anchored to one explicit path.

    Unlike ``ChannelLock`` this carries no diagnostics because shared holders
    must never write over one another. Lock files are permanent anchors; only
    their held descriptors confer ownership.
    """

    def __init__(self, path: Path, *, shared: bool = False,
                 timeout: float = 0.0) -> None:
        self.path = Path(path)
        self.shared = shared
        self.timeout = max(0.0, float(timeout))
        self._fd: int | None = None

    def acquire(self) -> "ResourceLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o644)
        # Read locks on Windows need an actual byte to cover. Concurrent writers
        # all write the same inert byte before any one of them can own the lock.
        try:
            if os.fstat(fd).st_size == 0:
                os.write(fd, b"\0")
                os.lseek(fd, 0, os.SEEK_SET)
        except OSError:
            os.close(fd)
            raise

        deadline = time.monotonic() + self.timeout
        while not _try_lock(fd, shared=self.shared):
            if time.monotonic() >= deadline:
                os.close(fd)
                raise ResourceBusy(f"resource is owned by another process: {self.path}")
            time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
        self._fd = fd
        return self

    def release(self) -> None:
        if self._fd is None:
            return
        _unlock(self._fd)
        try:
            os.close(self._fd)
        except OSError:
            pass
        self._fd = None

    def __enter__(self) -> "ResourceLock":
        return self.acquire()

    def __exit__(self, *_: object) -> None:
        self.release()


def session_lock_path(session_path: Path) -> Path:
    return Path(session_path) / ".locks" / "session-recovery.lock"


def chunk_lock_path(session_path: Path, label: str) -> Path:
    return Path(session_path) / ".locks" / f"{label}-mutation.lock"


def media_lock_path(root: Path, media: Path) -> Path:
    """Stable cross-process lock path for a media pathname, existing or not."""
    absolute = os.path.normcase(str(Path(media).resolve(strict=False)))
    digest = hashlib.sha256(absolute.encode("utf-8", "surrogatepass")).hexdigest()
    return Path(root) / ".locks" / "media" / f"{digest}.lock"
