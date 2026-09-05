"""Session and chunk state: the shared truth the recorder writes and the dashboard reads.

Every mutation goes through the store's lock and is flushed to the session's own
`session.json`, so a crashed process leaves behind a directory that still explains
itself and the dashboard can repopulate from disk on restart.
"""

from __future__ import annotations

import copy
import json
import math
import re
import secrets
import shutil
import threading
import time
from dataclasses import asdict, dataclass, field, fields as dataclass_fields
from pathlib import Path, PurePath
from typing import Any

from .channels import InvalidChannel, parse_channel
from .locks import ResourceLock
from .util import LOG, atomic_write_json, safe_name_component

# Chunk lifecycle.
STARTING = "starting"
RECORDING = "recording"
REMUXING = "remuxing"
COMPLETE = "complete"
FAILED = "failed"
INTERRUPTED = "interrupted"

# Per-artefact job states.
PENDING = "pending"
RUNNING = "running"
DONE = "done"
ERROR = "error"
SKIPPED = "skipped"
EXPIRED = "expired"

# A missing version identifies the legacy schema. It remains loadable for
# existing data, but goes through the same strict field validation as v1.
MANIFEST_VERSION = 1

_SESSION_STATES = frozenset({STARTING, RECORDING, COMPLETE, FAILED, INTERRUPTED})
_CHUNK_STATES = frozenset(
    {STARTING, RECORDING, REMUXING, COMPLETE, FAILED, INTERRUPTED})
_ARTIFACT_STATES = frozenset({PENDING, RUNNING, DONE, ERROR, SKIPPED})
_PROXY_STATES = _ARTIFACT_STATES | {EXPIRED}

_SESSION_FIELDS = frozenset({
    "version", "session_id", "channel", "started_at", "ended_at",
    "directory", "status", "error", "title", "ad_events", "ad_ranges",
    "quality_selected", "quality_available", "quality_warning", "chunks",
    "source_kind", "source_url",
})

# How a session's media was obtained. "live" is the default and the value legacy
# manifests (which predate the field) are read as.
SOURCE_LIVE = "live"
SOURCE_VOD = "vod"
_SOURCE_KINDS = frozenset({SOURCE_LIVE, SOURCE_VOD})
_CHUNK_FIELDS = frozenset({
    "index", "session_id", "channel", "started_at", "ts_name",
    "master_name", "proxy_name", "duration", "size_bytes", "status",
    "master_error", "session_offset", "proxy_status", "proxy_error",
    "transcript_status", "transcript_error", "summary_status",
    "summary_error", "chat_status", "chat_error",
    "transcribed_through", "word_count", "ended_at",
    "width", "height", "label", "errors", "error",
})

# Fields earlier versions persisted and this one does not. They are still
# *accepted* on read, because `_known_fields` raises on anything it does not
# recognise and every session recorded while the edited cut existed carries
# them -- deleting them from `_CHUNK_FIELDS` alone would make those manifests
# unreadable and the application unable to start. They are dropped on load and
# never written, so they are gone from the file after the next save. This is
# `schema.RETIRED_PATHS` for the manifest.
_RETIRED_CHUNK_FIELDS = frozenset({
    "edit_status", "edit_error", "edit_name",
})
_RETIRED_ERRORS = frozenset({"edit"})


class ManifestValidationError(ValueError):
    """A persisted session manifest is not safe or internally valid."""


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ManifestValidationError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _invalid_json_constant(value: str) -> Any:
    raise ManifestValidationError(f"non-finite JSON number {value}")


def _read_manifest(path: Path) -> Any:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ManifestValidationError(f"could not read UTF-8 JSON: {exc}") from exc
    try:
        return json.loads(text, object_pairs_hook=_reject_duplicate_keys,
                          parse_constant=_invalid_json_constant)
    except ManifestValidationError:
        raise
    except json.JSONDecodeError as exc:
        raise ManifestValidationError(
            f"malformed JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}"
        ) from exc


