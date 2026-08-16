"""Everything Premiere and the editor read: Static Transcript JSON, SRT, text, censor list.

The Static Transcript schema and its 0.4s pause rule are ported verbatim from the
C# tool. Premiere will happily import an SRT, but that only gives you captions --
text-based editing needs this schema, with a timing on every individual word.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Protocol, Sequence

from .transcript import (
    PREMIERE_PAUSE_SECONDS,
    CensorList,
    Word,
    ends_sentence,
    publish_text_sets,
    reading_segments,
    reconcile_publication,
    segment_words,
    words_json_text,
)
from .util import LOG, fmt_clock, fmt_srt_time, round3

# Adobe keys speakers by GUID. One speaker, one stable id -- a livestream VOD is a
# single microphone and diarisation would only fragment the transcript panel.
SPEAKER_ID = "631fbbc0-9c02-47c4-bb8c-732c020fa24f"
SPEAKER_NAME = "Speaker 1"

# The exact set Adobe's schema accepts (schemas.adobe.com/transcript/v1.0.0).
# It is a closed enum, and the schema is `additionalProperties: false` throughout,
# so a tag outside this set is not a cosmetic mismatch -- it is an invalid file.
# Our previous list emitted `zh-cn`, `fi-fi`, `he-il`, `uk-ua` and others that are
# not in it.
PREMIERE_LANGUAGE_CODES = frozenset({
    "en-us", "en-gb", "zh-hk", "cmn-hans", "cmn-hant", "es-es", "de-de",
    "fr-fr", "ja-jp", "pt-pt", "pt-br", "ko-kr", "it-it", "ru-ru", "hi-in",
    "nb-no", "sv-se", "nl-nl", "da-dk", "id-id", "th-th", "vi-vn", "ms-my",
    "tr-tr", "pl-pl", "fil-ph", "te-in", "ml-in", "pa-in",
})

# Adobe's own value for "unknown or unsupported language". Having a sanctioned
# way to say "I don't know" is what lets us keep the rule that a transcript is
# never relabelled as English just to make it import.
UNKNOWN_LANGUAGE = "??-??"

# Default region for a bare code. Every value here is in the enum above.
PREMIERE_LANGUAGES = {
    "en": "en-us", "es": "es-es", "fr": "fr-fr", "de": "de-de", "pt": "pt-br",
    "it": "it-it", "nl": "nl-nl", "ja": "ja-jp", "ko": "ko-kr", "ru": "ru-ru",
    "pl": "pl-pl", "sv": "sv-se", "da": "da-dk", "no": "nb-no", "nb": "nb-no",
    "tr": "tr-tr", "hi": "hi-in", "id": "id-id", "vi": "vi-vn", "th": "th-th",
    "ms": "ms-my", "fil": "fil-ph", "tl": "fil-ph", "te": "te-in",
    "ml": "ml-in", "pa": "pa-in", "zh": "cmn-hans", "cmn": "cmn-hans",
}

# Regioned tags Adobe does not list but which are unambiguously a supported
# language. Mapping these keeps the language honest and text-based editing
# working; the alternative is `??-??`, which Premiere cannot edit text against.
PREMIERE_LANGUAGE_ALIASES = {
    "zh-cn": "cmn-hans", "zh-hans": "cmn-hans", "zh-sg": "cmn-hans",
    "zh-tw": "cmn-hant", "zh-hant": "cmn-hant", "zh-mo": "zh-hk",
    "en-au": "en-gb", "en-nz": "en-gb", "en-ie": "en-gb", "en-za": "en-gb",
    "en-in": "en-gb", "en-ca": "en-us",
    "es-mx": "es-es", "es-ar": "es-es", "es-us": "es-es", "es-419": "es-es",
    "fr-ca": "fr-fr", "fr-be": "fr-fr", "fr-ch": "fr-fr",
    "de-at": "de-de", "de-ch": "de-de",
}

# Well-formed BCP-47-ish tag: a language subtag plus optional subtags.
_LANGUAGE_TAG = re.compile(r"^[a-z]{2,3}(-[a-z0-9]{2,8})*$")

# Written last, naming the generation the files beside it belong to. Its presence
# is what makes a published set self-describing rather than merely present.
MANIFEST_NAME = "exports.json"

# The complete set of files a published transcript owns. Anything in here that a
# publish does not produce is removed, so a re-transcription cannot leave an
# older, longer transcript behind for Premiere to import.
PUBLISHED_EXPORTS = (
    "premiere.json",
    "transcript.json",
    "transcript.srt",
    "transcript.txt",
    "censor-words.txt",
)

# Files earlier versions wrote beside a transcript and this one does not. They
# stay owned so a publish deletes any left over from a recording made before
# they were retired; nothing ever writes them again.
#
# `IMPORT.md` repeated the same import instructions in every transcript folder --
# four per session, unchanged -- where README covers them once. `fillers.md` and
# `fillers.json` were the filler tagger's report and its review cache; Premiere
# reads filler tags from `premiere.json` alone and never did anything with either.
RETIRED_EXPORTS = ("IMPORT.md", "fillers.md", "fillers.json")

# Every file whose presence or absence belongs to one transcript generation.
# Rundowns are intentionally separate: they are derived later on another pool.
GENERATION_FILES = ("words.json", *PUBLISHED_EXPORTS, MANIFEST_NAME,
                    *RETIRED_EXPORTS)

_WARNED_LANGUAGES: set[str] = set()


def premiere_language(language: str) -> str:
    """Normalise a language code for Adobe's transcript header.

    Deliberately *not* a fallback to `en-us`. Mapping every unrecognised code onto
    English mislabelled non-English transcripts silently, and Premiere then offered
    English text-based editing over, say, Japanese speech.

    Adobe's enum is closed, so passing an unrecognised tag through unchanged --
    what this used to do -- produced a file that violates the schema rather than
    one Premiere merely renders oddly. The order is: accept a supported tag,
    expand a bare code, resolve a regional variant onto its supported sibling,
    and otherwise emit Adobe's own `??-??` for "unknown or unsupported". Only a
    malformed tag is rejected outright.
    """
    tag = (language or "en").strip().lower().replace("_", "-")
    if not tag:
        return "en-us"
    if tag in PREMIERE_LANGUAGE_CODES or tag == UNKNOWN_LANGUAGE:
        return tag
    if tag in PREMIERE_LANGUAGES:
        return PREMIERE_LANGUAGES[tag]
    if not _LANGUAGE_TAG.match(tag):
        raise ValueError(
            f"{language!r} is not a usable language tag; expected something like "
            "'en', 'en-gb' or 'pt-br'")
    if tag in PREMIERE_LANGUAGE_ALIASES:
        return PREMIERE_LANGUAGE_ALIASES[tag]
    # A regional variant of a language Adobe does support, e.g. `pt-ao`: the
    # language is right even though the region is not one of its choices.
    base = tag.split("-", 1)[0]
    if base in PREMIERE_LANGUAGES:
        resolved = PREMIERE_LANGUAGES[base]
        if tag not in _WARNED_LANGUAGES:
            _WARNED_LANGUAGES.add(tag)
            LOG.warning("Premiere has no transcript language %r; using %r, which "
                        "is the same language", tag, resolved)
        return resolved
    if tag not in _WARNED_LANGUAGES:
        _WARNED_LANGUAGES.add(tag)
        LOG.warning("Premiere does not support transcripts in %r; labelling it "
                    "%s rather than claiming it is English. Text-based editing "
                    "will be unavailable for this transcript.",
                    tag, UNKNOWN_LANGUAGE)
    return UNKNOWN_LANGUAGE


class WordFlagger(Protocol):
    def flag(self, words: Sequence[Word]) -> list[bool]:
        ...


def to_premiere_json(words: Sequence[Word], language: str = "en",
                     censor: WordFlagger | None = None) -> str:
    """Adobe Static Transcript JSON: import path is Text panel > Transcript > ... > Import.

    `tags` is the only thing in this file that Premiere's transcript *filters*
    read, and Adobe's schema allows exactly two values, `profanity` and `filler`.
    Only `profanity` is emitted. Automatic filler tagging was removed in 2026-08:
    Premiere reported "no filler words detected" against tags it was given, and
    even working, *Delete all fillers* makes hard cuts at word boundaries that a
    human would place differently. Fillers are still transcribed as ordinary
    words, so they can be found and cut individually.
    """
    if not words:
        raise ValueError("Adobe's transcript schema requires at least one speech segment")

    profane = censor.flag(words) if censor else [False] * len(words)
    tags_by_id = {
        id(word): (["profanity"] if is_profane else [])
        for word, is_profane in zip(words, profane)
    }
    tag = premiere_language(language)

    segments = []
    for segment in segment_words(words, PREMIERE_PAUSE_SECONDS):
        segments.append({
            "duration": round3(segment.duration),
            "language": tag,
            "speaker": SPEAKER_ID,
            "start": round3(segment.start),
            "words": [
                {
                    "confidence": round3(max(0.0, min(1.0, word.confidence))),
                    "duration": round3(max(0.0, word.duration)),
                    "eos": ends_sentence(word.text),
                    "start": round3(max(0.0, word.start)),
                    "tags": tags_by_id.get(id(word), []),
                    "text": word.text,
                    "type": "word",
                }
                for word in segment.words
            ],
        })

    return json.dumps({
        "language": tag,
        "segments": segments,
        "speakers": [{"id": SPEAKER_ID, "name": SPEAKER_NAME}],
    }, ensure_ascii=False, indent=2)


def to_srt(words: Sequence[Word]) -> str:
    lines = []
    for index, segment in enumerate(reading_segments(words), start=1):
        lines.append(str(index))
        lines.append(f"{fmt_srt_time(segment.start)} --> {fmt_srt_time(segment.end)}")
        lines.append(segment.text)
        lines.append("")
    return "\n".join(lines)


def to_text(words: Sequence[Word], offset: float = 0.0) -> str:
    """Timestamped plain text. Doubles as the summariser's input."""
    return "\n".join(
        f"[{fmt_clock(segment.start + offset)}] {segment.text}"
        for segment in reading_segments(words)
    ) + "\n"


