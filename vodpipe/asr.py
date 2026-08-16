"""Speech recognition providers.

Deepgram is the configured engine: it returns native word-level start/end times,
which is the single reason this pipeline needs no forced-alignment stage and can
publish a chunk's transcript about a minute after the chunk closes rather than six.

The provider interface is deliberately thin so a different engine can be dropped in
without anything downstream noticing.
"""

from __future__ import annotations

import json
import inspect
import math
import time
from email.utils import parsedate_to_datetime
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Protocol

from .transcript import Word, normalise
from .util import LOG

DEEPGRAM_ENDPOINT = "https://api.deepgram.com/v1/listen"

# How far past the end of the submitted audio a word may *begin* before the
# response is treated as describing something other than what we sent.
#
# CORRECTED 2026-08-16 -- do not reinstate a hard end-time bound. An 8-hour
# recording of a live channel failed roughly half of all rolling passes on
# "deepgram word 'x' ends beyond the response audio duration", and both halves
# of that check were wrong:
#
# * `metadata.duration` is coarse. nova-3 reported it as a whole number of
#   seconds for most slices (49.0, 63.0, 77.0, 93.0 ...) while the slice itself
#   was 63.9s, so words landed "beyond" audio that was really there.
# * A word's *end* is an estimate, not a measurement. The last word of a passage
#   routinely ends after the audio does, by a few milliseconds to about a second.
#
# Neither is a corruption signal. What is one is a word that *starts* after the
# audio ended: that describes a different, longer recording -- a mismatched or
# replayed response -- and no amount of end-time estimation produces it. Starts
# are therefore bounded and ends are clamped.
WORD_START_TOLERANCE = 2.0


class TranscriptionError(RuntimeError):
    pass


class AuthError(TranscriptionError):
    """A bad or missing key. Retrying will not help."""


class _RetryableHTTPError(TranscriptionError):
    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class ASRProvider(Protocol):
    def transcribe(self, audio: Path, *,
                   expected_duration: float | None = None) -> list[Word]:
        ...


def transcribe_audio(provider: ASRProvider, audio: Path,
                     expected_duration: float) -> list[Word]:
    """Pass measured duration when supported, preserving simple test providers."""
    method = provider.transcribe
    try:
        inspect.signature(method).bind(
            audio, expected_duration=expected_duration)
    except (TypeError, ValueError):
        return method(audio)
    return method(audio, expected_duration=expected_duration)


class DeepgramProvider:
    def __init__(
        self,
        api_key: str,
        *,
        model: str = "nova-3",
        language: str = "en",
        filler_words: bool = True,
        max_retries: int = 4,
        timeout: float = 600.0,
    ) -> None:
        if not api_key:
            raise AuthError("no Deepgram API key configured")
        self.api_key = api_key
        self.model = model
        self.language = language
        self.filler_words = filler_words
        self.max_retries = max(1, max_retries)
        self.timeout = timeout

    def _url(self) -> str:
        params = {
            "model": self.model,
            "language": self.language,
            "punctuate": "true",
            "smart_format": "true",
            # Fillers are transcribed by default so the transcript is verbatim:
            # a cut made from the text then lands where the editor expects, and
            # an "uh" can be selected and deleted on its own.
            "filler_words": "true" if self.filler_words else "false",
            "diarize": "false",
        }
        return f"{DEEPGRAM_ENDPOINT}?{urllib.parse.urlencode(params)}"

    def transcribe(self, audio: Path, *,
                   expected_duration: float | None = None) -> list[Word]:
        payload = audio.read_bytes()
        if not payload:
            return []

        deadline = time.monotonic() + max(0.0, self.timeout)
        last_error: Exception | None = None
        attempts = 0
        for attempt in range(self.max_retries):
            if time.monotonic() >= deadline:
                last_error = TimeoutError("Deepgram total deadline expired")
                break
            attempts = attempt + 1
            try:
                response = self._post(payload, deadline=deadline)
                return parse_deepgram(
                    response, expected_duration=expected_duration)
            except (_RetryableHTTPError, urllib.error.URLError, TimeoutError) as exc:
                last_error = exc
                if attempt == self.max_retries - 1:
                    break
                delay = (exc.retry_after
                         if isinstance(exc, _RetryableHTTPError)
                         and exc.retry_after is not None
                         else min(60.0, 2.0 ** attempt * 2.0))
                remaining = deadline - time.monotonic()
                if remaining <= 0.0 or delay >= remaining:
                    last_error = TimeoutError(
                        "Deepgram total deadline expired before retry")
                    break
                LOG.warning("deepgram attempt %d/%d failed (%s); retrying in %.0fs",
                            attempt + 1, self.max_retries, exc, delay)
                time.sleep(delay)

        raise TranscriptionError(f"deepgram failed after {attempts} attempt(s): "
                                 f"{last_error}")

    def _post(self, payload: bytes, *, deadline: float) -> dict[str, Any]:
        request = urllib.request.Request(
            self._url(),
            data=payload,
            method="POST",
            headers={
                "Authorization": f"Token {self.api_key}",
                "Content-Type": "audio/flac",
            },
        )
        try:
            remaining = _remaining(deadline)
            with urllib.request.urlopen(request, timeout=remaining) as response:
                body = _read_response(response, deadline)
        except urllib.error.HTTPError as exc:
            retry_after = _retry_after(exc.headers)
            try:
                try:
                    detail = _read_response(exc, deadline).decode(
                        "utf-8", "replace")[:500]
                except TimeoutError:
                    detail = "response body exceeded the total deadline"
            finally:
                exc.close()
            if exc.code in (401, 403):
                raise AuthError(f"deepgram rejected the API key ({exc.code}): {detail}")
            if exc.code in (408, 429) or 500 <= exc.code <= 599:
                raise _RetryableHTTPError(
                    f"deepgram HTTP {exc.code}: {detail}", retry_after)
            raise TranscriptionError(f"deepgram HTTP {exc.code}: {detail}")

        try:
            return json.loads(body)
        except json.JSONDecodeError as exc:
            raise TranscriptionError(f"deepgram returned unparsable JSON: {exc}")


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0.0:
        raise TimeoutError("Deepgram total deadline expired")
    return remaining


