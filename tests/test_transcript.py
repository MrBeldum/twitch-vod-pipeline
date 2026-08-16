"""Unit tests for the transcript model: seams, segmentation, censor matching."""

import unittest

from vodpipe.transcript import (
    CensorList,
    Word,
    merge_streams,
    normalise,
    reading_segments,
    segment_words,
)


def words(*specs):
    """Build a word stream from (text, start, duration) triples."""
    return [Word(text, start, duration) for text, start, duration in specs]


class MergeStreamsTests(unittest.TestCase):
    def test_empty_sides(self):
        stream = words(("a", 0.0, 0.4))
        self.assertEqual([w.text for w in merge_streams([], stream, 0.0)], ["a"])
        self.assertEqual([w.text for w in merge_streams(stream, [], 0.0)], ["a"])

    def test_overlap_is_not_duplicated(self):
        # The tail of the first slice is re-transcribed by the second.
        existing = words(("one", 0.0, 0.4), ("two", 1.0, 0.4), ("three", 2.0, 0.4))
        incoming = words(("two", 1.0, 0.4), ("three", 2.0, 0.4), ("four", 3.0, 0.4))
        merged = merge_streams(existing, incoming, overlap_start=1.0)
        self.assertEqual([w.text for w in merged], ["one", "two", "three", "four"])

    def test_seam_lands_in_the_widest_pause(self):
        existing = words(("a", 0.0, 0.3), ("b", 0.4, 0.3))
        # A long silence between "b" and "c" is the natural place to cut.
        incoming = words(("b", 0.4, 0.3), ("c", 3.0, 0.3), ("d", 3.5, 0.3))
        merged = merge_streams(existing, incoming, overlap_start=0.4)
        self.assertEqual([w.text for w in merged], ["a", "b", "c", "d"])

    def test_continuous_speech_across_the_seam_keeps_every_word_once(self):
        existing = words(("a", 0.0, 0.3), ("b", 0.3, 0.3), ("c", 0.6, 0.3))
        incoming = words(("b", 0.3, 0.3), ("c", 0.6, 0.3), ("d", 0.9, 0.3))
        merged = merge_streams(existing, incoming, overlap_start=0.3)
        self.assertEqual([w.text for w in merged], ["a", "b", "c", "d"])

    def test_disjoint_slices_are_concatenated(self):
        existing = words(("a", 0.0, 0.3))
        incoming = words(("z", 10.0, 0.3))
        merged = merge_streams(existing, incoming, overlap_start=10.0)
        self.assertEqual([w.text for w in merged], ["a", "z"])

    def test_no_word_is_split_by_the_seam(self):
        existing = words(("hello", 0.0, 0.5), ("wor", 1.0, 0.2))
        incoming = words(("world", 1.0, 0.6), ("again", 2.0, 0.4))
        merged = merge_streams(existing, incoming, overlap_start=0.9)
        self.assertEqual([w.text for w in merged], ["hello", "world", "again"])

    def test_normalized_token_jitter_does_not_duplicate_a_short_word(self):
        existing = words(("Hello,", 1.0, 0.04))
        incoming = words(("hello", 1.15, 0.04), ("next", 1.5, 0.2))
        merged = merge_streams(existing, incoming, overlap_start=0.9)
        self.assertEqual([w.text for w in merged], ["hello", "next"])

    def test_unrelated_words_that_overlap_in_time_both_survive(self):
        existing = words(("alpha", 1.0, 0.3))
        incoming = words(("beta", 1.1, 0.3))
        merged = merge_streams(existing, incoming, overlap_start=0.9)
        self.assertEqual([w.text for w in merged], ["alpha", "beta"])

    def test_equal_tokens_outside_the_drift_bound_are_not_aligned(self):
        existing = words(("same", 1.0, 0.1))
        incoming = words(("same", 2.0, 0.1))
        merged = merge_streams(existing, incoming, overlap_start=0.9)
        self.assertEqual([w.text for w in merged], ["same", "same"])


class NormaliseTests(unittest.TestCase):
    def test_overlapping_timings_are_trimmed(self):
        result = normalise(words(("a", 0.0, 2.0), ("b", 1.0, 0.5)))
        self.assertLessEqual(result[0].end, result[1].start + 1e-6)

    def test_zero_length_words_get_a_floor(self):
        result = normalise(words(("a", 1.0, 0.0)))
        self.assertGreater(result[0].duration, 0.0)

    def test_blank_words_are_dropped(self):
        self.assertEqual(normalise(words(("  ", 0.0, 0.3), ("x", 1.0, 0.3))).__len__(), 1)