def _object(value: Any, where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ManifestValidationError(f"{where} must be an object")
    return value


def _list(value: Any, where: str) -> list[Any]:
    if not isinstance(value, list):
        raise ManifestValidationError(f"{where} must be a list")
    return value


def _text(value: Any, where: str, *, empty: bool = True) -> str:
    if not isinstance(value, str):
        raise ManifestValidationError(f"{where} must be text")
    if not empty and not value:
        raise ManifestValidationError(f"{where} must not be empty")
    return value


def _number(value: Any, where: str, *, integer: bool = False) -> int | float:
    expected = int if integer else (int, float)
    if isinstance(value, bool) or not isinstance(value, expected):
        kind = "an integer" if integer else "a number"
        raise ManifestValidationError(f"{where} must be {kind}")
    try:
        finite = math.isfinite(value)
    except OverflowError as exc:
        raise ManifestValidationError(f"{where} must be finite") from exc
    if not finite or value < 0:
        raise ManifestValidationError(f"{where} must be finite and nonnegative")
    return value


def _status(value: Any, where: str, allowed: frozenset[str]) -> str:
    text = _text(value, where)
    if text not in allowed:
        raise ManifestValidationError(
            f"{where} must be one of {', '.join(sorted(allowed))}")
    return text


def _known_fields(data: dict[str, Any], allowed: frozenset[str], where: str) -> None:
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise ManifestValidationError(
            f"{where} has unknown field(s): {', '.join(unknown)}")


def _is_plain_filename(value: str) -> bool:
    """True only for a single filename with no directory part of any kind.

    Deliberately strict, and checked against both separators regardless of
    platform: a manifest written on one OS can be read on another, and the
    question is whether joining this onto a directory can ever leave it.
    """
    if value in ("", ".", ".."):
        return False
    if len(value) > 255:
        return False
    if any(character in value for character in '/\\:*?"<>|'):
        return False
    if any(ord(character) < 32 for character in value):
        return False
    if value.rstrip(". ") != value:
        return False
    if value.split(".")[0].lower() in {
            "con", "prn", "aux", "nul",
            *(f"com{i}" for i in range(1, 10)),
            *(f"lpt{i}" for i in range(1, 10))}:
        return False
    return PurePath(value).name == value


def _safe_component(value: Any, where: str) -> str:
    text = _text(value, where, empty=False)
    try:
        safe = safe_name_component(text, what=where)
    except ValueError as exc:
        raise ManifestValidationError(str(exc)) from exc
    if safe != text or not _is_plain_filename(text):
        raise ManifestValidationError(f"{where} is not a safe path component")
    return text


def _canonical_channel(value: Any, where: str) -> str:
    text = _text(value, where, empty=False)
    try:
        parsed = parse_channel(text)
    except InvalidChannel as exc:
        raise ManifestValidationError(f"{where}: {exc}") from exc
    if parsed != text:
        raise ManifestValidationError(
            f"{where} must be the canonical bare channel name {parsed!r}")
    return text


def _validate_chunk(raw: Any, position: int, session_id: str,
                    channel: str, seen_indexes: set[int]) -> None:
    where = f"chunks[{position}]"
    data = _object(raw, where)
    _known_fields(data, _CHUNK_FIELDS | _RETIRED_CHUNK_FIELDS, where)

    for required in ("index", "session_id", "channel", "started_at"):
        if required not in data:
            raise ManifestValidationError(f"{where}.{required} is required")

    index = _number(data["index"], f"{where}.index", integer=True)
    assert isinstance(index, int)
    if index in seen_indexes:
        raise ManifestValidationError(f"duplicate chunk index {index}")
    seen_indexes.add(index)

    chunk_session_id = _text(
        data["session_id"], f"{where}.session_id", empty=False)
    if chunk_session_id != session_id:
        raise ManifestValidationError(
            f"{where}.session_id does not match the session")
    chunk_channel = _canonical_channel(data["channel"], f"{where}.channel")
    if chunk_channel != channel:
        raise ManifestValidationError(f"{where}.channel does not match the session")

    _number(data["started_at"], f"{where}.started_at")
    _number(data.get("ended_at", 0.0), f"{where}.ended_at")

    for name in ("ts_name", "master_name", "proxy_name"):
        value = _text(data.get(name, ""), f"{where}.{name}")
        if value and not _is_plain_filename(value):
            raise ManifestValidationError(
                f"{where}.{name} must name one file inside the session")

    _status(data.get("status", RECORDING), f"{where}.status", _CHUNK_STATES)
    _status(data.get("proxy_status", PENDING),
            f"{where}.proxy_status", _PROXY_STATES)
    _status(data.get("transcript_status", PENDING),
            f"{where}.transcript_status", _ARTIFACT_STATES)
    _status(data.get("summary_status", PENDING),
            f"{where}.summary_status", _ARTIFACT_STATES)
    _status(data.get("chat_status", PENDING),
            f"{where}.chat_status", _ARTIFACT_STATES)

    for name in ("master_error", "proxy_error", "transcript_error",
                 "summary_error", "chat_error", "error"):
        if name in data:
            _text(data[name], f"{where}.{name}")

    for name in ("duration", "session_offset", "transcribed_through"):
        _number(data.get(name, 0.0), f"{where}.{name}")
    for name in ("size_bytes", "word_count", "width", "height"):
        _number(data.get(name, 0), f"{where}.{name}", integer=True)

    if "label" in data:
        label = _text(data["label"], f"{where}.label")
        if label != f"c{index:03d}":
            raise ManifestValidationError(f"{where}.label does not match its index")
    if "errors" in data:
        errors = _object(data["errors"], f"{where}.errors")
        allowed_errors = ({"master", "proxy", "transcript", "summary", "chat"}
                          | _RETIRED_ERRORS)
        unknown = sorted(set(errors) - allowed_errors)
        if unknown:
            raise ManifestValidationError(
                f"{where}.errors has unknown artifact(s): {', '.join(unknown)}")
        for artifact, message in errors.items():
            _text(message, f"{where}.errors.{artifact}")


def _validate_manifest(data: Any, masters_root: Path, manifest: Path) -> None:
    root = _object(data, "manifest")
    _known_fields(root, _SESSION_FIELDS, "manifest")

    version = root.get("version")
    if version is not None:
        if isinstance(version, bool) or not isinstance(version, int):
            raise ManifestValidationError("version must be an integer")
        if version != MANIFEST_VERSION:
            raise ManifestValidationError(
                f"unsupported manifest version {version}; expected {MANIFEST_VERSION}")

    for required in ("session_id", "channel", "directory"):
        if required not in root:
            raise ManifestValidationError(f"{required} is required")

    session_id = _safe_component(root["session_id"], "session_id")
    channel = _canonical_channel(root["channel"], "channel")
    _number(root.get("started_at", 0.0), "started_at")
    _number(root.get("ended_at", 0.0), "ended_at")
    _status(root.get("status", COMPLETE), "status", _SESSION_STATES)
    _status(root.get("source_kind", SOURCE_LIVE), "source_kind", _SOURCE_KINDS)

    for name in ("error", "title", "quality_selected", "quality_warning",
                 "source_url"):
        if name in root:
            _text(root[name], name)

    available = _list(root.get("quality_available", []), "quality_available")
    for position, name in enumerate(available):
        _text(name, f"quality_available[{position}]")

    events = _list(root.get("ad_events", []), "ad_events")
    event_fields = frozenset(
        {"kind", "detail", "at_wall", "approx_session_seconds"})
    for position, raw_event in enumerate(events):
        where = f"ad_events[{position}]"
        event = _object(raw_event, where)
        _known_fields(event, event_fields, where)
        for required in event_fields:
            if required not in event:
                raise ManifestValidationError(f"{where}.{required} is required")
        _text(event["kind"], f"{where}.kind", empty=False)
        _text(event["detail"], f"{where}.detail")
        _number(event["at_wall"], f"{where}.at_wall")
        _number(event["approx_session_seconds"],
                f"{where}.approx_session_seconds")

    # Validate and ignore this obsolete shape so legacy manifests load without
    # resurrecting the old, invalid media-exclusion behavior.
    if "ad_ranges" in root:
        ranges = _list(root["ad_ranges"], "ad_ranges")
        for position, raw_range in enumerate(ranges):
            where = f"ad_ranges[{position}]"
            values = _list(raw_range, where)
            if len(values) != 2:
                raise ManifestValidationError(f"{where} must contain two numbers")
            start = _number(values[0], f"{where}[0]")
            end = _number(values[1], f"{where}[1]")
            if end < start:
                raise ManifestValidationError(f"{where} ends before it starts")

    chunks = _list(root.get("chunks", []), "chunks")
    seen_indexes: set[int] = set()
    for position, raw_chunk in enumerate(chunks):
        _validate_chunk(raw_chunk, position, session_id, channel, seen_indexes)

    root_path = masters_root.resolve()
    session_path = manifest.parent.resolve()
    if (session_path == root_path or root_path not in session_path.parents or
            session_path.parent.parent != root_path):
        raise ManifestValidationError("manifest is outside the rooted session layout")
    if manifest.parent.name != session_id:
        raise ManifestValidationError("session_id does not match its directory name")
    if manifest.parent.parent.name != channel:
        raise ManifestValidationError("channel does not match its directory name")

    directory_text = _text(root["directory"], "directory", empty=False)
    if ".." in directory_text.replace("\\", "/").split("/"):
        raise ManifestValidationError("directory must not contain traversal")
    directory = Path(directory_text)
    if not directory.is_absolute():
        raise ManifestValidationError("directory must be absolute")
    if directory.resolve() != session_path:
        raise ManifestValidationError(
            "directory does not name this manifest's rooted session directory")


@dataclass
class Chunk:
    index: int
    session_id: str
    channel: str
    started_at: float
    ts_name: str = ""
    master_name: str = ""
    proxy_name: str = ""
    duration: float = 0.0
    size_bytes: int = 0
    # Lifecycle of the master itself. Each artifact carries its own status and
    # error: a shared field let a successful remux erase a transcription failure,
    # so a chunk could look healthy while an output was missing.
    status: str = RECORDING
    master_error: str = ""
    # Offset of this chunk's t=0 within the whole session, so a chunk-relative
    # transcript timestamp can be mapped back onto the broadcast.
    session_offset: float = 0.0
    proxy_status: str = PENDING
    proxy_error: str = ""
    transcript_status: str = PENDING
    transcript_error: str = ""
    summary_status: str = PENDING
    summary_error: str = ""
    chat_status: str = PENDING
    chat_error: str = ""
    # Seconds of audio already sent to the transcriber, chunk-relative.
    transcribed_through: float = 0.0
    word_count: int = 0
    ended_at: float = 0.0
    # Ground truth read back off the finished master, as opposed to the rendition
    # name streamlink said it was opening. Both are recorded because they can
    # disagree: the name is what we asked Twitch for, this is what we got.
    width: int = 0
    height: int = 0

    @property
    def label(self) -> str:
        return f"c{self.index:03d}"

    @property
    def errors(self) -> dict[str, str]:
        """Every artifact failure on this chunk, keyed by artifact."""
        pairs = (("master", self.master_error), ("proxy", self.proxy_error),
                 ("transcript", self.transcript_error),
                 ("summary", self.summary_error),
                 ("chat", self.chat_error))
        return {name: text for name, text in pairs if text}

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["label"] = self.label
        data["errors"] = self.errors
        return data


@dataclass
class Session:
    session_id: str
    channel: str
    started_at: float
    directory: str
    status: str = RECORDING
    ended_at: float = 0.0
    error: str = ""
    chunks: list[Chunk] = field(default_factory=list)
    # Ad events noted during the broadcast. Operational metadata only: these are
    # observations about what Twitch served, NOT intervals of this recording.
    # streamlink filters ad segments before they reach us, so no range of our
    # media corresponds to them. Never feed these to a cut or an exclusion.
    ad_events: list[dict[str, Any]] = field(default_factory=list)
    title: str = ""
    # The rendition streamlink opened, the whole ladder Twitch offered, and a
    # human explanation when the capture came in under the configured floor.
    # Recorded on every session so a disappointing master can be explained after
    # the fact instead of being rediscovered by opening the file in Premiere.
    quality_selected: str = ""
    quality_available: list[str] = field(default_factory=list)
    quality_warning: str = ""
    # How this session's media was obtained. "live" (streamlink from the channel's
    # live edge) or "vod" (streamlink downloading an archived VOD). Both flow
    # through the identical chunk/remux/proxy/transcript/rundown pipeline; this
    # only records provenance and lets the dashboard label VOD sessions and offer
    # the right stop control.
    source_kind: str = SOURCE_LIVE
    source_url: str = ""

    @property
    def path(self) -> Path:
        return Path(self.directory)

    def chunk(self, index: int) -> Chunk | None:
        for item in self.chunks:
            if item.index == index:
                return item
        return None

    def active_chunk(self) -> Chunk | None:
        for item in reversed(self.chunks):
            if item.status == RECORDING:
                return item
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": MANIFEST_VERSION,
            "session_id": self.session_id,
            "channel": self.channel,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "directory": self.directory,
            "status": self.status,
            "error": self.error,
            "title": self.title,
            "ad_events": self.ad_events,
            "quality_selected": self.quality_selected,
            "quality_available": list(self.quality_available),
            "quality_warning": self.quality_warning,
            "source_kind": self.source_kind,
            "source_url": self.source_url,
            "chunks": [chunk.to_dict() for chunk in self.chunks],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Session":
        chunks = []
        for raw in data.get("chunks", []):
            # `label` and `errors` are derived, and the retired fields belonged
            # to a feature this build does not have -- both are dropped here so
            # `Chunk(**fields)` sees only what it declares, and neither is
            # written back on the next save.
            fields = {key: value for key, value in raw.items()
                      if key not in ("label", "errors")
                      and key not in _RETIRED_CHUNK_FIELDS}
            # AUD2-064: artifact names come off disk and are joined straight onto
            # the session directory, and recovery *deletes* what they point at.
            # An absolute name replaces the prefix entirely -- `master_name` set
            # to `C:/Users/.../important.mp4` had recovery validate and unlink
            # that file -- and `..` walks out just as easily. Anything that is not
            # a single plain filename is dropped rather than trusted.
            for field_name in ("ts_name", "master_name", "proxy_name"):
                value = str(fields.get(field_name) or "")
                if value and not _is_plain_filename(value):
                    LOG.warning(
                        "session %s chunk %s: ignoring unsafe %s %r from the "
                        "manifest -- it does not name a file inside the session",
                        data.get("session_id"), raw.get("index"), field_name, value)
                    fields[field_name] = ""
            # A pre-split state file carried one shared `error`; attribute it to
            # the master rather than dropping it.
            legacy = fields.pop("error", "")
            if legacy and not fields.get("master_error"):
                fields["master_error"] = legacy
            chunks.append(Chunk(**fields))
        session = cls(
            session_id=data["session_id"],
            channel=data["channel"],
            started_at=data.get("started_at", 0.0),
            directory=data.get("directory", ""),
            status=data.get("status", COMPLETE),
            ended_at=data.get("ended_at", 0.0),
            error=data.get("error", ""),
            title=data.get("title", ""),
            quality_selected=str(data.get("quality_selected", "") or ""),
            quality_warning=str(data.get("quality_warning", "") or ""),
            source_kind=str(data.get("source_kind", SOURCE_LIVE) or SOURCE_LIVE),
            source_url=str(data.get("source_url", "") or ""),
        )
        session.quality_available = [str(name) for name
                                     in data.get("quality_available", [])
                                     if isinstance(name, str)]
        session.chunks = chunks
        # `ad_ranges` from an older build is deliberately not carried forward: it
        # held log-derived media intervals that were never valid.
        session.ad_events = [dict(item) for item in data.get("ad_events", [])
                             if isinstance(item, dict)]
        return session


class SessionStore:
    """Thread-safe registry of sessions, persisted one JSON file per session."""

    def __init__(self, masters_root: Path) -> None:
        self.masters_root = Path(masters_root).resolve()
        self._lock = threading.RLock()
        self._sessions: dict[str, Session] = {}
        # The last validated disk state synchronized into each live object. A
        # write is a patch from this snapshot, not a replacement of the manifest.
        self._bases: dict[str, dict[str, Any]] = {}
        self._manifest_diagnostics: list[dict[str, Any]] = []
        self._diagnostic_paths: set[str] = set()

    # -- lifecycle ---------------------------------------------------------

    def add(self, session: Session) -> Session:
        with self._lock:
            self._sessions[session.session_id] = session
            self._flush(session)
        return session

    def get(self, session_id: str) -> Session | None:
        with self._lock:
            return self._sessions.get(session_id)

    def all(self) -> list[Session]:
        with self._lock:
            return sorted(self._sessions.values(),
                          key=lambda item: item.started_at, reverse=True)

    def active(self) -> list[Session]:
        return [item for item in self.all()
                if item.status in (STARTING, RECORDING)]

    def active_for_channel(self, channel: str) -> Session | None:
        for session in self.active():
            if session.channel.lower() == channel.lower():
                return session
        return None

    def update(self, session: Session, **changes: Any) -> Session:
        with self._lock:
            for key, value in changes.items():
                setattr(session, key, value)
            self._flush(session)
        return session

    def update_chunk(self, session: Session, chunk: Chunk, **changes: Any) -> Chunk:
        with self._lock:
            for key, value in changes.items():
                setattr(chunk, key, value)
            self._flush(session)
        return chunk

    def add_chunk(self, session: Session, chunk: Chunk) -> Chunk:
        """Register a chunk, or return the one already holding that index.

        Get-or-create rather than blind append. Two records with the same index
        share a label, a `.ts` name and a master name, so they cannot both be
        right: they produced duplicate finalisation callbacks, contradictory
        status, and a spurious failure when whichever finished second found the
        shared `.ts` already reclaimed.
        """
        with self._lock:
            for existing in session.chunks:
                if existing.index == chunk.index:
                    return existing
            session.chunks.append(chunk)
            self._flush(session)
        return chunk

    def confirm_first_media(self, session: Session) -> None:
        """Commit the startup-to-capture transition in one manifest write."""
        with self._lock:
            changed = False
            if session.status == STARTING:
                session.status = RECORDING
                changed = True
            for chunk in session.chunks:
                if chunk.status == STARTING:
                    chunk.status = RECORDING
                    changed = True
            if changed:
                self._flush(session)

    def add_ad_event(self, session: Session, kind: str, detail: str,
                     at_wall: float, at_session: float) -> None:
        """Note an ad observation. Informational; never an exclusion interval."""
        with self._lock:
            session.ad_events.append({
                "kind": kind,
                "detail": detail[:300],
                "at_wall": round(at_wall, 3),
                "approx_session_seconds": round(at_session, 3),
            })
            self._flush(session)

    def flush(self, session: Session) -> None:
        with self._lock:
            self._flush(session)

    def _flush(self, session: Session) -> None:
        if not session.directory:
            self._bases[session.session_id] = copy.deepcopy(session.to_dict())
            return

        manifest = Path(session.directory) / "session.json"
        local = session.to_dict()
        # Reject an unsafe object before using its directory to create a lock.
        _validate_manifest(local, self.masters_root, manifest)
        lock_path = manifest.parent / ".locks" / "session-manifest.lock"
        with ResourceLock(lock_path, timeout=30.0):
            if manifest.exists():
                latest_raw = _read_manifest(manifest)
                _validate_manifest(latest_raw, self.masters_root, manifest)
                latest_session = Session.from_dict(latest_raw)
                latest_session.directory = str(manifest.parent.resolve())
                latest = latest_session.to_dict()
            else:
                latest = None

            merged = self._merge_session(
                self._bases.get(session.session_id), local, latest)
            _validate_manifest(merged, self.masters_root, manifest)
            atomic_write_json(manifest, merged)
            self._synchronize_session(session, merged)
            self._bases[session.session_id] = copy.deepcopy(merged)

    @staticmethod
    def _merge_sequence(base: list[Any], local: list[Any],
                        latest: list[Any]) -> list[Any]:
        """Apply deliberate list additions/removals without losing peer entries."""
        def fingerprint(value: Any) -> str:
            return json.dumps(value, sort_keys=True, ensure_ascii=False,
                              separators=(",", ":"))

        base_counts: dict[str, int] = {}
        local_counts: dict[str, int] = {}
        for item in base:
            key = fingerprint(item)
            base_counts[key] = base_counts.get(key, 0) + 1
        for item in local:
            key = fingerprint(item)
            local_counts[key] = local_counts.get(key, 0) + 1

        removals = {
            key: count - local_counts.get(key, 0)
            for key, count in base_counts.items()
            if count > local_counts.get(key, 0)
        }
        result: list[Any] = []
        for item in latest:
            key = fingerprint(item)
            if removals.get(key, 0):
                removals[key] -= 1
            else:
                result.append(copy.deepcopy(item))

        additions = {
            key: count - base_counts.get(key, 0)
            for key, count in local_counts.items()
            if count > base_counts.get(key, 0)
        }
        for item in local:
            key = fingerprint(item)
            if additions.get(key, 0):
                result.append(copy.deepcopy(item))
                additions[key] -= 1
        return result

    @classmethod
    def _merge_session(
        cls,
        base: dict[str, Any] | None,
        local: dict[str, Any],
        latest: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """Apply fields changed since ``base`` onto the latest disk snapshot."""
        if latest is None:
            # A vanished manifest is not interpreted as a request to discard the
            # still-live object. Recreate it from the last base plus local edits.
            latest = copy.deepcopy(base if base is not None else local)
        merged = copy.deepcopy(latest)
        baseline = base or {}
        session_outcome_conflict = bool(
            base is not None
            and local.get("status") != baseline.get("status")
            and merged.get("status") != baseline.get("status")
            and merged.get("status") in (COMPLETE, FAILED, INTERRUPTED)
        )

        for name, value in local.items():
            if name in ("chunks", "ad_events"):
                continue
            if base is None or value != baseline.get(name):
                if session_outcome_conflict and name in ("status", "error"):
                    # A stale startup confirmation or crash reconciliation
                    # cannot replace the outcome a peer already committed.
                    continue
                if name == "ended_at" and base is not None:
                    value = max(float(value), float(merged.get(name, 0.0)))
                merged[name] = copy.deepcopy(value)

        base_events = list(baseline.get("ad_events", []))
        local_events = list(local.get("ad_events", []))
        if base is None and latest == local:
            merged["ad_events"] = copy.deepcopy(local_events)
        elif local_events != base_events:
            merged["ad_events"] = cls._merge_sequence(
                base_events, local_events, list(merged.get("ad_events", [])))

        chunk_fields = tuple(item.name for item in dataclass_fields(Chunk))

        def chunks_by_index(payload: dict[str, Any], where: str) -> dict[int, dict]:
            indexed: dict[int, dict] = {}
            for raw in payload.get("chunks", []):
                index = raw.get("index")
                if index in indexed:
                    raise ManifestValidationError(
                        f"duplicate chunk index {index!r} in {where}")
                indexed[index] = raw
            return indexed

        base_chunks = chunks_by_index(baseline, "base")
        local_chunks = chunks_by_index(local, "local state")
        latest_chunks = chunks_by_index(merged, "latest manifest")
        ordered = [raw["index"] for raw in merged.get("chunks", [])]

        for index, local_chunk in local_chunks.items():
            base_chunk = base_chunks.get(index)
            disk_chunk = latest_chunks.get(index)
            if disk_chunk is None:
                if base_chunk is None:
                    disk_chunk = copy.deepcopy(local_chunk)
                    latest_chunks[index] = disk_chunk
                    ordered.append(index)
                else:
                    # There is no chunk-deletion API. A chunk absent from the
                    # latest manifest is therefore left absent rather than being
                    # resurrected by an unrelated write from a stale object.
                    continue
            elif base_chunk is None:
                # add_chunk is get-or-create. If a peer won this index first,
                # adopt its object wholesale rather than regressing its outcomes.
                continue
            else:
                blocked: set[str] = set()
                outcome_groups = (
                    ("status", frozenset({COMPLETE, FAILED, INTERRUPTED}),
                     {"status", "master_error"}),
                    ("proxy_status", frozenset({DONE, ERROR, SKIPPED, EXPIRED}),
                     {"proxy_status", "proxy_error", "proxy_name"}),
                    ("transcript_status", frozenset({DONE, ERROR, SKIPPED}),
                     {"transcript_status", "transcript_error",
                      "transcribed_through", "word_count"}),
                    ("summary_status", frozenset({DONE, ERROR, SKIPPED}),
                     {"summary_status", "summary_error"}),
                )
                for status_name, terminal, group in outcome_groups:
                    if (local_chunk.get(status_name)
                            != base_chunk.get(status_name)
                            and disk_chunk.get(status_name)
                            != base_chunk.get(status_name)
                            and disk_chunk.get(status_name) in terminal):
                        blocked.update(group)
                for name in chunk_fields:
                    value = local_chunk.get(name)
                    if name in blocked:
                        continue
                    if value != base_chunk.get(name):
                        if name == "ended_at":
                            value = max(float(value),
                                        float(disk_chunk.get(name, 0.0)))
                        disk_chunk[name] = copy.deepcopy(value)
                # Derived fields are recomputed after field-level merging.
                merged_chunk = Chunk(**{
                    name: disk_chunk.get(name)
                    for name in chunk_fields
                }).to_dict()
                latest_chunks[index] = merged_chunk

        merged["chunks"] = [latest_chunks[index] for index in ordered
                            if index in latest_chunks]
        return merged

    @staticmethod
    def _synchronize_session(session: Session, payload: dict[str, Any]) -> None:
        """Refresh values in place so existing Session/Chunk references survive."""
        restored = Session.from_dict(payload)
        restored.directory = str(Path(restored.directory).resolve())
        for item in dataclass_fields(Session):
            if item.name != "chunks":
                setattr(session, item.name,
                        copy.deepcopy(getattr(restored, item.name)))

        existing = {chunk.index: chunk for chunk in session.chunks}
        synchronized: list[Chunk] = []
        for fresh in restored.chunks:
            current = existing.get(fresh.index)
            if current is None:
                current = fresh
            else:
                for item in dataclass_fields(Chunk):
                    setattr(current, item.name,
                            copy.deepcopy(getattr(fresh, item.name)))
            synchronized.append(current)
        session.chunks[:] = synchronized

    # -- recovery ----------------------------------------------------------

    def load_from_disk(self) -> None:
        """Repopulate from `session.json` files under the masters root.

        **Read-only.** This used to assume that any session still marked
        `recording` had lost its owner, and rewrote it to `interrupted` on disk
        during the load. That assumption is false whenever a second process
        exists: opening a CLI snapshot while the dashboard was recording
        relabelled the dashboard's live session, and if the second process then
        started, it could transcribe, remux and reclaim a `.ts` that the first
        was still appending to.

        Deciding a session has been abandoned requires knowing nobody owns its
        channel, and only the channel lock knows that. That decision now belongs
        to `Pipeline.recover()`, which takes the lock first. Loading merely
        reports what is on disk.
        """
        if not self.masters_root.exists():
            return
        self._load_quarantine_diagnostics()
        for path in sorted(self.masters_root.glob("*/*/session.json")):
            try:
                data = _read_manifest(path)
                _validate_manifest(data, self.masters_root, path)
                session = Session.from_dict(data)
                session.directory = str(path.parent.resolve())
                with self._lock:
                    existing = self._sessions.get(session.session_id)
                    if (existing is not None and
                            Path(existing.directory).resolve() == path.parent.resolve()):
                        continue
                    if existing is not None:
                        raise ManifestValidationError(
                            f"duplicate session_id {session.session_id!r}")
                    self._sessions[session.session_id] = session
                    self._bases[session.session_id] = copy.deepcopy(
                        session.to_dict())
            except Exception as exc:
                self._quarantine_manifest(path, exc)
                continue

    def diagnostics(self) -> list[dict[str, Any]]:
        """Manifest failures retained for logs, recovery tools, and the UI."""
        with self._lock:
            return [dict(item) for item in self._manifest_diagnostics]

    @property
    def manifest_diagnostics(self) -> list[dict[str, Any]]:
        return self.diagnostics()

    def _load_quarantine_diagnostics(self) -> None:
        for path in sorted(
                self.masters_root.glob("*/*/session.invalid-*.diagnostic.json")):
            key = str(path.resolve())
            with self._lock:
                if key in self._diagnostic_paths:
                    continue
            try:
                data = _read_manifest(path)
                item = _object(data, "quarantine diagnostic")
                for field_name in ("original_path", "quarantine_path", "error"):
                    _text(item.get(field_name), field_name, empty=False)
                _number(item.get("quarantined_at"), "quarantined_at")
            except Exception as exc:
                LOG.warning("could not read manifest diagnostic %s: %s", path, exc)
                continue
            item = dict(item)
            item["diagnostic_path"] = str(path)
            with self._lock:
                self._diagnostic_paths.add(key)
                self._manifest_diagnostics.append(item)

    def _quarantine_manifest(self, path: Path, error: Exception) -> None:
        reason = str(error) or error.__class__.__name__
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        quarantine = path.with_name(
            f"session.invalid-{stamp}-{secrets.token_hex(3)}.json")
        method = "failed"
        quarantine_path = ""
        try:
            path.replace(quarantine)
            method = "renamed"
            quarantine_path = str(quarantine)
        except OSError as rename_error:
            try:
                shutil.copy2(path, quarantine)
                method = "copied"
                quarantine_path = str(quarantine)
                reason += f"; source could not be renamed: {rename_error}"
            except OSError as copy_error:
                reason += (f"; quarantine failed (rename: {rename_error}; "
                           f"copy: {copy_error})")

        diagnostic: dict[str, Any] = {
            "original_path": str(path),
            "quarantine_path": quarantine_path,
            "error": reason,
            "quarantined_at": time.time(),
            "method": method,
        }
        diagnostic_path = ""
        if quarantine_path:
            target = quarantine.with_name(
                f"{quarantine.stem}.diagnostic.json")
            try:
                atomic_write_json(target, diagnostic)
                diagnostic_path = str(target)
            except OSError as exc:
                LOG.warning("could not persist manifest diagnostic %s: %s", target, exc)
        diagnostic["diagnostic_path"] = diagnostic_path
        with self._lock:
            if diagnostic_path:
                self._diagnostic_paths.add(str(Path(diagnostic_path).resolve()))
            self._manifest_diagnostics.append(diagnostic)
        LOG.error("quarantined invalid session manifest %s to %s: %s",
                  path, quarantine_path or "<quarantine failed>", reason)

    def reconcile_after_crash(self, session: Session) -> bool:
        """Relabel a session whose workers are gone. True if anything changed.

        Only ever called once the caller has established that no other process
        owns this session -- for a `recording` session that means holding its
        channel lock. Persisting the correction matters: without it every restart
        repeats the same in-memory relabel while disk keeps claiming the session
        is live.
        """
        with self._lock:
            repaired = False
            if session.status in (STARTING, RECORDING):
                startup = session.status == STARTING
                has_media = False
                for chunk in session.chunks:
                    if chunk.size_bytes > 0:
                        has_media = True
                        break
                    if not chunk.ts_name:
                        continue
                    try:
                        if ((session.path / "live" / chunk.ts_name).is_file()
                                and (session.path / "live" /
                                     chunk.ts_name).stat().st_size > 0):
                            has_media = True
                            break
                    except OSError:
                        continue
                session.status = (
                    FAILED if startup and not has_media else INTERRUPTED)
                if startup and not has_media and not session.error:
                    session.error = (
                        "recording startup was interrupted before any media arrived")
                if not session.ended_at:
                    manifest = session.path / "session.json"
                    try:
                        session.ended_at = manifest.stat().st_mtime
                    except OSError:
                        session.ended_at = time.time()
                repaired = True

            for chunk in session.chunks:
                if chunk.status in (STARTING, RECORDING):
                    if session.status == FAILED and chunk.status == STARTING:
                        chunk.status = FAILED
                        if not chunk.master_error:
                            chunk.master_error = "no media survived recording startup"
                    else:
                        chunk.status = INTERRUPTED
                    repaired = True
                # An artifact cannot still be running: its worker is gone.
                for field_name in ("proxy_status", "transcript_status",
                                   "summary_status"):
                    if getattr(chunk, field_name) == RUNNING:
                        setattr(chunk, field_name, PENDING)
                        repaired = True

            if repaired:
                self._flush(session)
            return repaired

    def forget(self, session_id: str) -> None:
        with self._lock:
            self._sessions.pop(session_id, None)
            self._bases.pop(session_id, None)


CONTROL_VERSION = 1
_REQUEST_ID = re.compile(r"[A-Za-z0-9_-]{16,128}")
_CONTROL_FIELDS = frozenset({
    "version", "requests", "results", "auto_suppressed",
})
_CONTROL_REQUEST_FIELDS = frozenset({
    "request_id", "channel", "requested_at", "attempt_session_id",
    "last_error",
})
_CONTROL_RESULT_FIELDS = frozenset({
    "request_id", "channel", "status", "session_id", "error",
    "completed_at",
})
_CONTROL_RESULT_STATES = frozenset({"complete", "error", "cancelled"})


def _control_request_id(value: Any, where: str) -> str:
    text = _text(value, where, empty=False)
    if not _REQUEST_ID.fullmatch(text):
        raise ManifestValidationError(f"{where} is not a valid request id")
    return text


def _validate_control_state(raw: Any) -> dict[str, Any]:
    data = _object(raw, "control state")
    _known_fields(data, _CONTROL_FIELDS, "control state")
    version = data.get("version")
    if version != CONTROL_VERSION:
        raise ManifestValidationError(
            f"unsupported control-state version {version!r}; "
            f"expected {CONTROL_VERSION}")

    request_ids: set[str] = set()
    request_channels: set[str] = set()
    for position, raw_request in enumerate(
            _list(data.get("requests", []), "requests")):
        where = f"requests[{position}]"
        request = _object(raw_request, where)
        _known_fields(request, _CONTROL_REQUEST_FIELDS, where)
        request_id = _control_request_id(request.get("request_id"),
                                         f"{where}.request_id")
        channel = _canonical_channel(request.get("channel"), f"{where}.channel")
        if request_id in request_ids:
            raise ManifestValidationError(f"duplicate request id {request_id!r}")
        if channel in request_channels:
            raise ManifestValidationError(
                f"more than one pending request for channel {channel!r}")
        request_ids.add(request_id)
        request_channels.add(channel)
        _number(request.get("requested_at"), f"{where}.requested_at")
        attempt = _text(request.get("attempt_session_id", ""),
                        f"{where}.attempt_session_id")
        if attempt:
            _safe_component(attempt, f"{where}.attempt_session_id")
        _text(request.get("last_error", ""), f"{where}.last_error")

    result_ids: set[str] = set()
    for position, raw_result in enumerate(_list(data.get("results", []), "results")):
        where = f"results[{position}]"
        result = _object(raw_result, where)
        _known_fields(result, _CONTROL_RESULT_FIELDS, where)
        request_id = _control_request_id(result.get("request_id"),
                                         f"{where}.request_id")
        if request_id in request_ids or request_id in result_ids:
            raise ManifestValidationError(f"duplicate request id {request_id!r}")
        result_ids.add(request_id)
        _canonical_channel(result.get("channel"), f"{where}.channel")
        _status(result.get("status"), f"{where}.status", _CONTROL_RESULT_STATES)
        session_id = _text(result.get("session_id", ""), f"{where}.session_id")
        if session_id:
            _safe_component(session_id, f"{where}.session_id")
        _text(result.get("error", ""), f"{where}.error")
        _number(result.get("completed_at"), f"{where}.completed_at")

    suppressed: set[str] = set()
    for position, value in enumerate(
            _list(data.get("auto_suppressed", []), "auto_suppressed")):
        channel = _canonical_channel(value, f"auto_suppressed[{position}]")
        if channel in suppressed:
            raise ManifestValidationError(
                f"duplicate auto-suppressed channel {channel!r}")
        suppressed.add(channel)
    return data


class ControlStateStore:
    """Atomic, kernel-locked recording intent outside session manifests."""

    def __init__(self, masters_root: Path) -> None:
        self.root = Path(masters_root).resolve()
        self.path = self.root / ".vodpipe-control.json"
        self.lock_path = self.root / ".locks" / "control-state.lock"
        self._lock = threading.RLock()
        self._base = self.empty()

    @staticmethod
    def empty() -> dict[str, Any]:
        return {
            "version": CONTROL_VERSION,
            "requests": [],
            "results": [],
            "auto_suppressed": [],
        }

    def load(self) -> dict[str, Any]:
        with self._lock, ResourceLock(self.lock_path, timeout=30.0):
            if not self.path.exists():
                self._base = self.empty()
                return copy.deepcopy(self._base)
            try:
                data = _read_manifest(self.path)
                validated = _validate_control_state(data)
            except Exception as exc:
                self._quarantine(exc)
                validated = self.empty()
            self._base = copy.deepcopy(validated)
            return copy.deepcopy(validated)

    def save(self, data: dict[str, Any]) -> dict[str, Any]:
        _validate_control_state(data)
        with self._lock, ResourceLock(self.lock_path, timeout=30.0):
            if self.path.exists():
                try:
                    latest = _validate_control_state(_read_manifest(self.path))
                except Exception as exc:
                    self._quarantine(exc)
                    latest = self.empty()
            else:
                latest = self.empty()

            merged = self._merge(self._base, data, latest)
            _validate_control_state(merged)
            atomic_write_json(self.path, merged)
            self._base = copy.deepcopy(merged)
            # Direct callers commonly retain and mutate their payload. Keep that
            # view aligned too, so a peer addition is not mistaken for a local
            # deletion on the next save.
            data.clear()
            data.update(copy.deepcopy(merged))
            return copy.deepcopy(merged)

    @staticmethod
    def _merge(base: dict[str, Any], local: dict[str, Any],
               latest: dict[str, Any]) -> dict[str, Any]:
        """Merge request generations and suppression deltas by durable identity."""
        def entities(payload: dict[str, Any]) -> dict[str, tuple[str, dict[str, Any]]]:
            result: dict[str, tuple[str, dict[str, Any]]] = {}
            for kind in ("requests", "results"):
                for item in payload.get(kind, []):
                    result[item["request_id"]] = (kind, item)
            return result

        base_entities = entities(base)
        local_entities = entities(local)
        latest_entities = entities(latest)
        for request_id in set(base_entities) | set(local_entities):
            local_value = local_entities.get(request_id)
            if local_value == base_entities.get(request_id):
                continue
            latest_entities.pop(request_id, None)
            if local_value is not None:
                latest_entities[request_id] = copy.deepcopy(local_value)

        requests = [copy.deepcopy(item) for kind, item in latest_entities.values()
                    if kind == "requests"]
        results = [copy.deepcopy(item) for kind, item in latest_entities.values()
                   if kind == "results"]
        requests.sort(key=lambda item: (item["requested_at"], item["request_id"]))
        results.sort(key=lambda item: (item["completed_at"], item["request_id"]))

        base_suppressed = set(base.get("auto_suppressed", []))
        local_suppressed = set(local.get("auto_suppressed", []))
        suppressed = set(latest.get("auto_suppressed", []))
        suppressed.update(local_suppressed - base_suppressed)
        suppressed.difference_update(base_suppressed - local_suppressed)
        return {
            "version": CONTROL_VERSION,
            "requests": requests,
            "results": results,
            "auto_suppressed": sorted(suppressed),
        }

    def _quarantine(self, error: Exception) -> None:
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        quarantine = self.path.with_name(
            f".vodpipe-control.invalid-{stamp}-{secrets.token_hex(3)}.json")
        try:
            self.path.replace(quarantine)
        except OSError as exc:
            LOG.error("invalid control state %s could not be quarantined: %s; %s",
                      self.path, error, exc)
            return
        LOG.error("quarantined invalid control state %s to %s: %s",
                  self.path, quarantine, error)


def new_session_id(channel: str, when: float | None = None) -> str:
    """A session id that cannot collide with one made in the same second.

    The timestamp alone has one-second resolution, so a stop-and-restart -- or two
    racing starts -- could produce the same id and therefore the same directory,
    segment list and state file.
    """
    stamp = time.strftime("%Y-%m-%d_%H%M%S", time.localtime(when or time.time()))
    return f"{channel.lower()}_{stamp}_{secrets.token_hex(3)}"


