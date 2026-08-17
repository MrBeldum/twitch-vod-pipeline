"""The automatic cut: what it removes, what it must never remove, and sync.

The contracts here fall into three groups, in descending order of how much a
regression would cost:

1. **Nothing that was spoken may be clipped.** Every rule that removes anything
   is checked against the transcript, because a cut that eats half a word is a
   defect the viewer hears and cannot be repaired without re-rendering.
2. **The audio and the video must describe the same timeline.** They are cut by
   two different mechanisms -- ffmpeg's frame `select` and a sample-level
   assembly -- and the only thing keeping them together is that the sample
   counts are *derived from* the frame counts. `SyncArithmeticTests` pins that.
3. **The plan must be reproducible.** It is pure, so the same envelope and the
   same words always give the same cut list.

Numbers quoted in the assertions come from the 7-hour, 60,693-word reference
recording; the rules were calibrated against it rather than guessed.
"""

from __future__ import annotations

import json
import math
import shutil
import tempfile
import unittest
import wave
from array import array
from pathlib import Path

from vodpipe.audio import AudioAssemblyError, assemble, segments_for
from vodpipe.edit import (
    FILLER,
    REPEAT,
    EditOptions,
    EditRefused,
    Mute,
    filler_spans,
    merge_ranges,
    mute_spans,
    plan_edit,
    remap_mutes,
    remap_words,
    render_report,
    repeat_spans,
    silence_spans,
    subtract_range,
)
from vodpipe.media import keep_expression, parse_envelope
from vodpipe.transcript import (
    CensorList,
    CorruptWordsFile,
    Word,
    words_from_json,
    words_json_text,
    words_to_json,
)
from vodpipe.util import Tools


def _tools() -> Tools:
    return Tools(ffmpeg="ffmpeg", ffprobe="ffprobe", streamlink="", claude="")


def envelope(pattern: str, hop: float = 0.010) -> list[float]:
    """`#` is speech, `.` is silence; one character per `hop`."""
    return [-20.0 if character == "#" else -80.0 for character in pattern]


def speech(*pairs: tuple[str, float, float]) -> list[Word]:
    return [Word(text, start, end - start, 0.9) for text, start, end in pairs]


def words_at(texts: str, start: float = 0.0, step: float = 0.30) -> list[Word]:
    """Contiguous words, the way Deepgram actually reports them: zero gap."""
    out, clock = [], start
    for text in texts.split():
        out.append(Word(text, clock, step, 0.9))
        clock += step
    return out


class SilenceTests(unittest.TestCase):
    def test_only_runs_longer_than_the_minimum_are_silence(self):
        levels = envelope("####" + "." * 10 + "####" + "." * 30 + "####")
        found = silence_spans(levels, 0.010, -41.0, 0.200)
        self.assertEqual([(round(a, 3), round(b, 3)) for a, b in found],
                         [(0.180, 0.480)])

    def test_a_trailing_silence_still_counts(self):
        levels = envelope("####" + "." * 40)
        found = silence_spans(levels, 0.010, -41.0, 0.200)
        self.assertEqual(len(found), 1)
        self.assertAlmostEqual(found[0][1], 0.440, places=3)

    def test_the_threshold_is_a_strict_comparison(self):
        self.assertEqual(silence_spans([-41.0] * 40, 0.010, -41.0, 0.200), [])
        self.assertEqual(len(silence_spans([-41.1] * 40, 0.010, -41.0, 0.200)), 1)

    def test_digital_silence_parses_as_a_reading_not_a_failure(self):
        text = ("frame:0 pts_time:0\n"
                "lavfi.astats.Overall.RMS_level=-23.5\n"
                "frame:1 pts_time:0.01\n"
                "lavfi.astats.Overall.RMS_level=-inf\n")
        self.assertEqual(parse_envelope(text), [-23.5, -120.0])


