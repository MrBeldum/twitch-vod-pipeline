"""Tests for the Premiere-facing exports."""

import json
import tempfile
import unittest
from pathlib import Path

from vodpipe.exports import (
    PREMIERE_LANGUAGE_CODES,
    SPEAKER_ID,
    generation_id,
    premiere_language,
    to_censor_list,
    to_premiere_json,
    to_srt,
    to_text,
    write_exports,
)
from vodpipe.transcript import CensorList, Word


def sample_words():
    return [
        Word("Hello", 0.0, 0.40, 0.99),
        Word("there.", 0.45, 0.35, 0.97),
        # A 1.2s pause: Premiere must start a new segment here.
        Word("Damn", 2.00, 0.30, 0.91),
        Word("it", 2.35, 0.20, 0.88),
        Word("worked!", 2.60, 0.50, 0.95),
    ]


class PremiereJsonTests(unittest.TestCase):
    def setUp(self):
        self.payload = json.loads(to_premiere_json(sample_words(), "en",
                                                   CensorList(["damn"])))

    def test_top_level_shape(self):
        self.assertEqual(self.payload["language"], "en-us")
        self.assertEqual(self.payload["speakers"],
                         [{"id": SPEAKER_ID, "name": "Speaker 1"}])

    def test_pause_starts_a_new_segment(self):
        self.assertEqual(len(self.payload["segments"]), 2)
        self.assertEqual(len(self.payload["segments"][0]["words"]), 2)
        self.assertEqual(len(self.payload["segments"][1]["words"]), 3)

    def test_every_word_carries_the_required_keys(self):
        required = {"confidence", "duration", "eos", "start", "tags", "text", "type"}
        for segment in self.payload["segments"]:
            for word in segment["words"]:
                self.assertEqual(set(word), required)
                self.assertEqual(word["type"], "word")
                self.assertGreater(word["duration"], 0)

    def test_end_of_sentence_flag(self):
        flags = {word["text"]: word["eos"]
                 for segment in self.payload["segments"] for word in segment["words"]}
        self.assertTrue(flags["there."])
        self.assertTrue(flags["worked!"])
        self.assertFalse(flags["Hello"])

    def test_profanity_is_tagged(self):
        tags = {word["text"]: word["tags"]
                for segment in self.payload["segments"] for word in segment["words"]}
        self.assertEqual(tags["Damn"], ["profanity"])
        self.assertEqual(tags["Hello"], [])

    def test_segment_bounds_match_their_words(self):
        for segment in self.payload["segments"]:
            first, last = segment["words"][0], segment["words"][-1]
            self.assertAlmostEqual(segment["start"], first["start"], places=3)
            self.assertAlmostEqual(segment["duration"],
                                   round(last["start"] + last["duration"]
                                         - first["start"], 3), places=2)

    def test_empty_input_is_rejected(self):
        with self.assertRaises(ValueError):
            to_premiere_json([], "en")


class LanguageTagTests(unittest.TestCase):
    """AUD-035: every unrecognised language used to be relabelled as en-us.

    Premiere then offered English text-based editing over speech that was not
    English, and nothing anywhere said so.
    """

    def test_a_bare_code_gains_its_usual_region(self):
        self.assertEqual(premiere_language("en"), "en-us")
        self.assertEqual(premiere_language("pt"), "pt-br")
        self.assertEqual(premiere_language("ja"), "ja-jp")

    def test_a_supported_regioned_tag_is_kept_as_given(self):
        self.assertEqual(premiere_language("en-gb"), "en-gb")
        self.assertEqual(premiere_language("pt-pt"), "pt-pt")

    def test_case_and_underscores_are_normalised(self):
        self.assertEqual(premiere_language("EN_GB"), "en-gb")
        self.assertEqual(premiere_language("  De  "), "de-de")

    def test_an_unsupported_region_resolves_to_the_same_language(self):
        """Adobe's enum is closed, so `fr-ca` is an invalid file, not a quirk.

        Falling back to the language's supported region keeps the transcript
        honest about what was spoken and keeps text-based editing working.
        """
        self.assertEqual(premiere_language("fr-ca"), "fr-fr")
        self.assertEqual(premiere_language("es-mx"), "es-es")
        self.assertEqual(premiere_language("en-au"), "en-gb")
        self.assertEqual(premiere_language("zh-tw"), "cmn-hant")
        # A region Adobe does not list and we do not alias: still Portuguese.
        self.assertEqual(premiere_language("pt-ao"), "pt-br")

    def test_an_unlisted_language_is_never_relabelled_as_english(self):
        """The regression, stated plainly.

        `??-??` is Adobe's own value for an unsupported language, so saying "I
        do not know" is now expressible without either lying or writing a tag
        that fails the schema.
        """
        for code in ("sw", "is", "mt-mt"):
            self.assertEqual(premiere_language(code), "??-??", code)

    def test_every_tag_produced_is_one_adobe_accepts(self):
        """The property that actually matters: the file must validate."""
        allowed = set(PREMIERE_LANGUAGE_CODES) | {"??-??"}
        for code in ("en", "en-gb", "en-au", "fr-ca", "pt", "pt-ao", "zh",
                     "zh-cn", "zh-tw", "sw", "is", "mt-mt", "fil", "tl",
                     "no", "nb", "cmn-hant", "", "EN_GB"):
            self.assertIn(premiere_language(code), allowed, code)

    def test_a_malformed_tag_is_rejected(self):
        for bad in ("english", "e", "en-", "12", "en gb"):
            with self.assertRaises(ValueError, msg=bad):
                premiere_language(bad)

    def test_blank_falls_back_to_english(self):
        self.assertEqual(premiere_language(""), "en-us")
        self.assertEqual(premiere_language(None), "en-us")

    def test_the_tag_reaches_the_published_json(self):
        payload = json.loads(to_premiere_json(sample_words(), "ja"))
        self.assertEqual(payload["language"], "ja-jp")
        self.assertTrue(all(word["type"] == "word"
                            for segment in payload["segments"]
                            for word in segment["words"]))