class SegmentationTests(unittest.TestCase):
    def test_splits_at_the_premiere_pause(self):
        # 0.5s gap exceeds the 0.4s rule; 0.1s does not.
        stream = words(("a", 0.0, 0.3), ("b", 0.4, 0.3), ("c", 1.2, 0.3))
        segments = segment_words(stream, 0.4)
        self.assertEqual([len(seg.words) for seg in segments], [2, 1])

    def test_reading_segments_cap_length(self):
        stream = [Word(f"w{i}", i * 0.2, 0.15) for i in range(60)]
        for segment in reading_segments(stream):
            self.assertLessEqual(len(segment.words), 14)

    def test_segment_text_and_bounds(self):
        segments = segment_words(words(("Hi", 1.0, 0.5), ("there.", 1.6, 0.4)), 0.4)
        self.assertEqual(segments[0].text, "Hi there.")
        self.assertAlmostEqual(segments[0].start, 1.0)
        self.assertAlmostEqual(segments[0].end, 2.0)


class CensorListTests(unittest.TestCase):
    def setUp(self):
        self.censor = CensorList(["# comment", "damn", "porch monkey", "hoe", ""])

    def test_exact_match(self):
        self.assertEqual(self.censor.matches("Damn"), "damn")
        self.assertEqual(self.censor.matches("damn,"), "damn")

    def test_innocent_words_are_left_alone(self):
        for word in ("assess", "Scunthorpe", "niggardly", "shoe", "damnation"):
            self.assertIsNone(self.censor.matches(word), word)

    def test_stems_catch_inflections_and_compounds(self):
        self.assertIsNotNone(self.censor.matches("shitting"))
        self.assertIsNotNone(self.censor.matches("clusterfuck"))

    def test_phrases_flag_every_word_they_span(self):
        stream = words(("the", 0.0, 0.2), ("porch", 0.3, 0.2), ("monkey", 0.6, 0.2))
        self.assertEqual(self.censor.flag(stream), [False, True, True])

    def test_phrase_appears_in_the_export(self):
        stream = words(("porch", 0.0, 0.2), ("monkey", 0.3, 0.2), ("damn", 1.0, 0.2))
        terms = dict(self.censor.present_in(stream))
        self.assertIn("porch monkey", terms)
        self.assertEqual(terms["damn"], 1)

    def test_a_phrase_split_by_a_long_silence_is_not_a_phrase(self):
        """AUD-035: adjacency in the word list says nothing about time.

        With nothing said in between, two words either side of a ten-minute
        silence are neighbours in the stream -- and were read as a phrase.
        """
        censor = CensorList(["dead body"])
        stream = words(("dead", 4.0, 0.3), ("body", 604.0, 0.3))
        self.assertEqual(censor.flag(stream), [False, False])
        self.assertEqual(censor.present_in(stream), [])

    def test_the_same_phrase_spoken_normally_still_matches(self):
        censor = CensorList(["dead body"])
        stream = words(("dead", 4.0, 0.3), ("body", 4.4, 0.3))
        self.assertEqual(censor.flag(stream), [True, True])
        self.assertEqual(dict(censor.present_in(stream)), {"dead body": 1})

    def test_a_natural_pause_inside_a_phrase_is_allowed(self):
        censor = CensorList(["porch monkey"])
        stream = words(("porch", 0.0, 0.3), ("monkey", 0.9, 0.3))
        self.assertEqual(censor.flag(stream), [True, True])

    def test_the_gap_limit_is_adjustable(self):
        strict = CensorList(["dead body"], phrase_max_gap=0.1)
        stream = words(("dead", 0.0, 0.3), ("body", 0.9, 0.3))
        self.assertEqual(strict.flag(stream), [False, False])

    def test_a_roots_only_list_is_not_empty(self):
        """It can still flag things, so treating it as empty skipped its export."""
        censor = CensorList([])
        self.assertTrue(censor)
        self.assertIsNotNone(censor.matches("clusterfuck"))

    def test_a_list_with_no_terms_and_no_roots_is_empty(self):
        self.assertFalse(CensorList([], roots=()))

    def test_real_master_list_loads(self):
        from pathlib import Path
        path = Path.home() / "Desktop" / "censored_words_master.txt"
        if not path.exists():
            self.skipTest("master list not present on this machine")
        censor = CensorList.load(path)
        self.assertGreater(len(censor.exact), 100)
        self.assertGreater(len(censor.phrases), 5)
        # Comment lines must not become censor terms.
        self.assertFalse(any(term.startswith("#") for term in censor.exact))


if __name__ == "__main__":
    unittest.main()