class WordProtectionTests(unittest.TestCase):
    """The rule the whole feature stands on: a cut may not touch speech.

    On a five-minute sample of the reference recording, acoustic cuts alone
    clipped 37 of 701 words, the worst by 230 ms -- Deepgram pads a word to abut
    its neighbour, so a word's reported span routinely reaches into audio that
    really is silent. Trimming to the acoustics alone is therefore not safe, and
    this is the guard that makes it so.
    """

    def plan(self, levels, words, **overrides):
        options = EditOptions(fillers="off", repeats="off", censor="off",
                              **overrides)
        return plan_edit(words, levels, 0.010, len(levels) * 0.010, options)

    def test_a_keep_range_grows_to_contain_a_word_the_audio_called_quiet(self):
        # 0.30-0.70s reads as silence, but a word was transcribed across it.
        levels = envelope("#" * 30 + "." * 40 + "#" * 30)
        plan = self.plan(levels, speech(("mumbled", 0.25, 0.75)))
        for word in speech(("mumbled", 0.25, 0.75)):
            self.assertTrue(
                any(a <= word.start and word.end <= b for a, b in plan.keep),
                f"{plan.keep} does not fully contain the word")

    def test_no_word_is_ever_partially_kept(self):
        levels = envelope(("#" * 20 + "." * 60) * 6)
        words = [Word(f"w{index}", 0.75 * index, 0.22, 0.9) for index in range(6)]
        plan = self.plan(levels, words)
        for word in words:
            covering = [(a, b) for a, b in plan.keep
                        if a < word.end and b > word.start]
            self.assertTrue(covering, f"{word.text} was dropped entirely")
            self.assertTrue(
                any(a - 1e-4 <= word.start and word.end <= b + 1e-4
                    for a, b in covering),
                f"{word.text} at {word.start}-{word.end} was clipped by "
                f"{covering}")

    def test_a_short_island_is_dropped_unless_it_holds_a_word(self):
        levels = envelope("#" * 60 + "." * 60 + "#" * 8 + "." * 60 + "#" * 60)
        bare = self.plan(levels, [], margin_seconds=0.0)
        self.assertEqual(len(bare.keep), 2, "a 0.08s click should be dropped")

        held = self.plan(levels, speech(("yes", 1.20, 1.28)), margin_seconds=0.0)
        self.assertEqual(len(held.keep), 3,
                         "an island holding a transcribed word must survive")

    def test_the_margin_is_applied_on_both_sides(self):
        levels = envelope("." * 60 + "#" * 60 + "." * 60)
        plan = self.plan(levels, [], margin_seconds=0.200)
        (start, end), = plan.keep
        self.assertAlmostEqual(start, 0.400, places=3)
        self.assertAlmostEqual(end, 1.400, places=3)


class FillerTests(unittest.TestCase):
    def test_sounds_removes_hesitation_only(self):
        words = words_at("so uh I um think ah so")
        found = filler_spans(words, "sounds")
        self.assertEqual([item.detail for item in found], ["uh", "um", "ah"])

    def test_affirmations_are_never_removed(self):
        """"mhmm" and "uh-huh" are answers. Deleting one changes what was said,
        and Deepgram groups them under the same filler_words option."""
        words = words_at("mhmm uh-huh nuh-uh huh")
        self.assertEqual(filler_spans(words, "smart"), [])

    def test_filler_roots_do_not_match_inside_real_words(self):
        words = words_at("under until umbrella erase ahead")
        self.assertEqual(filler_spans(words, "smart"), [])

    def test_smart_removes_a_parenthetical_marker(self):
        words = speech(("about,", 0.0, 0.3), ("like,", 0.3, 0.6),
                       ("everything", 0.6, 1.2))
        found = filler_spans(words, "smart")
        self.assertEqual([item.detail for item in found], ["like,"])

    def test_smart_keeps_a_sentence_initial_marker(self):
        """Removing the "Actually," that opens a sentence changes its emphasis;
        on the reference recording only 3 of 86 "actually"s were parenthetical
        and every one of those was sentence-initial."""
        words = speech(("right.", 0.0, 0.3), ("Actually,", 0.3, 0.9),
                       ("no", 0.9, 1.2))
        self.assertEqual(filler_spans(words, "smart"), [])

    def test_smart_keeps_like_used_as_a_verb(self):
        words = words_at("I like it well enough")
        self.assertEqual(filler_spans(words, "smart"), [])

    def test_off_removes_nothing(self):
        self.assertEqual(filler_spans(words_at("uh um er"), "off"), [])

    def test_an_unknown_mode_is_rejected_rather_than_guessed(self):
        with self.assertRaises(ValueError):
            filler_spans(words_at("uh"), "aggressive")


