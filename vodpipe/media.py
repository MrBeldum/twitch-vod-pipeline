"""Every ffmpeg invocation the pipeline makes.

Keeping them in one file means the timestamp conventions stay consistent: chunk
time 0 is the first presentation timestamp of the recorded MPEG-TS, and both the
MP4 remux and every seek we perform are normalised against it.
"""

from __future__ import annotations

import shutil
import threading
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from .util import (
    LOG,
    Tools,
    ffprobe_json,
    media_duration,
    run,
)

_ENCODER_LOCK = threading.Lock()
_ENCODER_CACHE: dict[str, str] = {}


def probe_encoder(tools: Tools, preference: str = "auto") -> str:
    """Pick a proxy encoder, verifying the GPU actually accepts it.

    `-encoders` listing a codec only proves the build supports it; on a machine
    with no matching GPU h264_amf lists fine and then fails at runtime. So we
    encode two seconds of test pattern before trusting it.
    """
    if preference and preference != "auto":
        return preference

    with _ENCODER_LOCK:
        cached = _ENCODER_CACHE.get("proxy")
        if cached:
            return cached

        chosen = "libx264"
        for candidate in ("h264_amf", "h264_nvenc", "h264_qsv"):
            probe = run(
                [tools.ffmpeg, "-hide_banner", "-loglevel", "error",
                 "-f", "lavfi", "-i", "testsrc=size=640x360:rate=30:duration=2",
                 "-c:v", candidate, "-f", "null", "-"],
                timeout=90,
            )
            if probe.returncode == 0:
                chosen = candidate
                break
        LOG.info("proxy encoder: %s", chosen)
        _ENCODER_CACHE["proxy"] = chosen
        return chosen


# ---------------------------------------------------------------------- recording


def segment_command(
    tools: Tools,
    output_pattern: Path,
    segment_list: Path,
    chunk_seconds: int,
) -> list[str]:
    """ffmpeg args for stream-copying stdin into keyframe-aligned .ts segments.

    The segment muxer can only cut a copied stream at a keyframe, so it rolls
    forward to the next one rather than splitting mid-GOP -- which is precisely
    the boundary guarantee the masters need. MPEG-TS rather than MP4 because an
    MP4 has no index until it is closed, and we need to read, slice and snapshot
    the chunk that is still being written.

    **Video and audio only, never `-map 0`.** *CORRECTED 2026-08-16.* Twitch's
    HLS carries a `data:timed_id3` metadata stream, and `-map 0` copied it into
    every recording. When the network stalls, streamlink logs a
    ``Sequence gap of N segments ... will result in incoherent output data`` and
    the timestamps jump. The mpegts demuxer corrects the audio for that (it logs
    ``timestamp discontinuity ... new offset=``) but the id3 stream gets no such
    correction, so its DTS goes backwards relative to everything else, the
    segment muxer refuses it --
    ``Application provided invalid, non monotonically increasing dts to muxer in
    stream 2`` -- and ffmpeg aborts with -22, killing the recording. A live
    zy0xxx capture died this way three times in fifteen minutes.

    The stream was never wanted: `plan_remux_maps` already drops it because MP4
    cannot carry it and it holds nothing editorial, and settled decision (the ad
    correction of 2026-08-13) rules out reading Twitch metadata for ad ranges.
    Capturing a stream we always discard, at the cost of the whole recording when
    the network hiccups, was a pure loss. `?` on each selector keeps a stream-less
    edge case from failing the map outright.

    `+discardcorrupt` covers the other half of the same event: the log shows
    ``Packet corrupt (stream = 1)`` immediately before the abort. Dropping a
    corrupt packet loses a frame; refusing it loses the broadcast.
    """
    return [
        tools.ffmpeg,
        "-hide_banner",
        "-loglevel", "warning",
        "-nostdin",
        "-fflags", "+genpts+discardcorrupt",
        "-i", "pipe:0",
        "-c", "copy",
        "-map", "0:v?",
        "-map", "0:a?",
        "-f", "segment",
        "-segment_time", str(int(chunk_seconds)),
        "-segment_format", "mpegts",
        "-segment_list", str(segment_list),
        "-segment_list_type", "csv",
        "-reset_timestamps", "1",
        str(output_pattern),
    ]


def proxy_args(proxy: str) -> list[str]:
    """streamlink `--http-proxy` arguments, or nothing when no proxy is set.

    streamlink 8.4's `--http-proxy` covers *all* HTTP and HTTPS requests
    (including the WebSocket the Twitch plugin uses), so one flag routes both the
    stream fetch and the metadata/live-status calls. This is the lighter-weight
    alternative to a system VPN for reaching Twitch from a region it withholds the
    source rendition in -- notably South Korea, which it left in February 2024.
    """
    proxy = (proxy or "").strip()
    return ["--http-proxy", proxy] if proxy else []


def oauth_args(oauth_token: str) -> list[str]:
    """`--twitch-api-header` arguments carrying an OAuth token, or nothing.

    Turbo covers every channel, which is what makes this workable when the channel
    list is arbitrary. streamlink 8.4 removed `--twitch-disable-ads`, so the token
    is the only first-class ad avoidance left.
    """
    token = (oauth_token or "").strip()
    if not token:
        return []
    if token.lower().startswith("oauth:"):
        token = token[len("oauth:"):]
    return ["--twitch-api-header", f"Authorization=OAuth {token}"]


def format_hls_time(seconds: float) -> str:
    """Render seconds as the `HH:MM:SS.mmm` streamlink accepts for its HLS offsets."""
    seconds = max(0.0, float(seconds))
    hours, rest = divmod(seconds, 3600.0)
    minutes, secs = divmod(rest, 60.0)
    return f"{int(hours):02d}:{int(minutes):02d}:{secs:06.3f}"


def streamlink_command(
    tools: Tools,
    url: str,
    quality: str,
    *,
    oauth_token: str = "",
    low_latency: bool = False,
    no_config: bool = False,
    proxy: str = "",
) -> list[str]:
    cmd = [
        tools.streamlink,
        "--stdout",
        "--loglevel", "debug",
        "--hls-live-restart",
        "--retry-streams", "5",
        "--retry-max", "0",
        "--retry-open", "5",
        "--stream-timeout", "120",
        "--ringbuffer-size", "64M",
    ]
    if no_config:
        # Opt-in, not the default. A user's own streamlink config is a legitimate
        # place to keep a token or a plugin setting, and ignoring it silently
        # would change how recordings are made without saying so.
        cmd.append("--no-config")
    cmd += proxy_args(proxy)
    if low_latency:
        cmd.append("--twitch-low-latency")
    cmd += oauth_args(oauth_token)
    cmd += [url, quality]
    return cmd


