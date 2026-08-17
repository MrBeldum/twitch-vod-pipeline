"""Assembling the edited audio track, sample by sample.

The video side of an edit is done by ffmpeg's `select` filter, which drops whole
frames and therefore quantises every cut to 1/fps. If the audio were cut by the
matching `aselect` it would quantise to its own filter frame instead, and the two
roundings would disagree at every join. They are independent, so the error is a
random walk: over the ~950 cuts a two-hour chunk of this material produces, that
is a quarter of a second of drift by the end -- audible, and impossible to fix
afterwards.

So the audio is assembled here instead, in samples, from segments whose lengths
are derived from the video frame counts rather than measured separately. Each
kept range contributes exactly `frames * sample_rate / fps` samples, so the two
tracks are equal by construction and no drift can accumulate. Verified against a
synthetic source with a flash and a beep on every second: A/V offset stays inside
one frame, and its spread is identical at 21 cuts and at 113.

Doing it here rather than in ffmpeg also makes the two things the operator asked
for straightforward -- a real crossfade at every join, and muting rather than
cutting for censored words -- neither of which a filtergraph does well at this
scale.

Stdlib only: `wave` and `array`. The bulk of the work is `readframes` into
`writeframes`, which is a copy; only the few hundred samples around each join are
touched individually, so a two-hour track costs seconds rather than minutes.
"""

from __future__ import annotations

import array
import math
import wave
from pathlib import Path
from typing import Sequence

# s16le is what the extraction asks ffmpeg for. `array('h')` is exactly that on
# every little-endian platform, which is the only kind this runs on; anything
# else is rejected rather than silently mangled.
SAMPLE_WIDTH = 2
_MAX = 32767
_MIN = -32768


class AudioAssemblyError(RuntimeError):
    """The PCM could not be assembled. Nothing has been published."""


def _fade_tables(length: int) -> tuple[list[float], list[float]]:
    """Equal-power crossfade curves.

    Equal power rather than linear because the two sides of a cut are unrelated
    signals: summing them linearly dips ~3 dB in the middle, which is audible as
    a hole exactly where the edit is. sin/cos keeps the sum of squares at one.
    """
    fade_in = [math.sin(math.pi / 2 * (index + 0.5) / length)
               for index in range(length)]
    fade_out = [math.cos(math.pi / 2 * (index + 0.5) / length)
                for index in range(length)]
    return fade_in, fade_out


def assemble(
    source: Path,
    destination: Path,
    segments: Sequence[tuple[int, int]],
    *,
    mutes: Sequence[tuple[int, int]] = (),
    crossfade_samples: int = 0,
    mute_ramp_samples: int = 0,
) -> int:
    """Write `segments` of `source` to `destination`. Returns frames written.

    `segments` are `(start_frame, length)` pairs in source frames, already
    ordered; `mutes` are `(start_frame, end_frame)` spans, also in *source*
    frames, which are silenced wherever they fall inside a kept segment.

    The crossfade is length-preserving. At each join the first `crossfade`
    frames of the incoming segment are mixed with the frames that would have
    *followed* the outgoing one -- that is, the beginning of the material being
    cut out. Both sides of the blend are therefore real audio continuing
    naturally from where the listener was, and the output is not one sample
    longer or shorter for it.
    """
    if not segments:
        raise AudioAssemblyError("an edit must keep at least one segment")

    with wave.open(str(source), "rb") as reader:
        channels = reader.getnchannels()
        width = reader.getsampwidth()
        rate = reader.getframerate()
        available = reader.getnframes()
        if width != SAMPLE_WIDTH:
            raise AudioAssemblyError(
                f"expected 16-bit PCM, found {width * 8}-bit; the extraction "
                f"step is what guarantees this")
        if channels <= 0 or rate <= 0:
            raise AudioAssemblyError("source audio declares no channels or rate")

        def read(start: int, count: int) -> array.array:
            """`count` frames from `start`, zero-padded past the end of file."""
            block = array.array("h")
            if count <= 0:
                return block
            begin = max(0, min(start, available))
            usable = max(0, min(count, available - begin))
            if usable:
                reader.setpos(begin)
                block.frombytes(reader.readframes(usable))
            missing = count * channels - len(block)
            if missing > 0:
                # A list of zeros, not `bytes(...)`: `array('h').extend()` walks
                # an iterable of *ints*, so a bytes object appends one sample per
                # byte and pads to twice the length asked for. The audio then no
                # longer matches the frame count it was derived from, which is
                # the one thing this module exists to guarantee.
                block.extend([0] * missing)
            return block

        written = 0
        previous_end: int | None = None
        destination.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(destination), "wb") as writer:
            writer.setnchannels(channels)
            writer.setsampwidth(SAMPLE_WIDTH)
            writer.setframerate(rate)

            for start, length in segments:
                if length <= 0:
                    continue
                block = read(start, length)

                if previous_end is not None and crossfade_samples > 0:
                    span = min(crossfade_samples, length)
                    tail = read(previous_end, span)
                    fade_in, fade_out = _fade_tables(span)
                    for index in range(span):
                        rising, falling = fade_in[index], fade_out[index]
                        base = index * channels
                        for channel in range(channels):
                            at = base + channel
                            mixed = int(block[at] * rising + tail[at] * falling)
                            block[at] = _MAX if mixed > _MAX else (
                                _MIN if mixed < _MIN else mixed)

                _apply_mutes(block, start, length, channels, mutes,
                             mute_ramp_samples)

                writer.writeframes(block.tobytes())
                written += length
                previous_end = start + length

    if written <= 0:
        raise AudioAssemblyError("the assembled audio is empty")
    return written


