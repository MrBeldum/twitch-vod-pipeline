"""Shared helpers: tool discovery, subprocess plumbing, ffprobe, atomic writes.

Everything here is stdlib-only on purpose. Python 3.14 is new enough that several
common wheels are still missing, and a recorder that cannot start because a
transitive dependency failed to build is worse than no recorder at all.
"""

from __future__ import annotations

import json
import logging
import os
import errno
import shutil
import subprocess
import sys
import tempfile
import threading
import unicodedata
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

LOG = logging.getLogger("vodpipe")

# On Windows every subprocess we spawn would otherwise flash a console window when
# the dashboard is launched from a shortcut.
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


# --------------------------------------------------------------------------- tools


@dataclass(frozen=True)
class Tools:
    ffmpeg: str
    ffprobe: str
    streamlink: str
    claude: str | None


_WINDOWS_HINTS = {
    "ffmpeg": [r"C:\ffmpeg\bin\ffmpeg.exe"],
    "ffprobe": [r"C:\ffmpeg\bin\ffprobe.exe"],
    "streamlink": [
        r"C:\Users\%USERNAME%\AppData\Local\Programs\Streamlink\bin\streamlink.exe",
        r"C:\Program Files\Streamlink\bin\streamlink.exe",
    ],
    "claude": [r"C:\Users\%USERNAME%\.local\bin\claude.exe"],
}


def find_tool(name: str, override: str | None = None) -> str | None:
    """Locate an executable: explicit config wins, then PATH, then known install spots."""
    if override:
        return override if Path(override).exists() else None

    found = shutil.which(name)
    if found:
        return found

    for hint in _WINDOWS_HINTS.get(name, []):
        candidate = Path(os.path.expandvars(hint))
        if candidate.exists():
            return str(candidate)
    return None


# Everything the recorder needs. Commands that never record ask for less.
REQUIRED_TOOLS = ("ffmpeg", "ffprobe", "streamlink")


def resolve_tools(overrides: dict[str, str] | None = None, *,
                  need: Sequence[str] = REQUIRED_TOOLS) -> Tools:
    """Locate the executables, insisting only on the ones `need` names.

    `need` exists because `vodpipe transcribe some.mp4` was refusing to run on a
    machine without streamlink installed, for work that never goes near it.
    """
    overrides = overrides or {}
    missing = []
    resolved: dict[str, str | None] = {}
    for name in REQUIRED_TOOLS:
        path = find_tool(name, overrides.get(name))
        if not path and name in need:
            missing.append(name)
        resolved[name] = path
    if missing:
        raise RuntimeError(
            f"Required tool(s) not found: {', '.join(missing)}. "
            "Set an explicit path under \"tools\" in config.json."
        )
    return Tools(
        # A tool that was not required and was not found is recorded as an empty
        # string: using it would fail loudly at the point of use, which is exactly
        # where the error belongs.
        ffmpeg=resolved["ffmpeg"] or "",
        ffprobe=resolved["ffprobe"] or "",
        streamlink=resolved["streamlink"] or "",
        claude=find_tool("claude", overrides.get("claude")),
    )


# --------------------------------------------------------------------- subprocesses


# Free-form text (a stderr tail) can still mention a credential. The optional
# scheme word matters: "Authorization=Token <secret>" would otherwise have its
# scheme consumed as the value and leave the secret in the clear.
_SECRET_TEXT = re.compile(
    r"(?i)\b(authorization|token|api[-_]?key)\b\s*[:=]?\s*"
    r"(?:(?:bearer|token|oauth)[\s:=]+)?\S+")

# argv elements that either carry a credential or introduce one.
_CREDENTIAL_FLAGS = ("--twitch-api-header", "--twitch-access-token-param")


def redact(text: str) -> str:
    """Strip credentials out of free-form text before it is logged."""
    return _SECRET_TEXT.sub(lambda match: f"{match.group(1)}=<redacted>", text)


def render_command(cmd: Sequence[str]) -> str:
    """Render a command for logging with credentials removed.

    Redaction is per-argument rather than over the joined string: the Twitch
    OAuth token is its own argv element (`--twitch-api-header`, then
    `Authorization=OAuth <token>`), so a regex over the joined line reliably
    missed the value. Debug logging a command verbatim previously wrote the
    user's token to the console and to any log file.

    Note this does not hide the token from process inspection while streamlink
    runs -- that is inherent to passing a credential on a command line.
    """
    parts: list[str] = []
    redact_next = False
    for item in cmd:
        text = str(item)
        lowered = text.lower()
        if redact_next:
            parts.append("<redacted>")
            redact_next = False
            continue
        if lowered.startswith(_CREDENTIAL_FLAGS):
            if "=" in text:
                parts.append(text.split("=", 1)[0] + "=<redacted>")
            else:
                # The value is the following argument.
                parts.append(text)
                redact_next = True
            continue
        if any(marker in lowered for marker in ("authorization", "api-key", "apikey")):
            parts.append("<redacted>")
            continue
        parts.append(text)
    return " ".join(parts)


