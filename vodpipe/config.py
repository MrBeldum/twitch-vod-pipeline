"""Configuration: layered defaults, on-disk overrides, and secret handling.

Secrets live in the same config.json, which is gitignored. The dashboard can write
them (the user asked for paste-into-the-UI to work), so this module owns the merge
and never lets a partial POST wipe unrelated keys.
"""

from __future__ import annotations

import copy
import errno
import json
import os
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator, TypeVar

from .schema import ConfigError, validate
from .util import atomic_write_json

APP_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = APP_ROOT / "config.json"

# Keys whose values are masked whenever config is handed to the browser.
SECRET_KEYS = {"deepgram_api_key", "twitch_oauth_token"}

# Sentinels the dashboard round-trips for secret fields.
MASK = "__unchanged__"      # keep whatever is stored
CLEAR = "__clear__"         # deliberately erase the stored value


def _default_masters_root() -> str:
    desktop = Path(os.path.expanduser("~")) / "Desktop"
    return str(desktop / "twitch-vods")


DEFAULTS: dict[str, Any] = {
    "paths": {
        # Masters land on the Desktop and stay until manually cleared.
        "masters_root": _default_masters_root(),
        # Scratch space for audio slices. Small, continuously recycled.
        "work_root": str(APP_ROOT / ".work"),
        "censor_master_list": str(
            Path(os.path.expanduser("~")) / "Desktop" / "censored_words_master.txt"
        ),
    },
    "recording": {
        "chunk_seconds": 7200,
        # streamlink accepts a fallback chain, e.g. "1080p60,best". "best" alone
        # always takes the top of whatever ladder Twitch offers -- which is not
        # always 1080p; see quality.py and README, "Why a recording can be 720p".
        "quality": "best",
        # Vertical resolution the operator expects. A capture below this is
        # called out loudly rather than discovered hours later. 0 disables.
        "min_height": 1080,
        # What to do about it: "warn" records anyway and says so, "refuse" stops.
        # Warn is the default because a 720p recording of a broadcast that only
        # happens once beats no recording at all.
        "on_low_quality": "warn",
        # Refuse to open a new chunk below this. One drive, no second disk.
        "free_space_floor_gb": 50,
        # Absolute reserve. Crossing it aborts the session mid-chunk, because a
        # disk that fills while ffmpeg is writing costs the whole chunk.
        "hard_reserve_gb": 10,
        "twitch_low_latency": False,
        # How long ffmpeg is given to finalise the chunk it is on after streamlink
        # closes the pipe. It is writing out the last segment and its csv row here,
        # so cutting this short costs the tail of the recording.
        "ffmpeg_grace_seconds": 120,
        # Abandon an attempt that has not received a single byte of video in this
        # long. streamlink retries forever, which is right once a stream exists
        # and wrong at the very start: a channel that was not live left a session
        # sitting at `recording` with an empty chunk. 0 disables the watchdog.
        "startup_timeout_seconds": 120,
        # Keep the .ts working copy after a successful MP4 remux. Off by default:
        # it doubles on-disk footprint for no editorial benefit.
        "keep_ts_after_remux": False,
        # Pass --no-config to streamlink, so an ambient user config cannot change
        # how recordings are made. Off by default because that config is also a
        # legitimate place for the user's own token and plugin settings.
        "streamlink_no_config": False,
        # Read the finished master end to end before publishing it, and before
        # the `.ts` it was built from is deleted. Costs one pass over the file
        # (3-14s for a two-hour chunk here, against a 30-40s remux) and is the
        # only check that can see an index which disagrees with the data behind
        # it -- the failure that cost two masters on 2026-08-18. Turn it off only
        # if the drive makes it painful, and keep `keep_ts_after_remux` on if you
        # do.
        "verify_master": True,
        # How many times to build the master before giving up on a chunk. A
        # remux is deterministic work over bytes already on disk, so a failure is
        # either permanent -- and costs a bounded couple of minutes to confirm --
        # or a one-off, and a one-off used to cost the master permanently.
        "remux_attempts": 3,
    },
    "proxies": {
        "enabled": True,
        "height": 540,
        "encoder": "auto",          # auto | h264_amf | libx264
        "quality": 24,              # CRF for libx264; mapped to QP for AMF
        "audio_bitrate": "128k",
        "retention_days": 1,
        # Adobe's own convention. Matching it means Attach Proxies finds them.
        "suffix": "_Proxy",
        "folder_name": "Proxies",
    },
    "transcription": {
        "enabled": True,
        "provider": "deepgram",
        "model": "nova-3",
        "language": "en",
        # Which logical source track to transcribe: automatic default/first,
        # zero-based audio ordinal, or a stream language tag.
        "audio_stream": "auto",
        # Transcribe "uh"/"um" as ordinary words rather than dropping them. This
        # is a transcript *fidelity* setting, not the removed automatic filler
        # tagging: a verbatim transcript matches the audio, so a cut made from
        # the text lands where the editor expects and a filler can be selected
        # and deleted individually. Turn it off for cleaner reading copy.
        "filler_words": True,
        # Keep each provider response verbatim under
        # `transcripts/<chunk>/deepgram/`. `words.json` is the *normalised*
        # stream -- sorted, de-overlapped, same-start collisions resolved --
        # which is what every export derives from and is not quite what
        # Deepgram said. About 15 KB per slice, so ~1.5 MB for a 2-hour
        # chunk. Archiving can never fail a transcription that succeeded.
        "keep_raw_responses": True,
        # Length of each rolling slice sent to Deepgram.
        "slice_seconds": 300,
        # Don't bother sending a stub while the chunk is still growing; a partial
        # slice would only be re-transcribed a minute later anyway.
        "min_slice_seconds": 45,
        # Slices overlap so a word straddling the seam is captured whole by at
        # least one of them; the seam is then resolved at the widest pause.
        "overlap_seconds": 3.0,
        # Stay this far behind the write head so we never read an incomplete
        # packet from the file ffmpeg is still appending to.
        "live_margin_seconds": 20,
        "max_retries": 4,
        "request_timeout_seconds": 600,
        # Repair words spoken across a chunk boundary by transcribing a short
        # passage built from both files. Chunks are separate files transcribed
        # independently, so without this a word on the join is clipped in both.
        "stitch_chunk_boundaries": True,
        "seam_seconds": 6.0,
    },
    "summary": {
        "enabled": True,
        # claude-cli | grok-cli | none. See models.PROVIDER_NAMES.
        # Both engines are local subscription CLIs (`claude -p` / `grok -p`).
        # The paid-API engines were removed 2026-08-19; the reason is in
        # models.py and it is worth reading before adding another.
        "provider": "claude-cli",
        # Passed through as `--model`. Blank means the engine's default:
        # claude -p picks from the subscription; grok -p uses its CLI default
        # (Grok 4.6 as of CLI 1.0.5). `grok-build` is a retired value.
        "model": "",
        "timeout_seconds": 900,
        # Attempts per report, bounded by timeout_seconds overall. A report is
        # background work whose engine can rate-limit -- both CLIs share the
        # user's subscription quota -- so a single transient refusal must not be
        # the end of it.
        "max_retries": 3,
        # Turn budget for an agent-shaped engine. `grok -p` is handed the
        # transcript as a file and writes the report as a file, which took 8
        # turns on the reference chunk; the ceiling is generous because one
        # turn too few discards the whole call. `claude -p` ignores this.
        "max_turns": 40,
        # Below this many words there is nothing to write a report about.
        "min_words": 25,
        "max_tokens": 8000,
    },
    "chat": {
        # Download Twitch chat for live broadcasts (IRC) and VODs (GraphQL,
        # the same comments API TwitchDownloader uses). Feeds the report's
        # moment analysis. A chat failure never fails a recording.
        "enabled": True,
        "vod_threads": 4,
        "timeout_seconds": 300,
    },
    "ads": {
        # Ad events are recorded as operational metadata ONLY. They are never
        # translated into media ranges and never remove anything from a
        # transcript or a master.
        #
        # Why: streamlink 8.4's Twitch plugin filters ad segments out before the
        # stream reaches us -- TwitchHLSStreamWriter.should_filter_segment()
        # returns segment.ad -- so ad content is not in our recording to begin
        # with, and there is no interval in our file that corresponds to it. An
        # earlier version of this pipeline mapped these log lines onto recording
        # timestamps, which deleted legitimate content. See README.
        "log_events": True,
        # Matched against streamlink's log to note when Twitch served ads.
        # "Will skip ad segments" is deliberately absent: the reader logs it
        # unconditionally at startup, so it says nothing about a real break.
        "event_patterns": [
            "waiting for pre-roll ads",
            "detected advertisement break",
        ],
    },
    "snapshots": {
        # Each cut is an ffmpeg run against the drive the recorder is writing to,
        # so the number in flight is bounded rather than left to how fast the user
        # can click.
        "max_concurrent": 2,
        "max_per_session": 1,
    },
    "dashboard": {
        "host": "127.0.0.1",
        "port": 8420,
        "open_browser": True,
        "poll_seconds": 2,
    },
    "network": {
        # An HTTP/SOCKS proxy for every streamlink request -- live capture, VOD
        # download, and the live-status probe. Empty means direct. This is the
        # light-weight way to reach Twitch from a region it withholds the source
        # rendition in (South Korea, which Twitch left in February 2024) without a
        # system-wide VPN. Accepts http(s):// and socks4/4a/5/5h:// URLs, e.g.
        # "socks5://127.0.0.1:1080" or "http://user:pass@host:3128".
        "proxy": "",
    },
    "channels": [],
    "watcher": {
        "enabled": True,
        "check_seconds": 60,
        # A live check that has not answered by now is not information about the
        # channel; it is a stuck process holding up the rest of the list.
        "probe_timeout_seconds": 25,
    },
    "secrets": {
        "deepgram_api_key": "",
        "twitch_oauth_token": "",
    },
    "tools": {
        "ffmpeg": "",
        "ffprobe": "",
        "streamlink": "",
        "claude": "",
        "grok": "",
    },
}


def deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


_T = TypeVar("_T")
_PATH_LOCKS_GUARD = threading.Lock()
_PATH_LOCKS: dict[str, threading.RLock] = {}


def _path_thread_lock(path: Path) -> threading.RLock:
    key = os.path.normcase(str(path.resolve()))
    with _PATH_LOCKS_GUARD:
        return _PATH_LOCKS.setdefault(key, threading.RLock())


def _acquire_kernel_lock(fd: int) -> None:
    if sys.platform == "win32":
        import msvcrt

        retryable = {errno.EACCES, errno.EAGAIN, errno.EDEADLK}
        while True:
            try:
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                return
            except OSError as exc:
                if exc.errno not in retryable:
                    raise
                time.sleep(0.05)
    else:  # pragma: no cover - exercised by the POSIX test run
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_EX)


def _release_kernel_lock(fd: int) -> None:
    try:
        if sys.platform == "win32":
            import msvcrt
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:  # pragma: no cover - exercised by the POSIX test run
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass


@contextmanager
def _config_write_lock(path: Path) -> Iterator[None]:
    """Serialize one config transaction across threads, objects, and processes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    thread_lock = _path_thread_lock(path)
    lock_path = path.with_name(path.name + ".lock")
    with thread_lock:
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            # msvcrt locks a byte range. Materialising that byte also keeps the
            # behavior consistent on filesystems that dislike locking past EOF.
            if os.fstat(fd).st_size == 0:
                os.write(fd, b"\0")
            _acquire_kernel_lock(fd)
            try:
                yield
            finally:
                _release_kernel_lock(fd)
        finally:
            os.close(fd)
    # Deliberately do not unlink: replacing a lock path lets two contenders lock
    # different inodes and both enter the supposedly exclusive transaction.


def _read_validated(path: Path) -> dict[str, Any]:
    if not path.exists():
        return validate(copy.deepcopy(DEFAULTS))

    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"could not read {path}: {exc}")
    try:
        stored = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigError(
            f"{path} is not valid JSON ({exc}). It has been left untouched; "
            "fix or delete it and start again.")
    if not isinstance(stored, dict):
        raise ConfigError(f"{path} must contain a JSON object")
    return validate(deep_merge(DEFAULTS, stored))


def _changed_overlay(base: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    """Return values changed locally without copying unrelated stale settings."""
    changed: dict[str, Any] = {}
    for key, value in current.items():
        original = base.get(key, object())
        if isinstance(value, dict) and isinstance(original, dict):
            nested = _changed_overlay(original, value)
            if nested:
                changed[key] = nested
        elif original != value:
            changed[key] = copy.deepcopy(value)
    return changed


class Config:
    def __init__(self, data: dict[str, Any], path: Path = CONFIG_PATH, *,
                 _origin: dict[str, Any] | None = None) -> None:
        self.data = data
        self.path = path
        # Guards read-modify-write sequences against concurrent dashboard saves.
        self._lock = threading.RLock()
        # Direct construction represents a not-yet-persisted config. load() passes
        # its exact origin so later saves can isolate only this object's changes.
        self._origin = copy.deepcopy(DEFAULTS if _origin is None else _origin)
        # These two roots define one running pipeline's identity. New values remain
        # visible in data/redacted output and on disk, but take effect after restart.
        self._masters_root = self._resolve_root(self.get("paths.masters_root"))
        self._work_root = self._resolve_root(self.get("paths.work_root"))

    @classmethod
    def load(cls, path: Path = CONFIG_PATH) -> "Config":
        """Load config, failing loudly on a corrupt file.

        Silently falling back to defaults would start the application with the
        wrong paths and then overwrite the user's real settings on the next save.
        """
        # AUD2-054: keep the *cleaned* result, not the raw merge. validate()
        # normalises -- channel URLs to bare logins, `EN_us` to `en-us` -- and
        # discarding its return value meant a value loaded from disk behaved
        # differently from the identical value saved through the dashboard, which
        # goes through apply(). A URL left in `channels` then became a directory
        # name and a config key.
        cleaned = _read_validated(path)
        return cls(cleaned, path, _origin=cleaned)

    def save(self) -> None:
        with self._lock:
            local = _changed_overlay(self._origin, self.data)
            with _config_write_lock(self.path):
                cleaned = validate(deep_merge(_read_validated(self.path), local))
                atomic_write_json(self.path, cleaned, mode=0o600)
            self.data = cleaned
            self._origin = copy.deepcopy(cleaned)

    # -- access ------------------------------------------------------------

    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self.data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def set(self, dotted: str, value: Any) -> None:
        with self._lock:
            parts = dotted.split(".")
            node = self.data
            for part in parts[:-1]:
                node = node.setdefault(part, {})
            node[parts[-1]] = value

    def apply(self, overlay: dict[str, Any]) -> None:
        """Validate and merge a partial update. Transactional.

        The prospective config is built and validated in full before anything is
        assigned, so a rejected update leaves memory and disk exactly as they were.
        Previously an invalid value was merged first and only failed later, which
        could leave the application unable to start.

        AUD2-021: the merge now happens *under* the lock. Two dashboard threads
        saving different sections could each read the same `self.data`, build a
        prospective config missing the other's change, and the second assignment
        silently discarded the first successful update.
        """
        with self._lock:
            resolved = self._resolve_secrets(copy.deepcopy(overlay))
            prospective = deep_merge(self.data, resolved)
            # validate() returns the *cleaned* config -- channels normalised to
            # bare logins, numbers coerced to their declared types -- so its
            # return value is what gets stored, not the raw input.
            self.data = validate(prospective)

    def apply_and_save(self, overlay: dict[str, Any]) -> None:
        """Apply an update and persist it, or change neither.

        AUD2-021: `apply()` then `save()` as separate steps left live memory
        holding a change that disk had rejected -- a full drive or a locked file
        produced a running application configured differently from the file it
        would reload on restart, plus a `config.json.tmp` that could contain
        plaintext secrets. Staging to disk first and only then swapping memory
        makes the pair atomic from the caller's point of view.
        """
        with self._lock:
            resolved = self._resolve_secrets(copy.deepcopy(overlay))
            local = _changed_overlay(self._origin, self.data)
            with _config_write_lock(self.path):
                latest = deep_merge(_read_validated(self.path), local)
                cleaned = validate(deep_merge(latest, resolved))
                atomic_write_json(self.path, cleaned, mode=0o600)
            # Publish to memory only after the durable write succeeds. Runtime roots
            # deliberately remain the values captured by __init__.
            self.data = cleaned
            self._origin = copy.deepcopy(cleaned)

    def mutate_and_save(self, mutator: Callable[[dict[str, Any]], _T]) -> _T:
        """Atomically mutate the latest config and persist the validated result.

        The callback receives a private copy while the cross-process lock is held,
        making list read-modify-write operations (notably channels) lossless. It may
        return any caller result. Validation, callback, or write failure changes
        neither this object's data nor the on-disk config.
        """
        if not callable(mutator):
            raise TypeError("mutator must be callable")
        with self._lock:
            local = _changed_overlay(self._origin, self.data)
            with _config_write_lock(self.path):
                prospective = deep_merge(_read_validated(self.path), local)
                result = mutator(prospective)
                cleaned = validate(prospective)
                atomic_write_json(self.path, cleaned, mode=0o600)
            self.data = cleaned
            self._origin = copy.deepcopy(cleaned)
            return result

    def _resolve_secrets(self, overlay: dict[str, Any]) -> dict[str, Any]:
        """Apply set/keep/clear semantics to secret fields.

        `MASK` means keep whatever is stored; `CLEAR` means deliberately erase it;
        an omitted key means keep. A plain empty string also means keep, because
        the dashboard sends empty for untouched password inputs -- clearing needs
        to be explicit, or every settings save would wipe the keys.
        """
        secrets = overlay.get("secrets")
        if not isinstance(secrets, dict):
            return overlay

        resolved: dict[str, Any] = {}
        for key, value in secrets.items():
            if value == CLEAR:
                resolved[key] = ""
            elif value == MASK or value == "":
                continue
            else:
                resolved[key] = value
        overlay["secrets"] = resolved
        return overlay

    def secret(self, name: str) -> str:
        """Config first, then environment, so CI or a shell export still works."""
        value = (self.get(f"secrets.{name}") or "").strip()
        if value:
            return value
        return os.environ.get(name.upper(), "").strip()

    def redacted(self) -> dict[str, Any]:
        clone = copy.deepcopy(self.data)
        secrets = clone.setdefault("secrets", {})
        for key in SECRET_KEYS:
            secrets[key] = MASK if (secrets.get(key) or os.environ.get(key.upper())) else ""
        return clone

    # -- derived paths -----------------------------------------------------

    @staticmethod
    def _resolve_root(value: Any) -> Path:
        return Path(os.path.expanduser(value)).resolve()

    @property
    def masters_root(self) -> Path:
        return self._masters_root

    @property
    def work_root(self) -> Path:
        return self._work_root

    def channel_root(self, channel: str) -> Path:
        return self.masters_root / channel.lower()
