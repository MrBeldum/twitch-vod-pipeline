"""Configuration schema and validation.

The dashboard can POST arbitrary JSON into the config, and the config controls
where files are written, how ffmpeg is invoked, and whether the disk guards work
at all. One bad request could previously persist `masters_root: null` or
`slice_seconds: 0` and stop the application from starting again.

Validation is therefore total and transactional: a prospective config is built and
checked in full, and only replaces the live one if every rule passes.
"""

from __future__ import annotations

import math
import re
from typing import Any, Callable
from urllib.parse import urlparse

from .channels import InvalidChannel, parse_channel
from .quality import LOW_QUALITY_POLICIES
from .util import safe_name_component


class ConfigError(ValueError):
    """A rejected configuration. Nothing has been changed."""


# -------------------------------------------------------------------- validators


def _number(low: float | None = None, high: float | None = None,
            integer: bool = False) -> Callable[[Any, str], Any]:
    def check(value: Any, path: str) -> Any:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ConfigError(f"{path} must be a number")
        if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
            raise ConfigError(f"{path} must be a finite number")
        if integer and float(value) != int(value):
            raise ConfigError(f"{path} must be a whole number")
        if low is not None and value < low:
            raise ConfigError(f"{path} must be at least {low}")
        if high is not None and value > high:
            raise ConfigError(f"{path} must be at most {high}")
        return int(value) if integer else float(value)
    return check


