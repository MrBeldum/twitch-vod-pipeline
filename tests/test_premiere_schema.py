"""Validate `premiere.json` against Adobe's published transcript schema.

The schema in `reference/` is Adobe's own file, taken from the spec they attach
to the "Import Your Own Transcript" announcement. It is checked in rather than
paraphrased because the useful parts of it are closed enums -- languages, word
types, tag names -- and paraphrasing them is exactly how we ended up emitting
`zh-cn`, `fi-fi` and `uk-ua`, none of which Adobe accepts.

The validator below is deliberately small and specific: it reads the enums and
required-key sets out of the spec, so replacing the file with a newer one is all
it takes to test against a newer Premiere.
"""

import json
import unittest
from pathlib import Path

from vodpipe.exports import to_premiere_json
from vodpipe.transcript import CensorList, Word

SPEC_PATH = (Path(__file__).resolve().parent.parent / "reference"
             / "PremierePro_transcript_format_spec.json")
SPEC = json.loads(SPEC_PATH.read_text(encoding="utf-8"))

LANGUAGES = set(SPEC["definitions"]["LanguageCode"]["enum"])
WORD_DEF = SPEC["definitions"]["Word"]["properties"]
WORD_TYPES = set(WORD_DEF["type"]["enum"])
WORD_TAGS = set(WORD_DEF["tags"]["items"]["enum"])
WORD_KEYS = set(SPEC["definitions"]["Word"]["required"])
SEGMENT_KEYS = set(SPEC["definitions"]["Segment"]["required"])
ROOT_KEYS = set(SPEC["required"])


def validate(document: dict) -> list[str]:
    """Every way `document` departs from the spec. Empty means it conforms."""
    problems: list[str] = []

    # `additionalProperties: false` at every level, so an extra key is an error
    # and not a harmless annotation.
    if set(document) != ROOT_KEYS:
        problems.append(f"root keys {sorted(document)} != {sorted(ROOT_KEYS)}")
    if document.get("language") not in LANGUAGES:
        problems.append(f"root language {document.get('language')!r} not in enum")
    if not document.get("segments"):
        problems.append("segments must have at least one entry")
    if not document.get("speakers"):
        problems.append("speakers must have at least one entry")

    speaker_ids = {speaker["id"] for speaker in document.get("speakers", [])}
    for index, segment in enumerate(document.get("segments", [])):
        where = f"segment {index}"
        if set(segment) != SEGMENT_KEYS:
            problems.append(f"{where} keys {sorted(segment)}")
            continue
        if segment["language"] not in LANGUAGES:
            problems.append(f"{where} language {segment['language']!r}")
        if segment["speaker"] not in speaker_ids:
            problems.append(f"{where} speaker not declared in speakers[]")
        if segment["duration"] < 0 or segment["start"] < 0:
            problems.append(f"{where} negative start/duration")
        if not segment["words"]:
            problems.append(f"{where} has no words")
        for word in segment["words"]:
            if set(word) != WORD_KEYS:
                problems.append(f"{where} word keys {sorted(word)}")
                continue
            if word["type"] not in WORD_TYPES:
                problems.append(f"{where} word type {word['type']!r}")
            if not 0 <= word["confidence"] <= 1:
                problems.append(f"{where} confidence {word['confidence']}")
            if word["duration"] < 0 or word["start"] < 0:
                problems.append(f"{where} negative word start/duration")
            if not isinstance(word["eos"], bool):
                problems.append(f"{where} eos is not a boolean")
            unknown = set(word["tags"]) - WORD_TAGS
            if unknown:
                problems.append(f"{where} unknown tags {sorted(unknown)}")
    return problems


def speech(*pairs) -> list[Word]:
    """Words at 0.2s intervals, close enough to stay in one segment."""
    words, clock = [], 0.0
    for text in pairs:
        words.append(Word(text, clock, 0.15, 0.9))
        clock += 0.2
    return words


class SchemaConformanceTests(unittest.TestCase):

    def test_the_spec_file_is_the_one_we_think_it_is(self):
        self.assertEqual(SPEC["$id"], "https://schemas.adobe.com/transcript/v1.0.0")
        self.assertEqual(WORD_TAGS, {"profanity", "filler"})
        self.assertEqual(WORD_TYPES, {"word", "punctuation"})

    def test_a_plain_transcript_conforms(self):
        document = json.loads(to_premiere_json(speech("Hello", "world.")))
        self.assertEqual(validate(document), [])

    def test_a_tagged_transcript_conforms(self):
        words = speech("um", "that", "was", "shit.")
        document = json.loads(to_premiere_json(
            words, "en", CensorList(["shit"])))
        self.assertEqual(validate(document), [])

    def test_every_language_we_can_emit_conforms(self):
        """Whatever the operator types into Settings, the file must still import."""
        for configured in ("en", "en-gb", "en-au", "fr-ca", "pt-ao", "zh-cn",
                           "zh-tw", "ko", "sw", "mt-mt", "no", "tl"):
            document = json.loads(to_premiere_json(speech("Hello."), configured))
            self.assertEqual(validate(document), [], configured)


class WordTagTests(unittest.TestCase):
    """`profanity` is the only tag this pipeline emits.

    Automatic filler tagging was removed on 2026-08-17. Premiere reported "no
    filler words detected" against transcripts that carried the tags, and even
    working it would have cut on word boundaries rather than where a human
    would. The words themselves are still transcribed -- see
    `test_fillers_are_transcribed_as_ordinary_words` -- so an editor can find
    and remove one individually.
    """

    def tagged(self, document, name):
        return [word["text"] for segment in document["segments"]
                for word in segment["words"] if name in word["tags"]]

    def test_no_word_is_ever_tagged_filler(self):
        words = speech("um", "I", "uh", "think,", "like,", "so.")
        document = json.loads(to_premiere_json(
            words, "en", CensorList(["shit"])))
        self.assertEqual(self.tagged(document, "filler"), [])

    def test_fillers_are_transcribed_as_ordinary_words(self):
        """Removing the tags must not remove the speech.

        Deepgram still receives `filler_words=true`, so the transcript stays
        verbatim and a cut made from the text lands where the editor expects.
        """
        words = speech("um", "I", "uh", "think", "so.")
        document = json.loads(to_premiere_json(words))
        spoken = [word["text"] for segment in document["segments"]
                  for word in segment["words"]]
        self.assertEqual(spoken, ["um", "I", "uh", "think", "so."])

    def test_profanity_is_still_tagged(self):
        words = speech("um", "shit.")
        document = json.loads(to_premiere_json(
            words, "en", CensorList(["shit"])))
        self.assertEqual(self.tagged(document, "profanity"), ["shit."])

    def test_an_untagged_word_carries_an_empty_tag_list(self):
        """Adobe requires the key; only its contents changed."""
        document = json.loads(to_premiere_json(speech("Hello.")))
        word = document["segments"][0]["words"][0]
        self.assertEqual(word["tags"], [])


if __name__ == "__main__":
    unittest.main()