def _set_response_timeout(response: Any, timeout: float) -> None:
    """Keep each socket read bounded by the remaining total deadline."""
    raw = getattr(getattr(response, "fp", None), "raw", None)
    sock = getattr(raw, "_sock", None)
    if sock is not None and hasattr(sock, "settimeout"):
        sock.settimeout(max(0.001, timeout))


def _read_response(response: Any, deadline: float) -> bytes:
    chunks: list[bytes] = []
    reader = getattr(response, "read1", None)
    if not callable(reader):
        reader = response.read
    while True:
        _set_response_timeout(response, _remaining(deadline))
        chunk = reader(64 * 1024)
        if time.monotonic() > deadline:
            raise TimeoutError("Deepgram total deadline expired while reading")
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)


def _retry_after(headers: Any) -> float | None:
    if headers is None:
        return None
    value = headers.get("Retry-After")
    if value is None:
        return None
    text = str(value).strip()
    try:
        return max(0.0, float(text))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(text)
            return max(0.0, parsed.timestamp() - time.time())
        except (TypeError, ValueError, OverflowError):
            return None


def _response_number(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TranscriptionError(f"deepgram {field} was not a number")
    number = float(value)
    if not math.isfinite(number):
        raise TranscriptionError(f"deepgram {field} was not finite")
    return number


def parse_deepgram(response: dict[str, Any], *,
                   expected_duration: float | None = None) -> list[Word]:
    """Pull the word stream out of a Deepgram response.

    `punctuated_word` is preferred over `word`: Premiere's end-of-sentence flag is
    derived from trailing punctuation, so the punctuated form is what the exporter
    needs to see.

    Every level of the envelope is checked rather than defaulted away. This used
    to be a chain of `or []`, so an error-shaped 200, a truncated body, or a
    changed schema all produced an empty list -- indistinguishable from real
    silence. The caller believes that: it advances its coverage cursor past the
    audio and can retire the previous exports, so a passage of speech is
    permanently published as having contained none. Only an explicit `words: []`
    inside a well-formed alternative counts as silence; transcript text with no
    word timings does not.
    """
    if not isinstance(response, dict):
        raise TranscriptionError("deepgram response was not a JSON object")
    for key in ("err_code", "err_msg", "error", "message"):
        if key not in response:
            continue
        value = response[key]
        present = (bool(value.strip()) if isinstance(value, str)
                   else bool(value) if isinstance(value, (dict, list, tuple, set))
                   else value is not None)
        if present:
            detail = (json.dumps(value, ensure_ascii=False)
                      if isinstance(value, (dict, list)) else str(value))
            raise TranscriptionError(
                f"deepgram reported an error in {key}: {detail[:300]}")

    results = response.get("results")
    if not isinstance(results, dict):
        raise TranscriptionError("deepgram response has no results object")
    channels = results.get("channels")
    if not isinstance(channels, list) or not channels:
        raise TranscriptionError("deepgram response has no channels")
    if not isinstance(channels[0], dict):
        raise TranscriptionError("deepgram channel was not an object")
    alternatives = channels[0].get("alternatives")
    if not isinstance(alternatives, list) or not alternatives:
        raise TranscriptionError("deepgram response has no alternatives")
    if not isinstance(alternatives[0], dict):
        raise TranscriptionError("deepgram alternative was not an object")

    audio_duration: float | None = None
    if "metadata" in response:
        metadata = response["metadata"]
        if not isinstance(metadata, dict):
            raise TranscriptionError("deepgram metadata was not an object")
        if "duration" in metadata:
            audio_duration = _response_number(
                metadata["duration"], "metadata duration")
            if audio_duration < 0.0:
                raise TranscriptionError("deepgram metadata duration was negative")
    submitted_duration: float | None = None
    if expected_duration is not None:
        submitted_duration = _response_number(
            expected_duration, "submitted audio duration")
        if submitted_duration < 0.0:
            raise TranscriptionError("submitted audio duration was negative")

    raw = alternatives[0].get("words")
    if raw is None:
        raise TranscriptionError(
            "deepgram response carried no word timings; refusing to treat that "
            "as silence")
    if not isinstance(raw, list):
        raise TranscriptionError("deepgram words was not a list")
    # AUD2-007: an empty word list is only silence when the transcript is also
    # empty. A non-blank transcript with no word timings is a contradiction --
    # the model heard speech but returned no timings -- and treating it as silence
    # lets the caller advance its cursor and retire good exports over real speech.
    if not raw:
        transcript = alternatives[0].get("transcript")
        if isinstance(transcript, str) and transcript.strip():
            raise TranscriptionError(
                "deepgram returned transcript text but no word timings; refusing "
                "to treat that as silence")

    words: list[Word] = []
    previous_start = 0.0
    for position, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise TranscriptionError(
                f"deepgram word entry {position} was not an object")
        if "punctuated_word" in entry:
            text = entry["punctuated_word"]
        elif "word" in entry:
            text = entry["word"]
        else:
            raise TranscriptionError(
                f"deepgram word entry {position} has no text")
        if not isinstance(text, str) or not text.strip():
            raise TranscriptionError(
                f"deepgram word entry {position} has blank or invalid text")
        missing = [key for key in ("start", "end", "confidence")
                   if key not in entry]
        if missing:
            raise TranscriptionError(
                f"deepgram word {text.strip()!r} is missing {', '.join(missing)}")
        start = _response_number(entry["start"], "word start")
        end = _response_number(entry["end"], "word end")
        confidence = _response_number(entry["confidence"], "word confidence")
        if start < 0.0 or end < start:
            raise TranscriptionError(
                f"deepgram word {text.strip()!r} has a nonsensical span "
                f"({start} -> {end})")
        if start + 1e-6 < previous_start:
            raise TranscriptionError(
                f"deepgram word {text.strip()!r} precedes the previous word")
        if not 0.0 <= confidence <= 1.0:
            raise TranscriptionError(
                f"deepgram word {text.strip()!r} has confidence outside 0..1")
        # Our own ffprobe measurement of the file we uploaded outranks the
        # response's self-report, which nova-3 rounds to the second.
        limit = (submitted_duration if submitted_duration is not None
                 else audio_duration)
        if limit is not None:
            if start > limit + WORD_START_TOLERANCE:
                raise TranscriptionError(
                    f"deepgram word {text.strip()!r} starts beyond the "
                    f"{'submitted' if submitted_duration is not None else 'response'}"
                    f" audio duration ({start} > {limit}); the response does not "
                    f"describe the audio that was sent")
            # An overrunning end is an estimate, not extra audio. Keep the word
            # -- it was spoken -- and cap it at the audio it was heard in.
            end = max(start, min(end, limit))
        words.append(Word(
            text=text.strip(),
            start=start,
            duration=max(0.0, end - start),
            confidence=confidence,
        ))
        previous_start = start
    return normalise(words)


def build_provider(config, secret: str) -> ASRProvider:
    name = (config.get("transcription.provider") or "deepgram").lower()
    if name != "deepgram":
        raise TranscriptionError(f"unknown transcription provider: {name}")
    return DeepgramProvider(
        secret,
        model=config.get("transcription.model", "nova-3"),
        language=config.get("transcription.language", "en"),
        filler_words=bool(config.get("transcription.filler_words", True)),
        max_retries=int(config.get("transcription.max_retries", 4)),
        timeout=float(config.get("transcription.request_timeout_seconds", 600)),
    )