def run(
    cmd: Sequence[str],
    *,
    timeout: float | None = None,
    check: bool = False,
    stdin_bytes: bytes | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a command to completion, capturing text output."""
    LOG.debug("run: %s", render_command(cmd))
    proc = subprocess.run(
        [str(part) for part in cmd],
        input=stdin_bytes,
        capture_output=True,
        timeout=timeout,
        creationflags=_NO_WINDOW,
    )
    result = subprocess.CompletedProcess(
        proc.args,
        proc.returncode,
        proc.stdout.decode("utf-8", "replace") if proc.stdout else "",
        proc.stderr.decode("utf-8", "replace") if proc.stderr else "",
    )
    if check and result.returncode != 0:
        raise RuntimeError(
            f"{cmd[0]} exited {result.returncode}: "
            f"{redact(result.stderr.strip()[-2000:])}"
        )
    return result


def popen(cmd: Sequence[str], **kwargs: Any) -> subprocess.Popen:
    LOG.debug("popen: %s", render_command(cmd))
    return subprocess.Popen(
        [str(part) for part in cmd], creationflags=_NO_WINDOW, **kwargs
    )


# -------------------------------------------------------------------------- ffprobe


def ffprobe_json(ffprobe: str, path: Path, *extra: str) -> dict[str, Any]:
    result = run(
        [ffprobe, "-v", "error", "-of", "json", "-show_format", "-show_streams",
         *extra, str(path)],
        timeout=120,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return {}
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return {}


def media_duration(ffprobe: str, path: Path, *, allow_scan: bool = True) -> float:
    """Elapsed duration in seconds, tolerant of a file ffmpeg is still writing.

    Returns *elapsed* time, not the final timestamp. MPEG-TS typically starts at a
    nonzero PTS, and every seek we issue is file-relative, so elapsed is the only
    figure that composes correctly with the rest of the pipeline.

    `allow_scan=False` suppresses the packet-walk fallback. Live callers pass it
    because that fallback reads the whole file, which is ruinous to repeat every
    few seconds against a chunk growing towards 14 GB.
    """
    probe = ffprobe_json(ffprobe, path)
    duration = _as_float(probe.get("format", {}).get("duration"))
    if duration and duration > 0:
        return duration

    for stream in probe.get("streams", []):
        duration = _as_float(stream.get("duration"))
        if duration and duration > 0:
            return duration

    if not allow_scan:
        return 0.0

    # Last resort for a stream ffprobe cannot summarise. Elapsed = last - first;
    # returning the last PTS alone would overstate a stream that starts at 1.4s.
    result = run(
        [ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries",
         "packet=pts_time", "-of", "csv=p=0", str(path)],
        timeout=120,
    )
    times = [_as_float(line) for line in result.stdout.splitlines() if line.strip()]
    times = [value for value in times if value is not None]
    if not times:
        return 0.0
    return max(0.0, max(times) - min(times))


def video_info(ffprobe: str, path: Path) -> dict[str, Any]:
    probe = ffprobe_json(ffprobe, path)
    for stream in probe.get("streams", []):
        if stream.get("codec_type") == "video":
            return {
                "codec": stream.get("codec_name"),
                "width": stream.get("width"),
                "height": stream.get("height"),
                "fps": _parse_fraction(stream.get("avg_frame_rate")
                                       or stream.get("r_frame_rate")),
            }
    return {}


def _as_float(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return None if result != result else result  # reject NaN


def _parse_fraction(value: Any) -> float | None:
    if not value or not isinstance(value, str) or "/" not in value:
        return _as_float(value)
    numerator, _, denominator = value.partition("/")
    num = _as_float(numerator)
    den = _as_float(denominator)
    if not num or not den:
        return None
    return num / den


# ----------------------------------------------------------------------------- io


_WRITE_LOCK = threading.Lock()


def _fsync_parent(path: Path) -> None:
    """Flush a completed rename to stable storage where directory fsync exists."""
    if os.name == "nt":
        # The stdlib cannot open a Windows directory as a file descriptor. ReplaceFile
        # is durable enough for ordinary operation, but there is no parent handle to
        # FlushFileBuffers here.
        return

    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    unsupported = {
        errno.EACCES,
        errno.EBADF,
        errno.EINVAL,
        getattr(errno, "ENOTSUP", errno.EINVAL),
        getattr(errno, "EOPNOTSUPP", errno.EINVAL),
    }
    try:
        fd = os.open(path.parent, flags)
    except OSError as exc:
        if exc.errno in unsupported:
            return
        raise
    try:
        try:
            os.fsync(fd)
        except OSError as exc:
            if exc.errno not in unsupported:
                raise
    finally:
        os.close(fd)


def atomic_write_text(path: Path, text: str, *, mode: int | None = None) -> None:
    """Write via a sibling temp file so a reader never sees a half-written export.

    The file is flushed before replacement and the parent directory afterwards where
    the platform supports it. A unique temp avoids cross-process writers trampling
    each other, and every failure path removes that temp. `mode=0o600` is used for
    secret-bearing files such as config.json.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _WRITE_LOCK:
        fd = -1
        temp: Path | None = None
        try:
            fd, temp_name = tempfile.mkstemp(
                prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
            temp = Path(temp_name)
            if mode is not None:
                try:
                    os.fchmod(fd, mode)
                except (AttributeError, NotImplementedError, OSError):
                    # mkstemp is already restrictive on POSIX. Windows exposes only
                    # limited chmod semantics and relies primarily on inherited ACLs.
                    pass
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                fd = -1
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, path)
            temp = None
            _fsync_parent(path)
        finally:
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass
            if temp is not None:
                try:
                    temp.unlink(missing_ok=True)
                except OSError:
                    pass


def atomic_write_json(path: Path, value: Any, *, mode: int | None = None) -> None:
    atomic_write_text(
        path, json.dumps(value, indent=2, ensure_ascii=False) + "\n", mode=mode)


def read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def ensure_within(root: Path, candidate: Path) -> Path:
    """Resolve `candidate` and prove it sits inside `root`.

    Used wherever caller-supplied text becomes part of a path. `resolve()` first,
    because containment has to be judged after `..`, symlinks and drive letters
    have been collapsed -- a prefix check on the raw string is not a check at all.
    """
    root = root.resolve()
    target = Path(candidate).resolve()
    if target != root and root not in target.parents:
        raise ValueError(f"{target} is outside {root}")
    return target


def safe_name_component(value: str, *, what: str = "name") -> str:
    """A single path component with no separators, traversal or reserved names."""
    text = str(value).strip()
    if not text:
        raise ValueError(f"{what} is required")
    if len(text) > 64:
        raise ValueError(f"{what} is too long")
    if any(character in text for character in '\\/:*?"<>|') or "\0" in text:
        raise ValueError(f"{what} contains characters that are not allowed")
    if text in (".", "..") or text.strip(". ") != text:
        raise ValueError(f"{what} is not a usable folder name")
    if text.split(".")[0].lower() in {
        "con", "prn", "aux", "nul",
        *(f"com{i}" for i in range(1, 10)),
        *(f"lpt{i}" for i in range(1, 10)),
    }:
        raise ValueError(f"{what} is a reserved device name on Windows")
    return text


def free_bytes(path: Path) -> int:
    probe = path
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    return shutil.disk_usage(probe).free


def dir_size(path: Path) -> int:
    total = 0
    for root, _, files in os.walk(path):
        for name in files:
            try:
                total += (Path(root) / name).stat().st_size
            except OSError:
                pass
    return total


# ------------------------------------------------------------------------ strings


_SLUG_STRIP = re.compile(r"[^a-z0-9._-]+")


def slugify(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    return _SLUG_STRIP.sub("-", normalized.strip().lower()).strip("-_.") or "untitled"


def fmt_clock(seconds: float) -> str:
    seconds = max(0.0, seconds)
    hours, rest = divmod(int(seconds), 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def fmt_srt_time(seconds: float) -> str:
    total_ms = max(0, round(seconds * 1000))
    hours, rest = divmod(total_ms, 3_600_000)
    minutes, rest = divmod(rest, 60_000)
    secs, millis = divmod(rest, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def human_bytes(count: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(count) < 1024 or unit == "TB":
            return f"{count:.1f} {unit}" if unit != "B" else f"{int(count)} B"
        count /= 1024
    return f"{count:.1f} TB"


def round3(value: float) -> float:
    """The one rounding rule for every published timestamp.

    Premiere reads these numbers directly and our own boundary maths compares
    them, so they are rounded identically everywhere or not at all.
    """
    return round(value + 0.0, 3)


# ----------------------------------------------------------------------- logging


def setup_logging(log_file: Path | None = None, verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    root = logging.getLogger("vodpipe")
    root.setLevel(level)
    root.handlers.clear()

    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s",
                            datefmt="%H:%M:%S")
    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(fmt)
    root.addHandler(console)

    if log_file:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(log_file, encoding="utf-8")
        handler.setFormatter(fmt)
        root.addHandler(handler)