class RepeatTests(unittest.TestCase):
    def texts(self, words, found):
        removed = {round(item.start, 3) for item in found}
        return [w.text for w in words if round(w.start, 3) in removed]

    def test_a_stutter_loses_every_copy_but_the_last(self):
        words = words_at("it's it's it's undefeated")
        found = repeat_spans(words, "stutters")
        self.assertEqual(len(found), 1, "one contiguous run is one span")
        self.assertAlmostEqual(found[0].start, 0.0, places=3)
        self.assertAlmostEqual(found[0].end, 0.60, places=3)

    def test_punctuated_repetition_is_kept(self):
        """"No. No. No." and "money, money, money" are the speaker meaning it.
        147 of the 185 correctly-kept pairs on the reference recording are
        distinguished by this one test."""
        for text in ("No. No. Not that guy.", "cash money, money, money, and"):
            words = words_at(text)
            self.assertEqual(repeat_spans(words, "restarts"), [], text)

    def test_an_emphatic_word_is_kept_even_unpunctuated(self):
        self.assertEqual(repeat_spans(words_at("go go go"), "restarts"), [])

    def test_a_gap_means_deliberate(self):
        words = speech(("by", 0.0, 0.3), ("by", 0.6, 0.9))
        self.assertEqual(repeat_spans(words, "restarts"), [])

    def test_a_phrase_restart_loses_its_first_copy(self):
        words = words_at("that could be that could be one reason")
        found = repeat_spans(words, "restarts")
        self.assertEqual(len(found), 1)
        self.assertAlmostEqual(found[0].start, 0.0, places=3)
        self.assertAlmostEqual(found[0].end, 0.90, places=3)
        self.assertEqual(found[0].detail, "that could be")

    def test_stutters_mode_leaves_phrase_restarts_alone(self):
        words = words_at("when he when he sees")
        self.assertEqual(repeat_spans(words, "stutters"), [])

    def test_off_removes_nothing(self):
        self.assertEqual(repeat_spans(words_at("the the the"), "off"), [])


class CensorTests(unittest.TestCase):
    def test_a_listed_term_becomes_a_mute_with_margin(self):
        words = words_at("that was shit really")
        found = mute_spans(words, CensorList(["shit"]), margin=0.05)
        self.assertEqual(len(found), 1)
        self.assertAlmostEqual(found[0].start, 0.55, places=3)
        self.assertAlmostEqual(found[0].end, 0.95, places=3)

    def test_adjacent_mutes_merge(self):
        words = words_at("shit shit fine")
        found = mute_spans(words, CensorList(["shit"]), margin=0.10)
        self.assertEqual(len(found), 1)

    def test_no_list_means_no_mutes(self):
        self.assertEqual(mute_spans(words_at("shit"), None), [])

    def test_muting_does_not_change_the_timeline(self):
        """A mute is chosen over a cut precisely so the picture, the transcript
        and the lip sync are untouched."""
        levels = envelope("#" * 200)
        words = words_at("that was shit really")
        plan = plan_edit(words, levels, 0.010, 2.0,
                         EditOptions(fillers="off", repeats="off"),
                         CensorList(["shit"]))
        self.assertEqual(len(plan.mutes), 1)
        self.assertEqual(len(remap_words(words, plan.keep)), len(words))


class RemapTests(unittest.TestCase):
    def test_a_removed_word_does_not_reappear_in_the_transcript(self):
        """A removed word's start sits exactly on the boundary of the range cut
        around it, so a start-based placement hands it back at the tail of the
        preceding range -- the deleted "uh" in the transcript of a file that no
        longer contains it."""
        words = words_at("I uh think")
        keep = [(0.0, 0.30), (0.60, 0.90)]
        remapped = remap_words(words, keep)
        self.assertEqual([w.text for w in remapped], ["I", "think"])

    def test_times_are_compacted_onto_the_new_timeline(self):
        words = words_at("a b c d")            # 0.0 0.3 0.6 0.9
        keep = [(0.0, 0.30), (0.90, 1.20)]
        remapped = remap_words(words, keep)
        self.assertEqual([w.text for w in remapped], ["a", "d"])
        self.assertAlmostEqual(remapped[1].start, 0.30, places=3)

    def test_the_result_stays_ordered_and_inside_the_new_duration(self):
        words = words_at(" ".join(f"w{i}" for i in range(20)))
        keep = [(0.0, 0.9), (1.8, 2.7), (4.2, 5.1)]
        remapped = remap_words(words, keep)
        total = sum(b - a for a, b in keep)
        self.assertEqual(remapped, sorted(remapped, key=lambda w: w.start))
        self.assertLessEqual(remapped[-1].end, total + 1e-6)

    def test_mutes_move_with_the_words(self):
        keep = [(0.0, 1.0), (2.0, 3.0)]
        moved = remap_mutes([Mute(2.4, 2.6, "x")], keep)
        self.assertEqual([(round(a, 3), round(b, 3)) for a, b in moved],
                         [(1.400, 1.600)])

    def test_a_frame_locked_range_start_does_not_overlap_the_previous_word(self):
        """The ranges handed to `remap_words` are the ones actually rendered,
        and those are locked to frame boundaries -- so a boundary the planner
        put exactly on a word's start arrives up to one frame *later*. Placing
        the word at a negative offset from that start put it before its own
        range, overlapping the last word of the previous one: 48 of the 8,022
        words in the reference chunk, by 2-10 ms, i.e. one frame at 60 fps.
        The file was written and then refused by our own reader."""
        words = words_at("a b c d")                 # 0.0 0.3 0.6 0.9, 0.3 each
        frame = 1.0 / 60.0
        # The planner would keep [0, 0.3] and [0.6, 1.2]; frame-locking pushes
        # the second range's start past "c" by a fraction of a frame.
        keep = [(0.0, 0.30), (0.60 + frame / 2, 1.20)]
        remapped = remap_words(words, keep)
        self.assertEqual([w.text for w in remapped], ["a", "c", "d"])
        for previous, current in zip(remapped, remapped[1:]):
            self.assertLessEqual(previous.end, current.start + 1e-6)
        words_from_json(words_to_json(remapped))    # must load back

    def test_the_remapped_transcript_always_loads_back(self):
        """The contract, stated once: whatever ranges it is given, the result
        is a transcript this codebase can read. `words.json` is what recovery,
        `retranscribe` and the edit's generation check all read."""
        words = words_at(" ".join(f"w{i}" for i in range(60)))
        frame = 1.0 / 60.0
        keep = [(round(i * 1.2 + frame / 3, 6), i * 1.2 + 0.75)
                for i in range(15)]
        remapped = remap_words(words, keep)
        self.assertTrue(remapped)
        words_from_json(words_to_json(remapped))