def to_transcript_json(words: Sequence[Word], meta: dict[str, Any]) -> str:
    segments = [
        {
            "start": round3(segment.start),
            "end": round3(segment.end),
            "text": segment.text,
            "words": [word.to_dict() for word in segment.words],
        }
        for segment in reading_segments(words)
    ]
    return json.dumps({**meta, "segments": segments}, ensure_ascii=False, indent=2)


def to_censor_list(words: Sequence[Word], censor: CensorList) -> str:
    """Terms actually spoken in this chunk, ready to paste into Premiere.

    Premiere censors from a list the editor supplies -- nothing inside the
    transcript file drives it -- so this is a separate output by necessity.
    """
    entries = censor.present_in(words)
    if not entries:
        return "# no listed terms occurred in this transcript\n"
    header = [
        "# Terms from your master list that occur in this transcript.",
        "# Paste into Premiere: Text panel > Transcript > Filter > Censored words.",
        "",
    ]
    return "\n".join(header + [term for term, _ in entries]) + "\n"


def write_exports(
    directory: Path,
    words: Sequence[Word],
    *,
    language: str = "en",
    censor: CensorList | None = None,
    meta: dict[str, Any] | None = None,
    words_meta: dict[str, Any] | None = None,
) -> list[str]:
    """Publish the export set for `words`. Returns the file names written.

    The publish is a replacement, not an addition: any file in `PUBLISHED_EXPORTS`
    this call does not produce is removed. Without that, re-transcribing a chunk to
    a legitimately empty result left the previous `premiere.json` in place and
    Premiere went on importing a transcript for audio that no longer had any.

    Only reached on a *successful* transcription pass. A failed pass never calls
    this, so the last good outputs survive a provider outage untouched -- that
    asymmetry is the point, and `retranscribe` restores them if a rebuild fails
    part way through.
    """
    return write_export_sets([(
        directory,
        words,
        {
            "language": language,
            "censor": censor,
            "meta": meta,
            "words_meta": words_meta,
        },
    )])[0]