def vod_download_command(
    tools: Tools,
    url: str,
    quality: str,
    *,
    oauth_token: str = "",
    no_config: bool = False,
    proxy: str = "",
    start_offset: float | None = None,
    duration: float | None = None,
) -> list[str]:
    """streamlink args for downloading a Twitch VOD to stdout.

    The same stdout -> ffmpeg-segmenter path as live capture, so a VOD produces
    keyframe-aligned chunks, masters, proxies, rolling transcripts and rundowns
    identical to a live recording. The differences from `streamlink_command` are
    all because a VOD is finite and static:

    * No `--hls-live-restart` -- there is no live edge to jump to.
    * No `--retry-max 0`. Retrying forever is right for a live broadcast that may
      drop and come back; a VOD that stops downloading should fail, not spin.
    * No low-latency option -- that is a live-edge tuning with no VOD meaning.
    * `--hls-start-offset` / `--hls-duration` optionally fetch only a sub-range,
      which matters for multi-hour VODs.
    """
    cmd = [
        tools.streamlink,
        "--stdout",
        "--loglevel", "debug",
        "--retry-streams", "3",
        "--retry-open", "3",
        "--stream-timeout", "120",
        "--ringbuffer-size", "64M",
    ]
    if no_config:
        cmd.append("--no-config")
    cmd += proxy_args(proxy)
    cmd += oauth_args(oauth_token)
    if start_offset is not None and start_offset > 0:
        cmd += ["--hls-start-offset", format_hls_time(start_offset)]
    if duration is not None and duration > 0:
        cmd += ["--hls-duration", format_hls_time(duration)]
    cmd += [url, quality]
    return cmd


# Settled decision #2 is a stream-copy H.264 master. Anything else is refused
# rather than silently re-encoded, because a re-encoded master is a different
# product decision, not an implementation detail.
MASTER_VIDEO_CODEC = "h264"

# Audio codecs MP4 can carry and Premiere reads without complaint.
MP4_AUDIO_CODECS = {"aac", "mp3", "ac3", "eac3", "alac"}


@dataclass(frozen=True)
class MediaTopology:
    """Stable identity of the media streams an output is required to carry."""

    video: tuple[tuple[str, int, int], ...]
    audio: tuple[tuple[str, str, int, str], ...]