class WordsFileWriterTests(unittest.TestCase):
    """The one funnel every `words.json` is written through checks itself."""

    def test_an_unreadable_word_list_is_refused_rather_than_written(self):
        overlapping = [Word("one", 0.0, 0.5, 0.9), Word("two", 0.4, 0.5, 0.9)]
        with self.assertRaises(CorruptWordsFile) as caught:
            words_json_text(overlapping, {"language": "en"})
        self.assertIn("cannot be read back", str(caught.exception))

    def test_a_valid_word_list_still_round_trips(self):
        words = words_at("the quick brown fox")
        payload = json.loads(words_json_text(words, {"language": "en"}))
        self.assertEqual([w.text for w in words_from_json(payload["words"])],
                         ["the", "quick", "brown", "fox"])
        self.assertEqual(payload["language"], "en")


class RangeArithmeticTests(unittest.TestCase):
    def test_subtract_splits_a_range(self):
        self.assertEqual(subtract_range([(0.0, 10.0)], 4.0, 6.0),
                         [(0.0, 4.0), (6.0, 10.0)])

    def test_subtract_outside_changes_nothing(self):
        self.assertEqual(subtract_range([(0.0, 2.0)], 5.0, 6.0), [(0.0, 2.0)])

    def test_merge_joins_across_a_gap_when_asked(self):
        self.assertEqual(merge_ranges([(0.0, 1.0), (1.1, 2.0)], gap=0.2),
                         [(0.0, 2.0)])
        self.assertEqual(merge_ranges([(0.0, 1.0), (1.1, 2.0)], gap=0.0),
                         [(0.0, 1.0), (1.1, 2.0)])


class PlanIntegrationTests(unittest.TestCase):
    def test_a_pointless_micro_cut_is_not_made(self):
        levels = envelope("#" * 100 + "." * 25 + "#" * 100)
        plan = plan_edit([], levels, 0.010, 2.25,
                         EditOptions(fillers="off", repeats="off", censor="off",
                                     margin_seconds=0.05, min_cut_seconds=0.25))
        self.assertEqual(len(plan.keep), 1,
                         "a 0.15s gap is not worth a jump cut")

    def test_a_gap_holding_a_removal_is_never_closed(self):
        levels = envelope("#" * 300)
        words = words_at("I uh think")
        plan = plan_edit(words, levels, 0.010, 3.0,
                         EditOptions(fillers="sounds", repeats="off",
                                     censor="off", min_cut_seconds=1.0))
        self.assertEqual(plan.count_by(FILLER), 1)
        self.assertEqual(len(plan.keep), 2,
                         "closing this gap would put the filler back")

    def test_a_plan_that_would_delete_the_recording_is_refused(self):
        levels = envelope("." * 1000)
        with self.assertRaises(EditRefused) as caught:
            plan_edit([], levels, 0.010, 10.0,
                      EditOptions(max_removed_fraction=0.75))
        self.assertIn("noise_floor_db", str(caught.exception))

    def test_planning_is_deterministic(self):
        levels = envelope(("#" * 40 + "." * 40) * 10)
        words = words_at("the the cat sat uh down", start=0.1)
        options = EditOptions()
        first = plan_edit(words, levels, 0.010, 8.0, options)
        second = plan_edit(words, levels, 0.010, 8.0, options)
        self.assertEqual(first.keep, second.keep)
        self.assertEqual([r.start for r in first.removals],
                         [r.start for r in second.removals])

    def test_removals_and_silence_are_accounted_separately(self):
        levels = envelope("#" * 100 + "." * 100 + "#" * 100)
        words = words_at("the the cat", start=0.0)
        plan = plan_edit(words, levels, 0.010, 3.0, EditOptions())
        self.assertEqual(plan.count_by(REPEAT), 1)
        self.assertGreater(plan.removed_seconds, plan.removed_by(REPEAT))

    def test_the_report_names_every_decision(self):
        levels = envelope("#" * 100 + "." * 100 + "#" * 100)
        words = words_at("the the cat uh sat")
        plan = plan_edit(words, levels, 0.010, 3.0, EditOptions(),
                         CensorList(["cat"]))
        report = render_report(plan, source="c000.mp4", envelope=levels,
                               hop=0.010)
        self.assertIn("Edit report", report)
        self.assertIn("Noise floor sensitivity", report)
        self.assertIn("Fillers removed", report)
        self.assertIn("untouched", report)