def write_export_sets(
    publications: Sequence[tuple[Path, Sequence[Word], dict[str, Any]]],
) -> list[list[str]]:
    """Render and transactionally publish one or more transcript generations."""
    prepared: list[tuple[Path, dict[str, str], Sequence[str]]] = []
    results: list[list[str]] = []
    for directory, words, options in publications:
        rendered, written = _render_generation(
            words,
            language=str(options.get("language", "en")),
            censor=options.get("censor"),
            meta=options.get("meta"),
            words_meta=options.get("words_meta"),
        )
        prepared.append((directory, rendered, GENERATION_FILES))
        results.append(written)
    publish_text_sets(prepared)
    return results


def _render_generation(
    words: Sequence[Word],
    *,
    language: str,
    censor: CensorList | None,
    meta: dict[str, Any] | None,
    words_meta: dict[str, Any] | None,
) -> tuple[dict[str, str], list[str]]:
    """Render every byte of a generation before its transaction begins."""
    meta = dict(meta or {})
    meta.setdefault("word_count", len(words))

    persisted = dict(words_meta or {})
    persisted.setdefault("language", language)
    # Keep the identity fields needed to derive the generation in words.json,
    # which is the durable source from which recovery rebuilds every export.
    for key in ("channel", "session_id", "chunk", "session_offset", "source"):
        if key in meta:
            persisted.setdefault(key, meta[key])
    persisted.setdefault("complete", bool(meta.get("complete")))
    inferred = max((word.end for word in words), default=0.0)
    persisted.setdefault("covered_seconds", round3(inferred))
    persisted.setdefault("expected_seconds",
                          round3(float(persisted["covered_seconds"])))
    language = str(persisted["language"])
    generation = generation_id(words, language, persisted)
    persisted["generation"] = generation
    meta["generation"] = generation

    # Every output, including words and the manifest, is rendered before any
    # canonical target is replaced, so they are all part of exactly the same
    # generation.
    if not words:
        exports = {
            "transcript.txt": "# no speech was transcribed for this chunk\n",
        }
    else:
        exports = {
            "premiere.json": to_premiere_json(words, language, censor),
            "transcript.json": to_transcript_json(words, meta),
            "transcript.srt": to_srt(words),
            "transcript.txt": to_text(words,
                                       float(meta.get("session_offset", 0.0))),
        }
        if censor:
            exports["censor-words.txt"] = to_censor_list(words, censor)

    written = ["words.json", *exports]
    rendered = {
        "words.json": words_json_text(words, persisted),
        **exports,
        MANIFEST_NAME: json.dumps({
            "generation": generation,
            "files": written,
            "word_count": len(words),
            "language": language,
        }, indent=2) + "\n",
    }
    return rendered, written