def _apply_mutes(block: array.array, start: int, length: int, channels: int,
                 mutes: Sequence[tuple[int, int]], ramp: int) -> None:
    """Silence the censored spans falling in this segment, with soft edges.

    A hard zero at an arbitrary sample is a step in the waveform, which is a
    click -- the exact artefact the whole edit is supposed to avoid. The ramp
    lives *outside* the muted span, inside the margin the caller already added
    around the word, so the word itself is still fully covered.
    """
    for mute_start, mute_end in mutes:
        low = max(mute_start, start) - start
        high = min(mute_end, start + length) - start
        if high <= low:
            continue
        for frame in range(low, high):
            base = frame * channels
            for channel in range(channels):
                block[base + channel] = 0
        if ramp <= 0:
            continue
        for step in range(min(ramp, low)):
            gain = 1.0 - (step + 1) / (ramp + 1)
            base = (low - ramp + step) * channels
            for channel in range(channels):
                block[base + channel] = int(block[base + channel] * gain)
        for step in range(min(ramp, length - high)):
            gain = (step + 1) / (ramp + 1)
            base = (high + step) * channels
            for channel in range(channels):
                block[base + channel] = int(block[base + channel] * gain)


def segments_for(keep: Sequence[tuple[float, float]], *, fps: float,
                 sample_rate: int, start_offset: float = 0.0,
                 max_frames: int | None = None,
                 ) -> tuple[list[tuple[int, int]], list[tuple[int, int]], int]:
    """Frame-locked video ranges and the audio segments that match them exactly.

    Returns `(video_ranges, audio_segments, total_frames)`, where `video_ranges`
    are the `(low, high)` seconds to hand ffmpeg's `select` -- offset by half a
    frame so a float comparison can never land ambiguously on a frame's exact
    timestamp -- and `audio_segments` are `(start_frame, length)` in source
    audio frames.

    `start_offset` is the video stream's start time minus the audio stream's, in
    seconds. Our masters carry a real one (0.034 vs 0.044 on the reference
    recording, i.e. 10 ms, i.e. 480 samples): ignoring it puts the whole track
    half a frame out from the first cut onward.

    `max_frames` is the number of video frames that actually exist, and it is not
    optional in practice. The plan is built on the *audio* timeline, and the two
    streams of a real capture do not end together: on the reference recording
    one chunk's audio runs 1.03 s past its video (3533.51 s against 3532.48 s).
    Without this clamp the tail of the plan claims fourteen frames that were
    never recorded, the encoder produces fewer frames than the audio was cut
    for, and the whole track is out of step from that point back.
    """
    if fps <= 0:
        raise AudioAssemblyError("cannot lock an edit to a non-positive frame rate")
    video: list[tuple[int, int]] = []
    audio: list[tuple[int, int]] = []
    total = 0
    shift = start_offset * sample_rate
    carry = 0.0
    ceiling = None if max_frames is None else max(0, int(max_frames) - 1)
    for low, high in keep:
        first = math.ceil(low * fps - 1e-9)
        last = math.floor(high * fps + 1e-9)
        if ceiling is not None:
            if first > ceiling:
                continue
            last = min(last, ceiling)
        if last < first:
            continue
        count = last - first + 1
        video.append((first, last))
        # Exact at 48 kHz against 60 or 30 fps (800 and 1600 samples a frame).
        # The carry keeps a fractional ratio -- 59.94, say -- from accumulating.
        exact = count * sample_rate / fps
        length = int(exact + carry + 0.5)
        carry += exact - length
        audio.append((int(round(first * sample_rate / fps + shift)), length))
        total += count
    return video, audio, total