class SyncArithmeticTests(unittest.TestCase):
    """Why the two tracks cannot drift apart.

    The video is cut by dropping whole frames and the audio by copying sample
    ranges. If those were measured independently the rounding would disagree at
    every join, and over the ~950 cuts a two-hour chunk produces the errors
    random-walk into a third of a second. They agree because the sample count is
    *derived from* the frame count rather than measured.
    """

    def test_audio_length_is_derived_from_the_frame_count(self):
        keep = [(0.0, 1.0), (2.5, 3.25), (10.0, 12.5)]
        video, audio, frames = segments_for(keep, fps=60, sample_rate=48000)
        self.assertEqual(len(video), len(audio))
        for (first, last), (_, length) in zip(video, audio):
            self.assertEqual(length, (last - first + 1) * 800)
        self.assertEqual(sum(length for _, length in audio), frames * 800)

    def test_the_stream_start_offset_shifts_the_audio_read(self):
        """The reference masters start video at 0.034s and audio at 0.044s.
        Ignoring that reads every segment 480 samples from the wrong place."""
        plain, = segments_for([(1.0, 2.0)], fps=60, sample_rate=48000)[1]
        shifted, = segments_for([(1.0, 2.0)], fps=60, sample_rate=48000,
                                start_offset=0.034 - 0.044)[1]
        self.assertEqual(plain[0] - shifted[0], 480)
        self.assertEqual(plain[1], shifted[1])

    def test_a_fractional_rate_does_not_accumulate_error(self):
        keep = [(index * 2.0, index * 2.0 + 1.0) for index in range(500)]
        _, audio, frames = segments_for(keep, fps=59.94, sample_rate=48000)
        exact = frames * 48000 / 59.94
        self.assertLess(abs(sum(length for _, length in audio) - exact), 1.5)

    def test_the_select_expression_keeps_exactly_the_planned_frames(self):
        """The expression is a balanced search tree rather than a sum of terms.
        This evaluates it the way ffmpeg would and checks it selects the same
        frames the audio segments were built from."""
        keep = [(0.5, 1.5), (3.0, 3.4), (7.25, 9.0), (12.0, 12.02)]
        fps = 60
        video, _, _ = segments_for(keep, fps=fps, sample_rate=48000)
        ranges = [((first - 0.5) / fps, (last + 0.5) / fps)
                  for first, last in video]
        expression = keep_expression(ranges)
        selected = {index for index in range(0, 13 * fps)
                    if _evaluate(expression, index / fps)}
        expected = {index for first, last in video
                    for index in range(first, last + 1)}
        self.assertEqual(selected, expected)

    def test_the_expression_nests_rather_than_summing(self):
        ranges = [(float(index), index + 0.5) for index in range(64)]
        expression = keep_expression(ranges)
        self.assertIn("if(lt(t,", expression)
        # A balanced tree over 64 leaves is 6 deep; a flat sum would be 64 wide.
        self.assertLessEqual(_depth(expression), 8)

    def test_an_empty_plan_yields_an_expression_that_keeps_nothing(self):
        self.assertEqual(keep_expression([]), "0")