def generation_id(words: Sequence[Word], language: str,
                   meta: dict[str, Any]) -> str:
    """A stable fingerprint of the transcript this export set describes.

    Written into `transcript.json` and the manifest so an output can be matched
    back to the words it came from -- which is what tells a rundown whether the
    transcript underneath it has been replaced since it was generated.
    """
    digest = hashlib.sha256()
    # Word bytes alone are not a transcript generation. The same tokens can be
    # produced from a different logical track/model, can cover a different span,
    # or can move on the session timeline. Rundown job keys use this identity, so
    # omitting those fields lets stale work deduplicate the replacement.
    identity = {
        "language": language,
        "channel": meta.get("channel"),
        "session_id": meta.get("session_id"),
        "chunk": meta.get("chunk"),
        "session_offset": meta.get("session_offset"),
        "source": meta.get("source"),
        "asr_identity": meta.get("asr_identity"),
        "complete": meta.get("complete"),
        "covered_seconds": meta.get("covered_seconds"),
        "expected_seconds": meta.get("expected_seconds"),
    }
    digest.update(json.dumps(
        identity, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False,
    ).encode("utf-8"))
    for word in words:
        digest.update(
            f"{word.text}\x1f{word.start:.3f}\x1f{word.duration:.3f}\x1f"
            f"{word.confidence:.3f}\x1e".encode("utf-8"))
    return digest.hexdigest()[:16]


def canonical_generation(words: Sequence[Word],
                          words_meta: dict[str, Any]) -> str:
    """Derive the export generation only from strict words.json content."""
    return generation_id(
        words,
        str(words_meta.get("language") or "en"),
        words_meta,
    )


def publication_is_consistent(directory: Path, words: Sequence[Word],
                              words_meta: dict[str, Any]) -> bool:
    """Whether markerless canonical exports all describe these stored words.

    Optional outputs are authoritative only when the manifest declares them.
    Recovery therefore neither invents a currently configured optional file nor
    republishes a valid generation because the censor list is absent.
    """
    reconcile_publication(directory)
    canonical = canonical_generation(words, words_meta)
    if words_meta.get("generation") != canonical:
        return False

    try:
        payload = json.loads(
            (directory / MANIFEST_NAME).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(payload, dict):
        return False
    if payload.get("generation") != canonical:
        return False
    if (isinstance(payload.get("word_count"), bool)
            or payload.get("word_count") != len(words)):
        return False
    language = str(words_meta.get("language") or "en")
    if payload.get("language") != language:
        return False

    declared_value = payload.get("files")
    if (not isinstance(declared_value, list)
            or any(not isinstance(name, str) for name in declared_value)
            or len(set(declared_value)) != len(declared_value)):
        return False
    declared = set(declared_value)
    canonical_names = {"words.json", *PUBLISHED_EXPORTS}
    if not declared.issubset(canonical_names):
        return False
    actual = {name for name in canonical_names
              if (directory / name).is_file()}
    if declared != actual:
        return False

    required = {"words.json", "transcript.txt"}
    if words:
        required.update({"premiere.json", "transcript.json", "transcript.srt"})
    if not required.issubset(declared):
        return False

    if words:
        try:
            transcript = json.loads(
                (directory / "transcript.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        if (not isinstance(transcript, dict)
                or transcript.get("generation") != canonical):
            return False
        # P3: premiere.json is the file Premiere actually imports, so validate its
        # structure too -- not just its presence and hash. A truncated or
        # mis-generated Static Transcript that still hashes into the manifest would
        # otherwise be adopted as a healthy generation and then fail to import.
        try:
            premiere = json.loads(
                (directory / "premiere.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        if (not isinstance(premiere, dict)
                or premiere.get("language") != premiere_language(language)
                or not isinstance(premiere.get("segments"), list)
                or not premiere.get("segments")
                or not isinstance(premiere.get("speakers"), list)
                or not premiere.get("speakers")):
            return False
    return True


def read_manifest(directory: Path) -> dict[str, Any]:
    """The manifest for the export set in `directory`, or {} if there is none."""
    reconcile_publication(directory)
    try:
        payload = json.loads((directory / MANIFEST_NAME).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


