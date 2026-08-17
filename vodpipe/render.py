"""Turning an `EditPlan` into a finished file.

Four passes, in this order, because each one's cost depends on the previous
one's answer:

1. **decode the audio to PCM** -- cheap, and everything else needs it;
2. **measure the loudness envelope** -- one more audio pass, no video touched;
3. **plan** -- pure arithmetic, and the only step allowed to refuse;
4. **encode the kept frames, assemble the audio, mux** -- the expensive part,
   reached only once the plan is known to be sane.

Planning before encoding is the whole reason for the ordering. A misconfigured
threshold or a silent track produces an absurd plan, and finding that out after
half an hour of h264 is the difference between a warning and a wasted evening.

The master is never touched. Everything is staged under a working directory and
the finished file is moved into place at the end, so a failure or a kill leaves
the previous edit -- or no edit -- rather than a half-written one.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

from . import media
from .audio import assemble, segments_for
from .exports import write_exports
from .edit import (
    EditOptions,
    EditPlan,
    EditRefused,
    describe,
    plan_edit,
    remap_words,
    render_report,
)
from .transcript import CensorList, Word
from .util import LOG, atomic_write_text


@dataclass
class EditResult:
    """What was produced, and enough about it to publish the transcript."""

    destination: Path
    plan: EditPlan
    words: list[Word]
    duration: float
    frames: int
    report: str


def render_edit(
    tools: media.Tools,
    source: Path,
    destination: Path,
    words: Sequence[Word],
    options: EditOptions,
    *,
    work_dir: Path,
    censor: CensorList | None = None,
    encoder: str = "libx264",
    quality: int = 20,
    audio_bitrate: str = "192k",
    crossfade_seconds: float = 0.020,
    mute_ramp_seconds: float = 0.010,
    audio_stream: str | int = "auto",
    session_offset: float = 0.0,
    progress: Callable[[str], None] | None = None,
    plan_only: bool = False,
) -> EditResult:
    """Cut `source` down to `destination` according to `options`."""

    def step(message: str) -> None:
        LOG.info("%s: %s", source.name, message)
        if progress is not None:
            progress(message)

    work_dir.mkdir(parents=True, exist_ok=True)
    pcm = work_dir / "source.wav"
    edited_pcm = work_dir / "edited.wav"
    video_only = work_dir / "video.mp4"
    staged = destination.with_suffix(".partial.mp4")

    try:
        fps, start_delta, sample_rate, video_frames = media.edit_stream_geometry(
            tools, source)

        step("extracting audio")
        media.extract_pcm(tools, source, pcm, sample_rate=sample_rate,
                          audio_stream=audio_stream)

        step("measuring levels")
        envelope, hop = media.audio_envelope(tools, pcm, sample_rate=sample_rate)
        # The edit lives where both streams exist. A capture's audio can outrun
        # its video by a second, and planning over that tail asks the encoder for
        # frames that were never recorded.
        duration = min(len(envelope) * hop, video_frames / fps)

        step("planning the edit")
        plan = plan_edit(words, envelope, hop, duration, options, censor)
        report = _report(plan, source, session_offset, envelope, hop)
        LOG.info("%s: %s", source.name, describe(plan))
        if plan_only:
            return EditResult(destination, plan, list(words), plan.kept_seconds,
                              0, report)

        # Frame-locked ranges, and the audio segments that match them exactly.
        video_ranges, audio_segments, total_frames = segments_for(
            plan.keep, fps=fps, sample_rate=sample_rate,
            start_offset=start_delta, max_frames=video_frames)
        if not video_ranges:
            raise EditRefused("the plan keeps no whole video frames")
        select_ranges = [((first - 0.5) / fps, (last + 0.5) / fps)
                         for first, last in video_ranges]

        step(f"encoding {total_frames} frames ({plan.cuts} cuts)")
        frames = media.render_edited_video(
            tools, source, video_only, select_ranges, fps=fps,
            encoder=encoder, quality=quality)
        if frames and frames != total_frames:
            raise RuntimeError(
                f"the edit rendered {frames} frames where the plan called for "
                f"{total_frames}; the audio would not line up with it")

        step("assembling audio")
        written = assemble(
            pcm, edited_pcm, audio_segments,
            mutes=_mutes_in_source_frames(plan, sample_rate, start_delta),
            crossfade_samples=max(0, int(crossfade_seconds * sample_rate)),
            mute_ramp_samples=max(0, int(mute_ramp_seconds * sample_rate)),
        )
        expected = int(round(total_frames * sample_rate / fps))
        if abs(written - expected) > 1:
            raise RuntimeError(
                f"assembled {written} audio frames against {expected} for the "
                f"video; refusing to publish a track that would drift")

        step("muxing")
        media.mux_edited(tools, video_only, edited_pcm, staged,
                         audio_bitrate=audio_bitrate)
        _verify(tools, staged, total_frames / fps)

        destination.parent.mkdir(parents=True, exist_ok=True)
        staged.replace(destination)
        staged = destination
        # Remapped against the ranges that were *rendered*, not the ones that
        # were planned. Each planned boundary is rounded to a frame, and
        # `remap_words` places a word by the cumulative length of everything
        # before it, so using the planned lengths lets those roundings
        # random-walk -- about 0.2 s of transcript drift by the end of a chunk
        # with 478 cuts. The frame-derived ranges sum to exactly the output's
        # duration, so there is nothing to accumulate.
        rendered = [(first / fps, (last + 1) / fps)
                    for first, last in video_ranges]
        return EditResult(
            destination, plan, remap_words(words, rendered),
            total_frames / fps, total_frames, report)
    finally:
        for path in (pcm, edited_pcm, video_only):
            path.unlink(missing_ok=True)
        if staged != destination:
            staged.unlink(missing_ok=True)
        shutil.rmtree(work_dir, ignore_errors=True)


def publish_edit(directory: Path, result: EditResult, *, generation: str,
                 language: str = "en", censor: CensorList | None = None,
                 meta: dict | None = None) -> None:
    """Write `edit.md` and the transcript of the *edited* media.

    Shared by the pipeline job and `vodpipe edit` on purpose. The transcript is
    what makes the cut file usable for text-based editing, and its `words.json`
    is also where the source generation is recorded -- so a caller that renders
    the media without publishing this leaves an edit that recovery cannot
    recognise and will rebuild on the next start.
    """
    atomic_write_text(directory / "edit.md", result.report)
    identity = dict(meta or {})
    write_exports(
        directory / "edited",
        result.words,
        language=language,
        censor=censor,
        meta={**identity, "complete": True,
              "source": result.destination.name},
        words_meta={
            **identity,
            "source": result.destination.name,
            "language": language,
            "complete": True,
            "covered_seconds": round(result.duration, 3),
            "expected_seconds": round(result.duration, 3),
            "edited_from_generation": generation,
        },
    )


def _mutes_in_source_frames(plan: EditPlan, sample_rate: int,
                            start_delta: float) -> list[tuple[int, int]]:
    """Mute spans as source PCM frame indices.

    Expressed against the *source* rather than the finished timeline because the
    assembler already walks the source segment by segment; converting once here
    keeps a second coordinate system out of the inner loop.
    """
    shift = start_delta * sample_rate
    return [(int(round(mute.start * sample_rate + shift)),
             int(round(mute.end * sample_rate + shift)))
            for mute in plan.mutes]


def _report(plan: EditPlan, source: Path, offset: float,
            envelope: Sequence[float], hop: float) -> str:
    return render_report(plan, source=source.name, offset=offset,
                         envelope=envelope, hop=hop)


def _verify(tools: media.Tools, path: Path, expected: float) -> None:
    """A published edit must actually be readable and the length it claims."""
    probe = media.ffprobe_json(tools.ffprobe, path)
    streams = probe.get("streams", [])
    if not any(s.get("codec_type") == "video" for s in streams):
        raise RuntimeError("the edited file has no video stream")
    if not any(s.get("codec_type") == "audio" for s in streams):
        raise RuntimeError("the edited file has no audio stream")
    actual = media.media_duration(tools.ffprobe, path, allow_scan=True)
    # One frame of tolerance at the slowest rate we would ever see; the two
    # tracks were built to match exactly, so a real discrepancy is a bug.
    if abs(actual - expected) > 0.2:
        raise RuntimeError(
            f"the edited file is {actual:.2f}s where the plan called for "
            f"{expected:.2f}s")