def _evaluate(expression: str, t: float) -> bool:
    """Evaluate ffmpeg's `if(lt(...),...)` / `between(...)` form in Python."""
    expression = expression.strip()
    if expression.startswith("between("):
        _, _, rest = expression.partition("(")
        _, low, high = rest[:-1].split(",")
        return float(low) <= t <= float(high)
    if expression.startswith("if(lt(t,"):
        body = expression[3:-1]
        pivot_text, remainder = _split_top(body)
        pivot = float(pivot_text[len("lt(t,"):-1])
        left, right = _split_top(remainder)
        return _evaluate(left if t < pivot else right, t)
    raise AssertionError(f"unexpected expression: {expression[:60]}")


def _split_top(text: str) -> tuple[str, str]:
    depth = 0
    for index, character in enumerate(text):
        if character == "(":
            depth += 1
        elif character == ")":
            depth -= 1
        elif character == "," and depth == 0:
            return text[:index], text[index + 1:]
    return text, ""


def _depth(expression: str) -> int:
    best = current = 0
    for character in expression:
        if character == "(":
            current += 1
            best = max(best, current)
        elif character == ")":
            current -= 1
    return best


class AudioAssemblyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="vodpipe-audio-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def write(self, name: str, samples: list[int], channels: int = 2,
              width: int = 2) -> Path:
        path = self.tmp / name
        with wave.open(str(path), "wb") as handle:
            handle.setnchannels(channels)
            handle.setsampwidth(width)
            handle.setframerate(48000)
            if width == 2:
                handle.writeframes(array("h", samples).tobytes())
            else:
                handle.writeframes(bytes(len(samples) * width))
        return path

    def read(self, path: Path) -> list[int]:
        with wave.open(str(path), "rb") as handle:
            data = array("h")
            data.frombytes(handle.readframes(handle.getnframes()))
            return list(data)

    def test_the_output_is_exactly_the_planned_length(self):
        source = self.write("in.wav", [1000] * 2000)          # 1000 frames
        out = self.tmp / "out.wav"
        written = assemble(source, out, [(0, 100), (400, 250), (900, 50)])
        self.assertEqual(written, 400)
        self.assertEqual(len(self.read(out)), 800)

    def test_a_crossfade_does_not_change_the_length(self):
        source = self.write("in.wav", [1000] * 2000)
        plain = self.tmp / "plain.wav"
        faded = self.tmp / "faded.wav"
        segments = [(0, 200), (600, 200)]
        self.assertEqual(assemble(source, plain, segments, crossfade_samples=0),
                         assemble(source, faded, segments,
                                  crossfade_samples=48))
        self.assertEqual(len(self.read(plain)), len(self.read(faded)))

    def test_a_crossfade_blends_rather_than_stepping(self):
        """A hard splice is a step in the waveform, which is the click the
        operator hears as a pop."""
        samples = [0] * 1000 + [20000] * 1000
        source = self.write("in.wav", samples, channels=1)
        out = self.tmp / "out.wav"
        # Segment two starts loud; without a fade the join is a 20000 step.
        assemble(source, out, [(0, 100), (1000, 100)], crossfade_samples=50)
        values = self.read(out)
        self.assertLess(abs(values[100]), 6000, "the join should ramp, not step")
        self.assertGreater(values[149], 15000, "and should reach full level")

    def test_a_mute_zeroes_its_span_and_ramps_into_it(self):
        source = self.write("in.wav", [10000] * 2000, channels=1)
        out = self.tmp / "out.wav"
        assemble(source, out, [(0, 1000)], mutes=[(400, 600)],
                 mute_ramp_samples=50)
        values = self.read(out)
        self.assertEqual(values[500], 0)
        self.assertLess(values[399], 300,
                        "the ramp should have reached near-silence by the mute")
        self.assertEqual(values[100], 10000, "audio outside the mute is intact")
        self.assertLess(values[370], 10000, "there is a ramp, not a step")
        self.assertGreater(values[355], values[395], "and it descends")

    def test_reading_past_the_end_pads_rather_than_truncating(self):
        source = self.write("in.wav", [500] * 200)            # 100 frames
        out = self.tmp / "out.wav"
        self.assertEqual(assemble(source, out, [(50, 200)]), 200)
        self.assertEqual(len(self.read(out)), 400)

    def test_an_empty_plan_is_refused(self):
        source = self.write("in.wav", [0] * 100)
        with self.assertRaises(AudioAssemblyError):
            assemble(source, self.tmp / "out.wav", [])

    def test_non_16_bit_input_is_refused_rather_than_mangled(self):
        source = self.write("in.wav", [0] * 100, width=1)
        with self.assertRaises(AudioAssemblyError) as caught:
            assemble(source, self.tmp / "out.wav", [(0, 10)])
        self.assertIn("16-bit", str(caught.exception))

    def test_segments_are_copied_from_the_right_place(self):
        samples = list(range(0, 1000))
        source = self.write("in.wav", samples, channels=1)
        out = self.tmp / "out.wav"
        assemble(source, out, [(100, 10), (500, 10)])
        self.assertEqual(self.read(out),
                         list(range(100, 110)) + list(range(500, 510)))