def _boolean(value: Any, path: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigError(f"{path} must be true or false")
    return value


def _reject_unusable_characters(value: str, path: str) -> str:
    """Refuse code points that break the things this config feeds.

    Two separate problems, both reproduced against the old validator:

    * **Lone surrogates** (U+D800-U+DFFF). JSON escapes accept them, so a posted
      `"\\ud800"` validated and was installed in live memory -- and then UTF-8
      persistence raised, leaving memory and disk disagreeing, with `/api/config`
      unable to serialise its own response afterwards.
    * **C0 controls.** Channel names and path components already rejected these,
      but plain text, paths and tool overrides did not, so a NUL could be stored
      and only fail later inside a path, a subprocess argument, or a filename.

    Escaping the JSON would not be enough: the eventual consumers are the
    filesystem and process creation, and neither accepts these.
    """
    for character in value:
        code = ord(character)
        if 0xD800 <= code <= 0xDFFF:
            raise ConfigError(
                f"{path} contains an unpaired surrogate (U+{code:04X}), which "
                f"cannot be written to disk")
        if code < 0x20 or code == 0x7F:
            raise ConfigError(
                f"{path} contains a control character (U+{code:04X})")
    return value


def _text(*, allow_empty: bool = True, max_length: int = 4096):
    def check(value: Any, path: str) -> str:
        if not isinstance(value, str):
            raise ConfigError(f"{path} must be text")
        if not allow_empty and not value.strip():
            raise ConfigError(f"{path} must not be empty")
        if len(value) > max_length:
            raise ConfigError(f"{path} is too long")
        return _reject_unusable_characters(value, path)
    return check


def _choice(*options: str):
    def check(value: Any, path: str) -> str:
        if value not in options:
            raise ConfigError(f"{path} must be one of: {', '.join(options)}")
        return value
    return check


def _component(value: Any, path: str) -> str:
    if not isinstance(value, str):
        raise ConfigError(f"{path} must be text")
    try:
        return safe_name_component(_reject_unusable_characters(value, path),
                                   what=path)
    except ValueError as exc:
        raise ConfigError(str(exc))


def _suffix(value: Any, path: str) -> str:
    if not isinstance(value, str) or len(value) > 32:
        raise ConfigError(f"{path} must be short text")
    if any(character in value for character in '\\/:*?"<>|'):
        raise ConfigError(f"{path} contains characters that are not allowed")
    return _reject_unusable_characters(value, path)


# A language subtag with optional region, e.g. "en", "en-gb", "pt-br". Checked
# here so a typo is rejected at the point of entry rather than reaching Premiere
# as a transcript header nothing can read.
_LANGUAGE_TAG = re.compile(r"^[a-z]{2,3}(-[a-z0-9]{2,8})*$", re.IGNORECASE)


def _language(value: Any, path: str) -> str:
    if not isinstance(value, str):
        raise ConfigError(f"{path} must be text")
    tag = value.strip().replace("_", "-")
    if not _LANGUAGE_TAG.match(tag):
        raise ConfigError(
            f"{path} must be a language tag like 'en', 'en-gb' or 'pt-br'")
    return tag.lower()


def _audio_stream(value: Any, path: str) -> str | int:
    if isinstance(value, bool):
        raise ConfigError(
            f"{path} must be 'auto', a zero-based ordinal, or a language tag")
    if isinstance(value, int):
        if value < 0:
            raise ConfigError(f"{path} ordinal must be zero or greater")
        return value
    if not isinstance(value, str):
        raise ConfigError(
            f"{path} must be 'auto', a zero-based ordinal, or a language tag")
    selector = value.strip().replace("_", "-")
    if selector.lower() == "auto":
        return "auto"
    if selector.isdecimal():
        return int(selector)
    return _language(selector, path)


# Proxy schemes streamlink understands via its requests transport. socks5h keeps
# DNS resolution on the proxy side, which is what a geo-block workaround usually
# wants.
_PROXY_SCHEMES = frozenset({
    "http", "https", "socks4", "socks4a", "socks5", "socks5h"})


def _proxy_url(value: Any, path: str) -> str:
    """A streamlink proxy URL, or empty for direct. Rejected early rather than
    reaching streamlink as an argument that silently disables the proxy."""
    if not isinstance(value, str):
        raise ConfigError(f"{path} must be text")
    text = _reject_unusable_characters(value.strip(), path)
    if not text:
        return ""
    if len(text) > 512:
        raise ConfigError(f"{path} is too long")
    try:
        parsed = urlparse(text)
    except ValueError as exc:
        raise ConfigError(f"{path} is not a valid proxy URL") from exc
    if parsed.scheme.lower() not in _PROXY_SCHEMES:
        raise ConfigError(
            f"{path} must start with one of: "
            f"{', '.join(sorted(_PROXY_SCHEMES))}:// (got {text!r})")
    try:
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        # urllib.parse raises for non-numeric and out-of-range ports. Keep the
        # schema contract by translating that into the ConfigError callers expect.
        raise ConfigError(f"{path} has an invalid port") from exc
    if not hostname:
        raise ConfigError(f"{path} must include a host, e.g. socks5://127.0.0.1:1080")
    if port is not None and not (0 < port <= 65535):
        raise ConfigError(f"{path} has an invalid port")
    return text


def _channel_list(value: Any, path: str) -> list[str]:
    if not isinstance(value, list):
        raise ConfigError(f"{path} must be a list")
    channels = []
    for item in value:
        if not isinstance(item, str):
            raise ConfigError(f"{path} must contain only channel names as text")
        try:
            channels.append(parse_channel(item))
        except InvalidChannel as exc:
            raise ConfigError(f"{path}: {exc}")
    return sorted(set(channels))


def _string_list(value: Any, path: str) -> list[str]:
    if not isinstance(value, list):
        raise ConfigError(f"{path} must be a list")
    if not all(isinstance(item, str) for item in value):
        raise ConfigError(f"{path} must be a list of text")
    return [_reject_unusable_characters(item, f"{path}[{index}]")
            for index, item in enumerate(value)]


# ------------------------------------------------------------------------ schema

SCHEMA: dict[str, Callable[[Any, str], Any]] = {
    "paths.masters_root": _text(allow_empty=False),
    "paths.work_root": _text(allow_empty=False),
    "paths.censor_master_list": _text(),

    "recording.chunk_seconds": _number(60, 86400, integer=True),
    "recording.quality": _text(allow_empty=False, max_length=64),
    "recording.free_space_floor_gb": _number(0, 100000),
    "recording.hard_reserve_gb": _number(0, 100000),
    "recording.twitch_low_latency": _boolean,
    "recording.keep_ts_after_remux": _boolean,
    "recording.streamlink_no_config": _boolean,
    "recording.ffmpeg_grace_seconds": _number(5, 3600),
    "recording.startup_timeout_seconds": _number(0, 3600),
    "recording.min_height": _number(0, 4320, integer=True),
    "recording.on_low_quality": _choice(*LOW_QUALITY_POLICIES),

    "proxies.enabled": _boolean,
    "proxies.height": _number(64, 2160, integer=True),
    "proxies.encoder": _choice("auto", "h264_amf", "h264_nvenc", "h264_qsv",
                               "libx264"),
    "proxies.quality": _number(0, 51, integer=True),
    "proxies.audio_bitrate": _text(allow_empty=False, max_length=16),
    "proxies.retention_days": _number(0, 3650),
    "proxies.suffix": _suffix,
    "proxies.folder_name": _component,

    "transcription.enabled": _boolean,
    "transcription.provider": _choice("deepgram"),
    "transcription.model": _text(allow_empty=False, max_length=64),
    "transcription.language": _language,
    "transcription.audio_stream": _audio_stream,
    "transcription.filler_words": _boolean,
    "transcription.slice_seconds": _number(5, 3600),
    "transcription.min_slice_seconds": _number(1, 3600),
    "transcription.overlap_seconds": _number(0, 120),
    "transcription.live_margin_seconds": _number(0, 600),
    "transcription.max_retries": _number(1, 20, integer=True),
    "transcription.request_timeout_seconds": _number(10, 7200),
    "transcription.stitch_chunk_boundaries": _boolean,
    "transcription.seam_seconds": _number(1, 120),

    "snapshots.max_concurrent": _number(1, 16, integer=True),
    "snapshots.max_per_session": _number(1, 16, integer=True),

    "summary.enabled": _boolean,
    "summary.provider": _choice("claude-cli", "anthropic-api", "none"),
    "summary.model": _text(allow_empty=False, max_length=64),
    "summary.timeout_seconds": _number(10, 7200),
    "summary.max_tokens": _number(256, 200000, integer=True),
    "summary.max_retries": _number(1, 10, integer=True),
    "summary.min_words": _number(0, 100000, integer=True),

    "ads.log_events": _boolean,
    "ads.event_patterns": _string_list,

    "dashboard.host": _text(allow_empty=False, max_length=64),
    "dashboard.port": _number(1, 65535, integer=True),
    "dashboard.open_browser": _boolean,
    "dashboard.poll_seconds": _number(1, 3600),

    "watcher.enabled": _boolean,
    "watcher.check_seconds": _number(10, 86400),
    "watcher.probe_timeout_seconds": _number(5, 600),

    "network.proxy": _proxy_url,

    "channels": _channel_list,

    "secrets.deepgram_api_key": _text(max_length=512),
    "secrets.twitch_oauth_token": _text(max_length=512),
    "secrets.anthropic_api_key": _text(max_length=512),

    "tools.ffmpeg": _text(),
    "tools.ffprobe": _text(),
    "tools.streamlink": _text(),
    "tools.claude": _text(),

    # Per-channel overrides are keyed by channel name; handled separately.
    "channel_settings": lambda value, path: _channel_settings(value, path),
}


# Settings a previous version accepted and this one does not. A config file
# written by that version is still a usable config, so these are dropped on load
# rather than rejected, and the next save writes the file without them.
#
# The alternative -- letting `_walk` report them as unknown -- would leave an
# already-installed application unable to start after an upgrade, which is the
# exact failure this module exists to prevent. Anything removed from SCHEMA
# belongs here.
RETIRED_PATHS = frozenset({
    # Automatic filler tagging, removed 2026-08-17. Premiere reported "no filler
    # words detected" against the tags it was given, and bulk deletion at word
    # boundaries was never going to produce cuts an editor would keep.
    # `transcription.filler_words` survives: it controls whether fillers are
    # transcribed at all, which is transcript fidelity.
    "transcription.filler_tagging",
    "transcription.filler_extra_words",
    "transcription.filler_review",
    "transcription.filler_review_provider",
    "transcription.filler_review_timeout_seconds",
    "transcription.filler_review_max_candidates",
    "transcription.filler_review_min_confidence",
})


def _channel_settings(value: Any, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError(f"{path} must be an object")
    cleaned: dict[str, Any] = {}
    for name, settings in value.items():
        if not isinstance(name, str):
            raise ConfigError(f"{path} keys must be channel names as text")
        try:
            channel = parse_channel(name)
        except InvalidChannel as exc:
            raise ConfigError(f"{path}: {exc}")
        if not isinstance(settings, dict):
            raise ConfigError(f"{path}.{channel} must be an object")
        entry = {}
        for key, item in settings.items():
            if key != "auto_record":
                raise ConfigError(f"{path}.{channel}.{key} is not a known setting")
            entry[key] = _boolean(item, f"{path}.{channel}.{key}")
        cleaned[channel] = entry
    return cleaned


# --------------------------------------------------------------- cross-field rules


def _cross_field(data: dict[str, Any]) -> None:
    def get(path: str, default: Any = None) -> Any:
        node: Any = data
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    overlap = get("transcription.overlap_seconds", 0)
    slice_seconds = get("transcription.slice_seconds", 1)
    if overlap >= slice_seconds:
        raise ConfigError(
            "transcription.overlap_seconds must be smaller than slice_seconds, "
            "or a slice would never move forward")

    minimum = get("transcription.min_slice_seconds", 1)
    if minimum > slice_seconds:
        raise ConfigError(
            "transcription.min_slice_seconds cannot exceed slice_seconds")

    reserve = get("recording.hard_reserve_gb", 0)
    floor = get("recording.free_space_floor_gb", 0)
    if reserve > floor:
        raise ConfigError(
            "recording.hard_reserve_gb must not exceed free_space_floor_gb: the "
            "reserve is the emergency stop below the floor")

    if get("snapshots.max_per_session", 1) > get("snapshots.max_concurrent", 1):
        raise ConfigError(
            "snapshots.max_per_session cannot exceed snapshots.max_concurrent")

    if get("proxies.height", 2) % 2 != 0:
        raise ConfigError("proxies.height must be even (H.264 requires it)")

    host = get("dashboard.host", "127.0.0.1")
    if host not in ("127.0.0.1", "localhost"):
        raise ConfigError(
            f"dashboard.host {host!r} is not a loopback address. The dashboard "
            "has no authentication, so binding it anywhere reachable would let "
            "any client on the network control recording and read secrets.")


# ------------------------------------------------------------------------- entry


def validate(data: dict[str, Any]) -> dict[str, Any]:
    """Validate a complete config. Returns the cleaned copy, or raises ConfigError."""
    if not isinstance(data, dict):
        raise ConfigError("configuration must be an object")

    cleaned = _walk(data, "")
    _cross_field(cleaned)
    return cleaned


def _walk(node: dict[str, Any], prefix: str) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in node.items():
        path = f"{prefix}.{key}" if prefix else key
        if path.startswith("_"):
            continue
        if path in RETIRED_PATHS:
            continue
        validator = SCHEMA.get(path)
        if validator is not None:
            result[key] = validator(value, path)
            continue
        if isinstance(value, dict) and any(
                name.startswith(f"{path}.") for name in SCHEMA):
            result[key] = _walk(value, path)
            continue
        raise ConfigError(f"{path} is not a known setting")
    return result


def known_paths() -> list[str]:
    return sorted(SCHEMA)