def _stream_index(stream: dict[str, Any]) -> int:
    value = stream.get("index")
    if isinstance(value, bool):
        raise RuntimeError("stream metadata has an invalid index")
    try:
        index = int(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("stream metadata has an invalid index") from exc
    if index < 0:
        raise RuntimeError("stream metadata has an invalid index")
    return index


def _selected_streams(streams: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """The one programme video and every audio stream mapped by our outputs."""
    if not isinstance(streams, (list, tuple)) or not all(
            isinstance(stream, dict) for stream in streams):
        raise RuntimeError("stream metadata is unreadable")
    media = [stream for stream in streams
             if stream.get("codec_type") in {"video", "audio"}]
    indexes = [_stream_index(stream) for stream in media]
    if len(indexes) != len(set(indexes)):
        raise RuntimeError("stream metadata has ambiguous indices")
    video = [stream for stream in media if stream.get("codec_type") == "video"]
    if not video:
        raise RuntimeError("media has no readable video stream")
    audio = [stream for stream in media if stream.get("codec_type") == "audio"]
    return [video[0], *audio]


def _stream_language(stream: dict[str, Any]) -> str:
    tags = stream.get("tags")
    if tags is not None and not isinstance(tags, dict):
        raise RuntimeError("stream metadata has invalid tags")
    value = str((tags or {}).get("language") or "").strip().lower().replace("_", "-")
    return "und" if value in ("", "und") else value


def _channel_identity(stream: dict[str, Any]) -> tuple[int, str]:
    value = stream.get("channels")
    if isinstance(value, bool):
        raise RuntimeError("audio stream metadata has an invalid channel count")
    try:
        channels = int(value or 0)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("audio stream metadata has an invalid channel count") from exc
    if channels < 0:
        raise RuntimeError("audio stream metadata has an invalid channel count")
    layout = str(stream.get("channel_layout") or "").strip().lower()
    return channels, layout or (f"{channels}ch" if channels else "unknown")


def _topology(
    streams: Sequence[dict[str, Any]],
    *,
    selected: bool,
    video_codec: str | None = None,
    audio_codec: str | None = None,
    dimensions: bool = True,
) -> MediaTopology:
    candidates = (_selected_streams(streams) if selected else
                  [stream for stream in streams
                   if isinstance(stream, dict)
                   and stream.get("codec_type") in {"video", "audio"}])
    videos: list[tuple[str, int, int]] = []
    audio: list[tuple[str, str, int, str]] = []
    for stream in candidates:
        kind = stream.get("codec_type")
        codec = str(stream.get("codec_name") or "").strip().lower()
        codec = (video_codec if kind == "video" else audio_codec) or codec
        if not codec:
            raise RuntimeError(f"{kind or 'media'} stream has no readable codec")
        if kind == "video":
            try:
                width = int(stream.get("width") or 0) if dimensions else 0
                height = int(stream.get("height") or 0) if dimensions else 0
            except (TypeError, ValueError) as exc:
                raise RuntimeError("video stream has invalid dimensions") from exc
            if dimensions and (width <= 0 or height <= 0):
                raise RuntimeError("video stream has no readable dimensions")
            videos.append((codec, width, height))
        elif kind == "audio":
            channels, layout = _channel_identity(stream)
            audio.append((codec, _stream_language(stream), channels, layout))
    return MediaTopology(tuple(videos), tuple(audio))


def _required_topology(
    streams: Sequence[dict[str, Any]],
    *,
    video_codec: str | None = None,
    audio_codec: str | None = None,
    dimensions: bool = True,
) -> MediaTopology:
    return _topology(streams, selected=True, video_codec=video_codec,
                     audio_codec=audio_codec, dimensions=dimensions)


def _assert_topology(probe: dict[str, Any], expected: MediaTopology,
                     label: str, *, dimensions: bool = True) -> None:
    streams = probe.get("streams") if isinstance(probe, dict) else None
    if not isinstance(streams, list):
        raise RuntimeError(f"{label} stream metadata is unreadable")
    if not dimensions:
        for stream in streams:
            if not isinstance(stream, dict) or stream.get("codec_type") != "video":
                continue
            try:
                width = int(stream.get("width") or 0)
                height = int(stream.get("height") or 0)
            except (TypeError, ValueError):
                width = height = 0
            if width <= 0 or height <= 0:
                raise RuntimeError(
                    f"{label} video stream has no readable dimensions")
    actual = _topology(streams, selected=False, dimensions=dimensions)
    if len(actual.video) != len(expected.video):
        raise RuntimeError(
            f"{label} has {len(actual.video)} video stream(s), expected "
            f"{len(expected.video)}")
    if len(actual.audio) != len(expected.audio):
        raise RuntimeError(
            f"{label} has {len(actual.audio)} audio stream(s), expected "
            f"{len(expected.audio)}; refusing to lose a source audio track")
    if actual.video != expected.video:
        raise RuntimeError(
            f"{label} video topology changed: expected {expected.video!r}, "
            f"got {actual.video!r}")
    if actual.audio != expected.audio:
        raise RuntimeError(
            f"{label} audio codec/language/layout topology changed: expected "
            f"{expected.audio!r}, got {actual.audio!r}")


def _probe_output_topology(tools: Tools, path: Path, expected: MediaTopology,
                           label: str, *, dimensions: bool = True,
                           expected_duration: float = 0.0,
                           shortfall_allowance: float | None = None) -> dict[str, Any]:
    if not path.exists() or path.stat().st_size <= 0:
        raise RuntimeError(f"{label} is missing or empty")
    probe = ffprobe_json(tools.ffprobe, path)
    _assert_topology(probe, expected, label, dimensions=dimensions)
    _assert_stream_coverage(
        probe, expected_duration, label,
        shortfall_allowance=shortfall_allowance)
    return probe


def _map_selected_streams(streams: Sequence[dict[str, Any]]) -> list[str]:
    maps: list[str] = []
    for stream in _selected_streams(streams):
        maps += ["-map", f"0:{_stream_index(stream)}"]
    return maps


def _stream_attribute_args(streams: Sequence[dict[str, Any]]) -> list[str]:
    """Preserve language and dispositions instead of letting muxers invent them."""
    audio = [stream for stream in _selected_streams(streams)
             if stream.get("codec_type") == "audio"]
    args: list[str] = []
    for ordinal, stream in enumerate(audio):
        disposition = stream.get("disposition")
        if disposition is not None and not isinstance(disposition, dict):
            raise RuntimeError("audio stream metadata has an invalid disposition")
        enabled = [str(name) for name, value in (disposition or {}).items()
                   if value in (1, True)]
        args += [f"-disposition:a:{ordinal}", "+".join(enabled) or "0"]
        language = _stream_language(stream)
        if language != "und":
            args += [f"-metadata:s:a:{ordinal}", f"language={language}"]
    return args


def _stream_frame_rate(stream: dict[str, Any]) -> float:
    value = stream.get("avg_frame_rate") or stream.get("r_frame_rate")
    if isinstance(value, str) and "/" in value:
        numerator, _, denominator = value.partition("/")
        try:
            rate = float(numerator) / float(denominator)
        except (TypeError, ValueError, ZeroDivisionError):
            return 0.0
    else:
        try:
            rate = float(value)
        except (TypeError, ValueError):
            return 0.0
    return rate if math.isfinite(rate) and rate > 0 else 0.0


def plan_remux_maps(streams: list[dict[str, Any]]) -> tuple[list[str], list[str]]:
    """Decide which streams enter the master. Returns (ffmpeg -map args, dropped).

    ffmpeg's automatic stream selection keeps only the "best" audio track, which
    silently discards a second commentary or language track. Mapping is explicit
    so what survives is a decision rather than a default.
    """
    maps: list[str] = []
    dropped: list[str] = []

    video = [s for s in streams if s.get("codec_type") == "video"]
    if not video:
        raise RuntimeError("recording has no video stream")
    primary = video[0]
    codec = (primary.get("codec_name") or "?").lower()
    if codec != MASTER_VIDEO_CODEC:
        raise RuntimeError(
            f"stream is {codec}, not {MASTER_VIDEO_CODEC}; refusing to publish a "
            "master that is not a stream copy (the .ts has been kept)"
        )
    maps += ["-map", f"0:{primary['index']}"]
    for extra in video[1:]:
        dropped.append(f"video:{extra.get('codec_name')}")

    # Every audio stream is carried or nothing is published. Dropping one and
    # continuing was worse than it looked: the caller deletes the .ts as soon as
    # the master validates, so a commentary or second-language track went away
    # permanently and only a log line said so. Refusing keeps the .ts, which
    # makes the failure recoverable -- and re-encoding to AAC instead would be a
    # different product decision from settled decision #2, not a bug fix.
    for stream in streams:
        if stream.get("codec_type") != "audio":
            continue
        name = (stream.get("codec_name") or "?").lower()
        if name not in MP4_AUDIO_CODECS:
            raise RuntimeError(
                f"audio stream {stream.get('index')} is {name}, which MP4 cannot "
                f"carry by stream copy; refusing to publish a master without it "
                f"(the .ts has been kept)"
            )
        maps += ["-map", f"0:{stream['index']}"]

    # Data, subtitle and timed-metadata streams are deliberately excluded: MP4
    # rejects several of them outright and none carry editorial value here.
    for stream in streams:
        if stream.get("codec_type") in ("subtitle", "data", "attachment"):
            dropped.append(f"{stream.get('codec_type')}:{stream.get('codec_name')}")

    return maps, dropped


# Streams every Twitch HLS recording carries and no master ever wants. Dropping
# one is the expected outcome, not an anomaly, so it is reported at INFO --
# warning about it on every chunk of every recording is how a log stops being
# read. Anything else dropped is still a warning.
ROUTINE_DROPPED_STREAMS = frozenset({"data:timed_id3", "data:bin_data"})


# A file shorter than the requested output is the dangerous direction. The
# allowance is one tenth of one percent, never less than one low-frame-rate frame
# and never more than two seconds on a long recording. A fixed half second was
# enough to accept only 0.1s of a 0.6s source.
SHORTFALL_FRACTION = 0.001
MIN_SHORTFALL_SECONDS = 1.0 / 15.0
MAX_SHORTFALL_SECONDS = 2.0


def allowed_shortfall(expected_duration: float,
                      tolerance: float = MAX_SHORTFALL_SECONDS) -> float:
    """How much shorter than expected an output may be and still be accepted."""
    cap = max(0.0, min(MAX_SHORTFALL_SECONDS, float(tolerance)))
    if expected_duration <= 0:
        return cap
    return min(cap, max(MIN_SHORTFALL_SECONDS,
                        expected_duration * SHORTFALL_FRACTION))


def _probe_number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _stream_duration(stream: dict[str, Any]) -> float | None:
    """Return elapsed stream duration from ffprobe summary metadata."""
    duration = _probe_number(stream.get("duration"))
    if duration is None:
        duration_ts = _probe_number(stream.get("duration_ts"))
        time_base = stream.get("time_base")
        if duration_ts is not None and isinstance(time_base, str):
            numerator, separator, denominator = time_base.partition("/")
            num = _probe_number(numerator)
            den = _probe_number(denominator)
            if separator and num is not None and den not in (None, 0.0):
                duration = duration_ts * num / den
    if duration is None or duration <= 0.0:
        return None
    # MPEG-TS commonly starts around PTS 1.4s. Coverage is elapsed duration, not
    # the final timestamp; adding start_time lets a short nonzero-PTS stream pass.
    return duration


def _probe_duration(probe: dict[str, Any]) -> float:
    """Best elapsed duration represented by one already-collected probe."""
    if isinstance(probe, dict):
        format_data = probe.get("format")
        if isinstance(format_data, dict):
            duration = _probe_number(format_data.get("duration"))
            if duration is not None and duration > 0:
                return duration
    durations = [
        duration for stream in (probe.get("streams") or [])
        if isinstance(stream, dict)
        and stream.get("codec_type") in {"video", "audio"}
        and (duration := _stream_duration(stream)) is not None
    ] if isinstance(probe, dict) else []
    return max(durations, default=0.0)


def _assert_stream_coverage(probe: dict[str, Any], expected_duration: float,
                            label: str,
                            tolerance: float = MAX_SHORTFALL_SECONDS, *,
                            shortfall_allowance: float | None = None) -> float:
    """Require every mapped video/audio stream to cover the output interval."""
    streams = probe.get("streams") if isinstance(probe, dict) else None
    if not isinstance(streams, list):
        raise RuntimeError(f"{label} stream metadata is unreadable")
    timed: list[tuple[str, float]] = []
    for stream in streams:
        if not isinstance(stream, dict) or stream.get("codec_type") not in {
                "video", "audio"}:
            continue
        duration = _stream_duration(stream)
        stream_label = (
            f"{stream.get('codec_type')} stream {stream.get('index', '?')}")
        if duration is None:
            raise RuntimeError(f"{label} {stream_label} reports no usable duration")
        timed.append((stream_label, duration))
    if not timed:
        raise RuntimeError(f"{label} reports no stream duration")

    if expected_duration > 0:
        limit = (allowed_shortfall(expected_duration, tolerance)
                 if shortfall_allowance is None else
                 max(0.0, float(shortfall_allowance)))
        for stream_label, duration in timed:
            shortfall = expected_duration - duration
            if shortfall - limit > 1e-9:
                raise RuntimeError(
                    f"{label} {stream_label} covers {duration:.3f}s but the "
                    f"expected output is {expected_duration:.3f}s -- short by "
                    f"{shortfall:.3f}s, more than the {limit:.3f}s allowed")
    return min(duration for _, duration in timed)


def validate_media_coverage(tools: Tools, path: Path,
                            expected_duration: float = 0.0, *,
                            label: str = "media") -> float:
    """Validate standalone output topology and return its shortest stream."""
    if not path.exists() or path.stat().st_size <= 0:
        raise RuntimeError(f"{label} is missing or empty")
    probe = ffprobe_json(tools.ffprobe, path)
    streams = probe.get("streams") if isinstance(probe, dict) else None
    if not isinstance(streams, list):
        raise RuntimeError(f"{label} stream metadata is unreadable")
    required = _required_topology(streams)
    _assert_topology(probe, required, label)
    return _assert_stream_coverage(probe, expected_duration, label)


def validate_master(tools: Tools, path: Path, expected_duration: float = 0.0,
                    tolerance: float = MAX_SHORTFALL_SECONDS, *,
                    source: Path | None = None,
                    required_topology: MediaTopology | None = None) -> None:
    """Prove a master is readable and complete. Raises if it is not.

    The caller deletes the only other copy of this video once this returns, so it
    checks the file rather than trusting ffmpeg's exit code, and it prefers a
    false negative -- a retained .ts and a failed chunk are both recoverable,
    a deleted .ts beside a truncated master is not.
    """
    if not path.exists() or path.stat().st_size < 1024:
        raise RuntimeError("master is missing or empty")

    probe = ffprobe_json(tools.ffprobe, path)
    streams = probe.get("streams")
    if not isinstance(streams, list):
        raise RuntimeError("master stream metadata is unreadable")
    video_streams = [stream for stream in streams
                     if isinstance(stream, dict)
                     and stream.get("codec_type") == "video"]
    if not video_streams:
        raise RuntimeError("master has no readable video stream")

    if required_topology is None and source is not None:
        try:
            source.stat()
        except OSError as exc:
            raise RuntimeError(
                f"could not inspect source inventory before validating master: "
                f"{exc}") from exc
        if not source.is_file():
            raise RuntimeError("master source is not a regular media file")
        source_probe = ffprobe_json(tools.ffprobe, source)
        source_streams = source_probe.get("streams")
        if not isinstance(source_streams, list):
            raise RuntimeError("source stream metadata is unreadable")
        # Apply the same policy as the remux itself before comparing inventories.
        plan_remux_maps(source_streams)
        required_topology = _required_topology(source_streams)
    if required_topology is not None:
        _assert_topology(probe, required_topology, "master")

    # Aggregate format duration can come from a complete audio track even when
    # video stopped much earlier. Require the video itself to be identifiable and
    # timed; this check authorises deletion of the source TS, so unknown is a
    # failure rather than permission to discard the recoverable copy.
    for stream in video_streams:
        if not isinstance(stream.get("codec_name"), str):
            raise RuntimeError("master video stream has no readable codec")
        try:
            width = int(stream.get("width") or 0)
            height = int(stream.get("height") or 0)
        except (TypeError, ValueError):
            width = height = 0
        if width <= 0 or height <= 0:
            raise RuntimeError("master video stream has no readable dimensions")

    _assert_stream_coverage(probe, expected_duration, "master", tolerance)


def _join_piece_shortfall(expected_duration: float) -> float:
    """Packet/mux slop accepted only for temporary MPEG-TS join pieces."""
    return max(MIN_SHORTFALL_SECONDS,
               min(0.25, expected_duration * 0.15))


def video_dimensions(tools: Tools, path: Path) -> tuple[int, int]:
    """(width, height) of the first video stream; (0, 0) when unreadable.

    Ground truth for what was actually captured. streamlink's `Opening stream:
    720p60` says what we asked Twitch for; this says what landed on disk, and
    the two are recorded separately because they can disagree.

    Never raises: this is diagnostic metadata, and failing a finished master over
    an unreadable probe would trade a real recording for a cosmetic field.
    """
    try:
        probe = ffprobe_json(tools.ffprobe, path)
    except Exception:
        return (0, 0)
    for stream in probe.get("streams") or []:
        if stream.get("codec_type") != "video":
            continue
        try:
            return (int(stream.get("width") or 0), int(stream.get("height") or 0))
        except (TypeError, ValueError):
            return (0, 0)
    return (0, 0)


def remux_to_mp4(tools: Tools, source: Path, destination: Path,
                 expected_duration: float = 0.0) -> list[str]:
    """Container-only rewrite of a finished .ts into a Premiere-friendly MP4.

    `make_zero` pins the output to start at 0 so master time and transcript time
    agree; `faststart` puts the index up front so Premiere opens it instantly.
    Returns the list of streams deliberately left out.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    probe = ffprobe_json(tools.ffprobe, source)
    source_streams = probe.get("streams")
    if not isinstance(source_streams, list):
        raise RuntimeError("recording stream metadata is unreadable")
    maps, dropped = plan_remux_maps(source_streams)
    required = _required_topology(source_streams)
    attributes = _stream_attribute_args(source_streams)
    if dropped:
        unexpected = [name for name in dropped
                      if name not in ROUTINE_DROPPED_STREAMS]
        log = LOG.warning if unexpected else LOG.info
        log("%s: not carried into the master: %s",
            source.name, ", ".join(dropped))

    temp = destination.with_suffix(".partial.mp4")
    try:
        result = run(
            [tools.ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
             "-fflags", "+genpts",
              "-i", str(source),
              *maps,
              "-c", "copy",
              *attributes,
              "-avoid_negative_ts", "make_zero",
             "-movflags", "+faststart",
             str(temp)],
            timeout=7200,
        )
        if result.returncode != 0:
            raise RuntimeError(f"remux failed: {result.stderr.strip()[-800:]}")
        validate_master(tools, temp, expected_duration,
                        required_topology=required)
        temp.replace(destination)
    finally:
        # Covers the timeout path too, where `run` raises before we get here.
        temp.unlink(missing_ok=True)
    return dropped


# ------------------------------------------------------------------------ proxies


def _proxy_output_dimensions(stream: dict[str, Any], height: int) -> tuple[int, int]:
    try:
        source_width = int(stream.get("width") or 0)
        source_height = int(stream.get("height") or 0)
        output_height = int(height)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("proxy source has invalid video dimensions") from exc
    if source_width <= 0 or source_height <= 0 or output_height <= 0:
        raise RuntimeError("proxy source has no usable video dimensions")
    scaled_width = source_width * output_height / source_height
    output_width = max(2, int(math.floor(scaled_width / 2.0 + 0.5)) * 2)
    return output_width, output_height


def _proxy_topology(source_probe: dict[str, Any], height: int) -> tuple[
        list[dict[str, Any]], MediaTopology, float]:
    source_streams = source_probe.get("streams")
    if not isinstance(source_streams, list):
        raise RuntimeError("proxy source stream metadata is unreadable")
    required_without_dimensions = _required_topology(
        source_streams, video_codec="h264", audio_codec="aac",
        dimensions=False)
    primary_video = next(stream for stream in source_streams
                         if stream.get("codec_type") == "video")
    width, output_height = _proxy_output_dimensions(primary_video, height)
    required = MediaTopology(
        tuple((codec, width, output_height)
              for codec, _, _ in required_without_dimensions.video),
        required_without_dimensions.audio,
    )
    expected_duration = _probe_duration(source_probe)
    if expected_duration <= 0:
        raise RuntimeError("proxy source reports no usable duration")
    return source_streams, required, expected_duration


def validate_proxy(tools: Tools, source: Path, path: Path, *,
                   height: int = 540) -> None:
    """Prove a proxy matches its master and covers its full elapsed duration."""
    source_probe = ffprobe_json(tools.ffprobe, source)
    _, required, expected_duration = _proxy_topology(source_probe, height)
    _probe_output_topology(
        tools, path, required, "proxy",
        expected_duration=expected_duration)


def _bitrate_bits_per_second(value: str) -> float:
    text = str(value).strip().lower()
    multipliers = {"k": 1_000.0, "m": 1_000_000.0, "g": 1_000_000_000.0}
    multiplier = 1.0
    if text and text[-1:] in multipliers:
        multiplier = multipliers[text[-1]]
        text = text[:-1]
    try:
        bitrate = float(text) * multiplier
    except (TypeError, ValueError):
        return 0.0
    return bitrate if math.isfinite(bitrate) and bitrate > 0 else 0.0


# Bits per pixel an H.264 encode is assumed not to exceed, anchored at CRF/QP 24
# and doubling every six steps towards lossless -- the usual rule of thumb for
# x264's rate ladder. The anchor is deliberately pessimistic: 0.15 bpp is around
# four times what a busy 1080p60 talk stream actually needs at 540p, measured
# against real output from this pipeline.
PROXY_BPP_AT_CRF24 = 0.15
PROXY_BPP_ANCHOR_QUALITY = 24.0
PROXY_BPP_HALVING_STEP = 6.0
# Raw yuv420p is 12 bits per pixel. H.264 never exceeds it, so it caps the model
# no matter how low the configured quality goes.
RAW_YUV420P_BPP = 12.0
# Applied on top of the ceiling above, because a reservation that is merely
# accurate leaves no room for content the model did not anticipate.
PROXY_SAFETY_FACTOR = 2.0


def estimate_proxy_peak_bytes(
    tools: Tools,
    source: Path,
    *,
    height: int = 540,
    quality: int = 24,
    audio_bitrate: str = "128k",
) -> int:
    """Conservative upper bound for one staged proxy encode.

    CORRECTED 2026-08-16. This used to bound the video by every output frame at
    *uncompressed* yuv420p size. That is a true upper bound and a useless one:
    for a 2-hour 540p60 chunk it asked for 319 GB, so every proxy on an
    eight-hour recording was refused on a drive with 200 GB free while the proxy
    that did get built came to 538 MB. A bound no drive can satisfy does not
    protect the disk, it just turns the feature off.

    The estimate is now modelled on what H.264 actually emits -- a bits-per-pixel
    ceiling that scales with the configured CRF/QP -- capped at the raw size the
    old bound used, doubled for headroom, plus encoded audio and mux overhead.
    Under-reserving is the recoverable direction: `make_proxy` stages a
    `.partial.mp4` and cleans it up, so a full drive costs one failed encode,
    whereas over-reserving costs every proxy.
    """
    probe = ffprobe_json(tools.ffprobe, source)
    streams, _, duration = _proxy_topology(probe, height)
    video = next(stream for stream in streams
                 if stream.get("codec_type") == "video")
    try:
        quality_value = float(int(quality))
    except (TypeError, ValueError) as exc:
        raise RuntimeError("proxy quality is invalid") from exc
    output_width, output_height = _proxy_output_dimensions(video, height)
    fps = _stream_frame_rate(video) or 60.0
    frame_count = max(1, math.ceil(duration * fps) + 2)
    pixels = frame_count * output_width * output_height
    # The schema bounds quality to 0..51; the clamp keeps a direct caller with a
    # wilder number from overflowing the exponent rather than hitting the cap.
    steps = min(64.0, (PROXY_BPP_ANCHOR_QUALITY - quality_value)
                / PROXY_BPP_HALVING_STEP)
    bits_per_pixel = min(RAW_YUV420P_BPP,
                         PROXY_BPP_AT_CRF24 * (2.0 ** steps))
    video_bytes = math.ceil(pixels * bits_per_pixel * PROXY_SAFETY_FACTOR / 8.0)

    audio_count = sum(stream.get("codec_type") == "audio" for stream in streams)
    bitrate = _bitrate_bits_per_second(audio_bitrate)
    if audio_count and bitrate <= 0:
        # The schema intentionally allows ffmpeg bitrate spellings. If one is not
        # understood here, reserve stereo PCM-equivalent bytes rather than zero.
        bitrate = 1_536_000.0
    audio_bytes = math.ceil(duration * bitrate / 8.0) * audio_count
    payload = video_bytes + audio_bytes
    mux_overhead = max(8 * 1024 * 1024, math.ceil(payload * 0.02))
    return int(payload + mux_overhead)


def make_proxy(
    tools: Tools,
    source: Path,
    destination: Path,
    *,
    height: int = 540,
    encoder: str = "libx264",
    quality: int = 24,
    audio_bitrate: str = "128k",
) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp = destination.with_suffix(".partial.mp4")

    probe = ffprobe_json(tools.ffprobe, source)
    source_streams, required, expected_duration = _proxy_topology(probe, height)
    maps = _map_selected_streams(source_streams)
    attributes = _stream_attribute_args(source_streams)

    primary_video = next(
        stream for stream in source_streams
        if stream.get("codec_type") == "video")
    fps = _stream_frame_rate(primary_video) or 30.0
    # A keyframe every second keeps scrubbing responsive, which is the entire
    # point of an editing proxy.
    gop = max(1, int(round(fps)))

    codec_args: list[str]
    if encoder == "libx264":
        codec_args = ["-c:v", "libx264", "-preset", "veryfast",
                      "-crf", str(quality), "-profile:v", "main"]
    elif encoder == "h264_amf":
        # AMF has no CRF. Constant-QP at a slightly looser number lands in the
        # same visual ballpark as the x264 CRF default.
        qp = max(0, min(51, int(quality) + 2))
        codec_args = ["-c:v", "h264_amf", "-rc", "cqp",
                      "-qp_i", str(qp), "-qp_p", str(qp), "-quality", "speed"]
    elif encoder == "h264_nvenc":
        codec_args = ["-c:v", "h264_nvenc", "-preset", "p3",
                      "-rc", "constqp", "-qp", str(int(quality) + 2)]
    else:
        codec_args = ["-c:v", encoder, "-b:v", "3M"]

    committed = False
    try:
        result = run(
            [tools.ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
             "-i", str(source),
              # ffmpeg's automatic selection keeps exactly one audio stream.
              # Absolute maps are intentional: the inventory above is the exact
              # contract the candidate must satisfy before it is published.
              *maps,
              "-vf", f"scale=-2:{int(height)}",
             *codec_args,
             "-pix_fmt", "yuv420p",
              "-g", str(gop),
              "-c:a", "aac", "-b:a", audio_bitrate,
              *attributes,
              "-movflags", "+faststart",
             str(temp)],
            timeout=14400,
        )
        if result.returncode != 0 or not temp.exists():
            raise RuntimeError(f"proxy encode failed: {result.stderr.strip()[-800:]}")
        _probe_output_topology(
            tools, temp, required, "proxy candidate",
            expected_duration=expected_duration)
        temp.replace(destination)
        committed = True
    finally:
        # AUD2-032: `run` raises TimeoutExpired rather than returning, so cleanup
        # on the returned-failure path alone left multi-gigabyte `.partial.mp4`
        # debris behind on every timeout.
        if not committed:
            temp.unlink(missing_ok=True)


# ----------------------------------------------------------------- audio streams


def _default_disposition(stream: dict[str, Any]) -> bool:
    disposition = stream.get("disposition")
    if disposition is not None and not isinstance(disposition, dict):
        raise RuntimeError("audio stream metadata has an invalid disposition")
    default = (disposition or {}).get("default", 0)
    if default not in (0, 1, False, True):
        raise RuntimeError("audio stream metadata has an invalid default flag")
    return bool(default)


def choose_asr_stream(streams: list[dict[str, Any]],
                      selector: str | int = "auto") -> int | None:
    """Which audio stream the transcript should describe. None if there is none.

    ffmpeg's automatic selection picks the "best" audio track, which it measures
    largely by channel count. On a broadcast carrying a stereo programme track
    plus a 5.1 commentary or second-language track that silently transcribes the
    wrong one -- and the exports would still be labelled with the configured
    programme language. The choice is therefore explicit: a stream marked
    default wins, otherwise the first audio stream in file order.
    """
    audio = [s for s in streams if s.get("codec_type") == "audio"]
    if not audio:
        return None

    indices: list[int] = []
    defaults: list[int] = []
    for stream in audio:
        index = stream.get("index")
        if isinstance(index, bool):
            raise RuntimeError("audio stream metadata has an invalid index")
        try:
            parsed_index = int(index)
        except (TypeError, ValueError) as exc:
            raise RuntimeError("audio stream metadata has an invalid index") from exc
        if parsed_index < 0 or parsed_index in indices:
            raise RuntimeError("audio stream metadata has ambiguous indices")
        indices.append(parsed_index)

        if _default_disposition(stream):
            defaults.append(parsed_index)

    if isinstance(selector, bool):
        raise RuntimeError("audio stream selector must be auto, an ordinal, or a language")
    if isinstance(selector, int):
        if selector < 0 or selector >= len(audio):
            raise RuntimeError(
                f"audio stream ordinal {selector} is unavailable; media has "
                f"{len(audio)} audio stream(s)")
        return indices[selector]
    if not isinstance(selector, str):
        raise RuntimeError("audio stream selector must be auto, an ordinal, or a language")
    value = selector.strip().lower().replace("_", "-")
    if value.isdecimal():
        ordinal = int(value)
        if ordinal >= len(audio):
            raise RuntimeError(
                f"audio stream ordinal {ordinal} is unavailable; media has "
                f"{len(audio)} audio stream(s)")
        return indices[ordinal]
    if value and value != "auto":
        matches = [index for index, stream in zip(indices, audio)
                   if (_stream_language(stream) == value
                       or _stream_language(stream).split("-", 1)[0]
                       == value.split("-", 1)[0])]
        if not matches:
            raise RuntimeError(f"no audio stream is tagged with language {value!r}")
        if len(matches) > 1:
            raise RuntimeError(
                f"audio stream selection is ambiguous: {len(matches)} tracks "
                f"match language {value!r}")
        return matches[0]
    if value != "auto":
        raise RuntimeError("audio stream selector must not be empty")
    if len(defaults) > 1:
        raise RuntimeError("audio stream selection is ambiguous: multiple defaults")
    return defaults[0] if defaults else indices[0]


def asr_stream_identity(streams: list[dict[str, Any]],
                        selector: str | int = "auto") -> tuple[int, dict[str, Any]]:
    """Resolve one logical audio track without persisting its container index."""
    index = choose_asr_stream(streams, selector)
    if index is None:
        raise RuntimeError("media has no readable audio stream")
    audio = [stream for stream in streams if stream.get("codec_type") == "audio"]
    selected = next(stream for stream in audio if _stream_index(stream) == index)
    codec = str(selected.get("codec_name") or "").strip().lower()
    if not codec:
        raise RuntimeError("selected audio stream has no readable codec")
    channels, layout = _channel_identity(selected)
    ordinal = next(position for position, stream in enumerate(audio)
                   if _stream_index(stream) == index)
    # MP4 marks its first audio track default even when MPEG-TS carried no
    # disposition at all. Treat the deterministic first-track fallback as the
    # effective default on both sides of that container-only rewrite.
    effective_default = (_default_disposition(selected)
                         or (ordinal == 0 and not any(
                             _default_disposition(stream) for stream in audio)))
    return index, {
        "ordinal": ordinal,
        "codec": codec,
        "language": _stream_language(selected),
        "channels": channels,
        "layout": layout,
        "default": effective_default,
    }


def probe_asr_stream(tools: Tools, source: Path,
                     selector: str | int = "auto") -> tuple[int, dict[str, Any]]:
    try:
        probe = ffprobe_json(tools.ffprobe, source)
    except Exception as exc:
        raise RuntimeError(f"could not probe {source.name} for audio: {exc}") from exc
    streams = probe.get("streams") if isinstance(probe, dict) else None
    if not isinstance(streams, list):
        raise RuntimeError(f"could not probe {source.name} for audio streams")
    try:
        return asr_stream_identity(streams, selector)
    except RuntimeError as exc:
        raise RuntimeError(f"could not select audio from {source.name}: {exc}") from exc


def extract_audio_slice(
    tools: Tools,
    source: Path,
    destination: Path,
    start: float,
    duration: float,
    *,
    sample_rate: int = 16000,
    audio_stream: str | int = "auto",
) -> Path:
    """Pull `duration` seconds of mono audio starting at chunk-relative `start`.

    FLAC rather than WAV: lossless, roughly half the bytes, and accepted by the
    transcriber as-is.

    `start` is file-relative and is passed to `-ss` unchanged. Input-side `-ss` is
    already measured from the beginning of the file, not from the container's first
    PTS, so adding the container start time would seek that far too late -- on a
    typical MPEG-TS that silently skipped the first ~1.4s of every slice and
    truncated tail requests.

    The audio stream is chosen explicitly rather than left to ffmpeg; see
    `choose_asr_stream()`. Probe failure and ambiguous metadata are fatal because
    implicit selection can silently transcribe a commentary or language track.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    index, _ = probe_asr_stream(tools, source, audio_stream)

    result = run(
        [tools.ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
         "-ss", f"{max(0.0, start):.3f}",
         "-i", str(source),
         "-t", f"{max(0.0, duration):.3f}",
          "-map", f"0:{index}",
         "-vn", "-sn", "-dn",
         "-ac", "1", "-ar", str(sample_rate),
         "-c:a", "flac", "-compression_level", "5",
         str(destination)],
        timeout=1800,
    )
    if result.returncode != 0 or not destination.exists():
        raise RuntimeError(f"audio slice failed: {result.stderr.strip()[-800:]}")
    return destination


def concat_audio(tools: Tools, parts: Sequence[Path], destination: Path) -> Path:
    """Join audio slices end to end into one file.

    Used to build the audio that spans a chunk boundary: the tail of one recording
    followed by the head of the next, transcribed as a single passage so a word
    spoken across the join is heard whole.

    Decodes and re-encodes rather than stream-copying. The pieces are seconds long,
    and the concat *filter* tolerates any difference between the two sources where
    a copy would silently produce a corrupt file.
    """
    parts = [path for path in parts if path.exists()]
    if not parts:
        raise RuntimeError("no audio to join")
    if len(parts) == 1:
        shutil.copyfile(parts[0], destination)
        return destination

    destination.parent.mkdir(parents=True, exist_ok=True)
    inputs: list[str] = []
    for path in parts:
        inputs += ["-i", str(path)]
    # aformat pins both streams to one format; libavfilter inserts the resampling
    # concat needs. Without it a mismatched pair fails the filter graph outright.
    common = "aformat=sample_fmts=s16:sample_rates=16000:channel_layouts=mono"
    chains = ";".join(f"[{index}:a]{common}[a{index}]"
                      for index in range(len(parts)))
    joined = "".join(f"[a{index}]" for index in range(len(parts)))
    graph = f"{chains};{joined}concat=n={len(parts)}:v=0:a=1[out]"

    result = run(
        [tools.ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
         *inputs,
         "-filter_complex", graph,
         "-map", "[out]",
         "-c:a", "flac", "-compression_level", "5",
         str(destination)],
        timeout=600,
    )
    if result.returncode != 0 or not destination.exists():
        raise RuntimeError(f"audio join failed: {result.stderr.strip()[-800:]}")
    return destination


# ----------------------------------------------------------------------- cutting


def _fs_limit(precise: bool, max_output_bytes: int | None) -> list[str]:
    """ffmpeg `-fs` size cap for a precise re-encode, or nothing.

    AUD2-018: a precise snapshot re-encodes, so its size is not bounded by the
    source. `-fs` makes it a hard ceiling equal to the admitted disk reservation,
    so a high-bitrate re-encode stops at the reserved bytes rather than overrunning
    the disk; the caller's coverage check then rejects the truncated output. Never
    applied to a stream copy -- there the output tracks the source and `-fs` could
    truncate a legitimate cut.
    """
    if precise and max_output_bytes and max_output_bytes > 0:
        return ["-fs", str(int(max_output_bytes))]
    return []


def cut_range(
    tools: Tools,
    source: Path,
    destination: Path,
    start: float,
    end: float,
    *,
    precise: bool = False,
    max_output_bytes: int | None = None,
) -> None:
    """Export [start, end) as a standalone MP4 without touching the source.

    Copy mode returns in seconds but can only begin on a keyframe, so the result
    may include up to one GOP (about two seconds on Twitch) of lead-in. Precise
    mode re-encodes video for a frame-exact start and is correspondingly slower.
    """
    if end <= start:
        raise ValueError("snapshot end must be after start")

    destination.parent.mkdir(parents=True, exist_ok=True)
    temp = destination.with_suffix(".partial.mp4")
    duration = end - start

    probe = ffprobe_json(tools.ffprobe, source)
    source_streams = probe.get("streams")
    if not isinstance(source_streams, list):
        raise RuntimeError("snapshot source stream metadata is unreadable")
    maps = _map_selected_streams(source_streams)
    attributes = _stream_attribute_args(source_streams)

    if precise:
        codec_args = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
                      "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k"]
        required = _required_topology(
            source_streams, video_codec="h264", audio_codec="aac")
    else:
        codec_args = ["-c", "copy"]
        required = _required_topology(source_streams)
    size_limit = _fs_limit(precise, max_output_bytes)

    committed = False
    try:
        result = run(
            [tools.ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
             "-ss", f"{max(0.0, start):.3f}",
              "-i", str(source),
              "-t", f"{duration:.3f}",
              *maps,
              *codec_args,
              *attributes,
              *size_limit,
              "-avoid_negative_ts", "make_zero",
             "-movflags", "+faststart",
             str(temp)],
            timeout=7200,
        )
        if result.returncode != 0 or not temp.exists() or temp.stat().st_size == 0:
            raise RuntimeError(f"snapshot failed: {result.stderr.strip()[-800:]}")
        _probe_output_topology(
            tools, temp, required, "snapshot candidate",
            expected_duration=duration)
        temp.replace(destination)
        committed = True
    finally:
        if not committed:
            temp.unlink(missing_ok=True)


def ffconcat_quote(path: Path) -> str:
    """Quote a path for the concat demuxer's `file` directive.

    Forward slashes because the demuxer's tokenizer treats backslash as an escape
    character; ffmpeg accepts POSIX separators on Windows. Apostrophes have to
    leave the quoted run, emit an escaped quote and re-enter, since a single-quoted
    string ends at the next apostrophe -- a user folder called `Dan's VODs` breaks
    naive quoting outright.
    """
    return "'" + path.as_posix().replace("'", "'\\''") + "'"


def cut_and_join(
    tools: Tools,
    parts: Sequence[tuple[Path, float, float]],
    destination: Path,
    *,
    precise: bool = False,
    work_dir: Path | None = None,
    max_output_bytes: int | None = None,
) -> None:
    """Cut (source, start, end) pieces and join them into one MP4.

    A snapshot range can straddle a chunk boundary, in which case it lives in two
    separate files. They came off the same broadcast with the same codec settings,
    so a stream-copy concat joins them without a re-encode.

    In `precise` mode the pieces are re-encoded to a common format before joining,
    so a frame-exact start is honoured across a boundary rather than only within a
    single part.
    """
    parts = [item for item in parts if item[2] - item[1] > 0.05]
    if not parts:
        raise ValueError("snapshot range covers no recorded media")

    if len(parts) == 1:
        source, start, end = parts[0]
        cut_range(tools, source, destination, start, end, precise=precise,
                  max_output_bytes=max_output_bytes)
        return

    plans: list[tuple[list[dict[str, Any]], MediaTopology]] = []
    for source, _, _ in parts:
        probe = ffprobe_json(tools.ffprobe, source)
        streams = probe.get("streams")
        if not isinstance(streams, list):
            raise RuntimeError(
                f"snapshot source {source.name} stream metadata is unreadable")
        required = _required_topology(
            streams,
            video_codec="h264" if precise else None,
            audio_codec="aac" if precise else None,
        )
        plans.append((streams, required))
    expected = plans[0][1]
    for position, (_, topology) in enumerate(plans[1:], start=1):
        if topology != expected:
            raise RuntimeError(
                f"snapshot pieces have incompatible video/audio topology "
                f"(piece 0 {expected!r}, piece {position} {topology!r}); "
                "refusing a lossy or corrupt join")

    work = work_dir or destination.parent / ".join"
    work.mkdir(parents=True, exist_ok=True)
    pieces: list[Path] = []
    try:
        for index, (source, start, end) in enumerate(parts):
            source_streams, required = plans[index]
            piece = work / f"part{index:03d}.ts"
            if precise:
                # Re-encode each piece so the join is frame-exact rather than
                # keyframe-aligned, and so both pieces share one format.
                codec_args = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
                              "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
                              "-f", "mpegts"]
            else:
                codec_args = ["-c", "copy", "-f", "mpegts"]
            result = run(
                [tools.ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                  "-ss", f"{max(0.0, start):.3f}",
                  "-i", str(source),
                  "-t", f"{end - start:.3f}",
                  *_map_selected_streams(source_streams),
                  *codec_args,
                  *_stream_attribute_args(source_streams),
                  *_fs_limit(precise, max_output_bytes),
                  str(piece)],
                timeout=3600,
            )
            if result.returncode != 0 or not piece.exists():
                raise RuntimeError(f"snapshot cut failed: {result.stderr.strip()[-500:]}")
            _probe_output_topology(
                tools, piece, required, f"snapshot piece {index}",
                expected_duration=end - start,
                shortfall_allowance=_join_piece_shortfall(end - start))
            pieces.append(piece)

        listing = work / "join.txt"
        listing.write_text(
            "".join(f"file {ffconcat_quote(piece)}\n" for piece in pieces),
            encoding="utf-8")
        temp = destination.with_suffix(".partial.mp4")
        try:
            first_piece_probe = ffprobe_json(tools.ffprobe, pieces[0])
            first_piece_streams = first_piece_probe.get("streams")
            if not isinstance(first_piece_streams, list):
                raise RuntimeError("snapshot piece stream metadata is unreadable")
            result = run(
                [tools.ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                 "-f", "concat", "-safe", "0", "-i", str(listing),
                 *_map_selected_streams(first_piece_streams),
                 "-c", "copy",
                 *_stream_attribute_args(plans[0][0]),
                 "-avoid_negative_ts", "make_zero",
                 "-movflags", "+faststart",
                 str(temp)],
                timeout=3600,
            )
            if result.returncode != 0 or not temp.exists():
                raise RuntimeError(f"snapshot join failed: {result.stderr.strip()[-800:]}")
            _probe_output_topology(
                tools, temp, expected, "joined snapshot candidate",
                expected_duration=sum(end - start for _, start, end in parts))
            temp.replace(destination)
        finally:
            temp.unlink(missing_ok=True)
    finally:
        shutil.rmtree(work, ignore_errors=True)


# ------------------------------------------------------------------------ helpers


def live_duration(tools: Tools, path: Path, *, allow_scan: bool = False) -> float:
    """Readable duration of a chunk, including one ffmpeg is still appending to.

    Scanning is off by default: this is called repeatedly against a growing file,
    and the packet-walk fallback reads the whole thing.
    """
    if not path.exists():
        return 0.0
    try:
        return media_duration(tools.ffprobe, path, allow_scan=allow_scan)
    except Exception:
        return 0.0