if __name__ == "__main__":
    unittest.main()


class NumericRepeatTests(unittest.TestCase):
    """A repeated number is a figure, not a stutter.

    Unlike a doubled word, dropping a copy changes the fact rather than the
    phrasing: "TikTok was fifty fifty" becomes "TikTok was fifty". All four
    occurrences on the reference recording were of exactly this kind.
    """

    def test_a_repeated_number_word_is_kept(self):
        words = words_at("it was fifty fifty right")
        self.assertEqual(repeat_spans(words, "restarts"), [])

    def test_a_repeated_digit_is_kept(self):
        words = words_at("paid 50 50 for it")
        self.assertEqual(repeat_spans(words, "restarts"), [])

    def test_a_year_read_as_a_phrase_is_kept(self):
        words = words_at("the twenty twenty eight presidential primary")
        self.assertEqual(repeat_spans(words, "restarts"), [])

    def test_an_ordinary_stutter_is_still_cut(self):
        words = words_at("the the presidential primary")
        self.assertEqual(len(repeat_spans(words, "restarts")), 1)


class SnapTests(unittest.TestCase):
    """A word-derived cut edge may only grow outward, into a gap.

    An energy search that is allowed to move freely walks into a stop consonant,
    which is quieter than the room -- the first version of this did exactly that
    and clipped 12 words on a five-minute sample.
    """

    def test_a_cut_moves_past_a_filler_tail_the_transcript_underestimated(self):
        """Deepgram's word *end* times are estimates. Here the "uh" really runs
        to 0.62 but is reported as ending at 0.60; cutting on the estimate would
        leave 20 ms of it behind."""
        levels = envelope("#" * 62 + "." * 4 + "#" * 40)
        words = speech(("I", 0.0, 0.30), ("uh", 0.30, 0.60), ("think", 0.66, 1.06))
        plan = plan_edit(words, levels, 0.010, 1.06,
                         EditOptions(remove_silence=False, repeats="off",
                                     censor="off", min_cut_seconds=0.0))
        (_, first_end), (second_start, _) = plan.keep
        self.assertAlmostEqual(first_end, 0.30, places=2)
        self.assertGreaterEqual(second_start, 0.62 - 1e-6,
                                "the cut should clear the audible tail")
        self.assertLessEqual(second_start, 0.66 + 1e-6,
                             "but must never reach the next word")

    def test_a_cut_does_not_move_when_the_boundary_is_already_quiet(self):
        """Nothing is gained by sliding a cut through silence that the silence
        pass will remove anyway, so ties resolve to where the transcript put
        it."""
        levels = envelope("#" * 60 + "." * 6 + "#" * 40)
        words = speech(("I", 0.0, 0.30), ("uh", 0.30, 0.60), ("think", 0.64, 1.00))
        plan = plan_edit(words, levels, 0.010, 1.06,
                         EditOptions(remove_silence=False, repeats="off",
                                     censor="off", min_cut_seconds=0.0))
        removal, = plan.removals
        self.assertAlmostEqual(removal.end, 0.60, places=2)

    def test_a_cut_never_reaches_a_neighbouring_word(self):
        """Words abutting with no gap: there is nowhere to grow, so the cut
        stays exactly where the transcript put it."""
        levels = envelope("#" * 100)
        words = words_at("I uh think")
        plan = plan_edit(words, levels, 0.010, 1.0,
                         EditOptions(remove_silence=False, repeats="off",
                                     censor="off", min_cut_seconds=0.0))
        for word in words:
            if word.text == "uh":
                continue
            self.assertTrue(
                any(a - 1e-6 <= word.start and word.end <= b + 1e-6
                    for a, b in plan.keep),
                f"{word.text} was clipped by {plan.keep}")

    def test_the_plan_reports_the_span_it_actually_cut(self):
        """The report is the audit trail, so it must show the snapped span and
        not the one that was proposed before the audio was consulted."""
        levels = envelope("#" * 62 + "." * 4 + "#" * 40)
        words = speech(("I", 0.0, 0.30), ("uh", 0.30, 0.60), ("think", 0.66, 1.06))
        plan = plan_edit(words, levels, 0.010, 1.06,
                         EditOptions(remove_silence=False, repeats="off",
                                     censor="off", min_cut_seconds=0.0))
        removal, = plan.removals
        self.assertGreater(removal.duration, 0.30)
        self.assertAlmostEqual(removal.duration, 1.06 - plan.kept_seconds,
                               places=2)