class TextExportTests(unittest.TestCase):
    def test_srt_is_well_formed(self):
        srt = to_srt(sample_words())
        self.assertTrue(srt.startswith("1\n"))
        self.assertIn(" --> ", srt)
        self.assertRegex(srt, r"\d{2}:\d{2}:\d{2},\d{3}")

    def test_text_is_timestamped(self):
        self.assertTrue(to_text(sample_words()).startswith("[00:00:00]"))

    def test_text_offset_shifts_the_clock(self):
        self.assertTrue(to_text(sample_words(), 3600).startswith("[01:00:00]"))

    def test_censor_list_reports_only_what_occurred(self):
        listing = to_censor_list(sample_words(), CensorList(["damn", "unused"]))
        self.assertIn("damn", listing)
        self.assertNotIn("unused", listing)

    def test_censor_list_when_nothing_matched(self):
        self.assertIn("no listed terms", to_censor_list(sample_words(), CensorList([])))


class WriteExportsTests(unittest.TestCase):
    def test_full_set_is_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            written = write_exports(directory, sample_words(),
                                    censor=CensorList(["damn"]),
                                    meta={"chunk": "c001"})
            for name in ("premiere.json", "transcript.json", "transcript.srt",
                         "transcript.txt", "censor-words.txt"):
                self.assertIn(name, written)
                self.assertTrue((directory / name).exists(), name)
            # Must be valid JSON on disk, not just in memory.
            json.loads((directory / "premiere.json").read_text(encoding="utf-8"))

    def test_no_speech_still_leaves_an_explanation(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            write_exports(directory, [])
            self.assertFalse((directory / "premiere.json").exists())
            self.assertIn("no speech", (directory / "transcript.txt").read_text())


class GenerationIdentityTests(unittest.TestCase):
    def base_meta(self):
        return {
            "channel": "chan", "session_id": "session", "chunk": "c001",
            "session_offset": 10.0, "source": "chan_c001.mp4",
            "complete": True, "covered_seconds": 10.0,
            "expected_seconds": 10.0,
            "asr_identity": {
                "provider": "deepgram", "model": "nova-3",
                "language": "en", "filler_words": True,
                "audio_stream": {
                    "ordinal": 0, "codec": "aac", "language": "eng",
                    "channels": 2, "layout": "stereo", "default": True,
                },
            },
        }

    def test_byte_identical_words_differ_across_semantic_generations(self):
        words = [Word("same", 0.0, 0.4, 0.9)]
        baseline = self.base_meta()
        original = generation_id(words, "en", baseline)
        changes = (
            ("provider", "other"), ("model", "nova-2"),
            ("language", "fr"), ("filler_words", False),
        )
        for key, value in changes:
            with self.subTest(key=key):
                changed = self.base_meta()
                changed["asr_identity"][key] = value
                self.assertNotEqual(generation_id(words, "en", changed), original)

        changed = self.base_meta()
        changed["asr_identity"]["audio_stream"]["ordinal"] = 1
        self.assertNotEqual(generation_id(words, "en", changed), original)

    def test_coverage_source_and_session_position_are_generation_identity(self):
        words = [Word("same", 0.0, 0.4, 0.9)]
        original = generation_id(words, "en", self.base_meta())
        for key, value in (
                ("complete", False), ("covered_seconds", 9.0),
                ("expected_seconds", 11.0), ("session_offset", 20.0),
                ("source", "different.mp4")):
            with self.subTest(key=key):
                changed = self.base_meta()
                changed[key] = value
                self.assertNotEqual(generation_id(words, "en", changed), original)


if __name__ == "__main__":
    unittest.main()