class StreamTailTests(unittest.TestCase):
    """The two streams of a real capture do not end together.

    On the reference recording one chunk's audio runs 1.03 s past its video
    (3533.51 s against 3532.48 s). The plan is built on the audio envelope, so
    without a frame ceiling its tail asks the encoder for fourteen frames that
    were never recorded — the encoder produces fewer frames than the audio was
    cut for and the track is out of step from that point back. This is not
    hypothetical: it is exactly how the first full-length render failed.
    """

    def test_a_range_past_the_last_frame_is_trimmed_to_it(self):
        video, audio, frames = segments_for(
            [(0.0, 1.0), (2.0, 5.0)], fps=60, sample_rate=48000, max_frames=180)
        self.assertEqual(video[-1][1], 179, "clamped to the last real frame")
        self.assertEqual(frames, sum(last - first + 1 for first, last in video))
        self.assertEqual(sum(length for _, length in audio), frames * 800)

    def test_a_range_entirely_past_the_end_is_dropped(self):
        video, audio, frames = segments_for(
            [(0.0, 1.0), (10.0, 12.0)], fps=60, sample_rate=48000,
            max_frames=120)
        self.assertEqual(len(video), 1)
        self.assertEqual(len(audio), 1)
        self.assertEqual(frames, 61)

    def test_the_audio_still_matches_the_clamped_frames_exactly(self):
        """The clamp must not break the property the whole module exists for."""
        _, audio, frames = segments_for(
            [(0.0, 2.0), (3.0, 9.0)], fps=60, sample_rate=48000,
            max_frames=300)
        self.assertEqual(sum(length for _, length in audio), frames * 800)

    def test_no_ceiling_keeps_the_old_behaviour(self):
        with_none = segments_for([(0.0, 1.0)], fps=60, sample_rate=48000)
        with_room = segments_for([(0.0, 1.0)], fps=60, sample_rate=48000,
                                 max_frames=10_000)
        self.assertEqual(with_none, with_room)


class EditReservationTests(unittest.TestCase):
    """A reservation the drive cannot meet turns the feature off.

    This is the third time this codebase has had to learn it: the proxy
    estimator once asked for 319 GB to build a 538 MB file, and the first
    version of *this* estimator asked for 82 GB to build a chunk whose real peak
    was 13 GB. Anchoring on the source file's own size -- the edit is a re-encode
    of that exact footage at that exact resolution -- keeps it honest.
    """

    def estimate(self, source_bytes: int, duration: float, **kwargs) -> int:
        from unittest.mock import patch
        from vodpipe import media

        probe = {"streams": [{"codec_type": "video", "width": 1920,
                              "height": 1080, "r_frame_rate": "60/1",
                              "duration": str(duration)},
                             {"codec_type": "audio", "sample_rate": "48000"}],
                 "format": {"duration": str(duration)}}
        stat = type("S", (), {"st_size": source_bytes})()
        with patch.object(media, "ffprobe_json", return_value=probe), \
                patch.object(Path, "stat", return_value=stat):
            return media.estimate_edit_peak_bytes(
                _tools(), Path("master.mp4"), **kwargs)

    def test_it_stays_within_reach_of_the_real_peak(self):
        # A two-hour 1080p60 chunk: 6.4 GB master, ~13 GB measured peak.
        reserved = self.estimate(6_910_501_904, 7200.0, quality=22)
        self.assertGreater(reserved, 13 * 1000 ** 3,
                           "must still cover what is actually written")
        self.assertLess(reserved, 30 * 1000 ** 3,
                        "a reservation the drive cannot meet is the bug")

    def test_it_covers_the_pcm_scratch_files(self):
        """They are 1.4 GB each for a two-hour stereo track, and leaving them
        out is how a full drive costs a whole encode."""
        small = self.estimate(1000, 7200.0)
        self.assertGreater(small, 2 * 7200 * 48000 * 2 * 2 * 0.9)

    def test_a_lower_quality_number_reserves_more(self):
        self.assertGreater(self.estimate(1_000_000_000, 600.0, quality=16),
                           self.estimate(1_000_000_000, 600.0, quality=22))

    def test_a_missing_master_is_an_error_not_a_zero(self):
        from vodpipe import media
        with self.assertRaises(RuntimeError):
            media.estimate_edit_peak_bytes(
                _tools(), Path("does-not-exist.mp4"))
