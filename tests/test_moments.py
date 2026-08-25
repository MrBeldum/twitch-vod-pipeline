"""Chat moment analysis has to read the messages, not just the rate."""

from __future__ import annotations

import unittest

from vodpipe.chat import ChatMessage
from vodpipe.moments import (
    HOP_SECONDS,
    MAX_MOMENTS,
    WINDOW_SECONDS,
    analyse_chat,
    render_moments,
    _classify,
    _score_window,
    _suppress,
)


def msg(offset: float, text: str, user: str = "u", *, bits: int = 0,
        kind: str = "privmsg") -> ChatMessage:
    return ChatMessage(
        message_id=f"{user}:{offset}:{text[:20]}",
        offset=offset, created_at=0.0, user=user, user_id="",
        text=text, bits=bits, kind=kind, source="irc",
    )


class MomentContentTests(unittest.TestCase):
    def test_laughter_spam_is_a_moment_even_at_modest_rate(self):
        messages = []
        for index in range(12):
            messages.append(msg(60.0 + index * 0.4, "KEKW", user=f"u{index}"))
            messages.append(msg(60.2 + index * 0.4, "LUL", user=f"v{index}"))
        # Background chatter so the chunk baseline is not the laugh window.
        for index in range(40):
            messages.append(msg(index * 10.0, "hi", user=f"bg{index}"))
        moments = analyse_chat(messages, duration=400.0, session_offset=0.0)
        kinds = {item.kind for item in moments}
        self.assertIn("laughter", kinds)
        laugh = next(item for item in moments if item.kind == "laughter")
        self.assertGreaterEqual(laugh.laugh_fraction, 0.28)
        self.assertTrue(any("KEKW" in sample or "LUL" in sample
                            for sample in laugh.samples))

    def test_a_rate_spike_of_raid_joins_is_not_called_a_clip(self):
        messages = [msg(5.0 + index * 0.05, "hi", user=f"raider{index}")
                    for index in range(80)]
        messages.append(msg(5.1, "10 raiders", user="twitch", kind="system"))
        moments = analyse_chat(messages, duration=30.0)
        self.assertTrue(all(item.kind != "clip-call" for item in moments))
        self.assertTrue(all(item.kind != "laughter" for item in moments))

    def test_clip_it_plus_a_joke_is_nominated(self):
        messages = [
            msg(20.0, "did he just say that", "a"),
            msg(20.4, "CLIP IT", "b"),
            msg(20.6, "clip that", "c"),
            msg(20.8, "KEKW", "d"),
            msg(21.0, "LULW", "e"),
            msg(21.2, "someone clip this", "f"),
        ]
        moments = analyse_chat(messages, duration=60.0)
        self.assertTrue(any(item.kind == "clip-call" for item in moments))

    def test_copypasta_is_detected_from_repeated_text_not_emotes(self):
        line = "we do a little trolling"
        messages = [msg(8.0 + index * 0.3, line, user=f"u{index}")
                    for index in range(10)]
        moments = analyse_chat(messages, duration=40.0)
        pasta = [item for item in moments if item.kind == "copypasta"]
        self.assertTrue(pasta)
        self.assertIn("trolling", pasta[0].copypasta)

    def test_quiet_chat_yields_nothing(self):
        messages = [msg(float(index * 30), "ok", user="one") for index in range(5)]
        self.assertEqual(analyse_chat(messages, duration=200.0), [])

    def test_render_includes_clocks_and_samples(self):
        messages = [msg(10.0 + index * 0.2, "KEKW", user=f"u{index}")
                    for index in range(20)]
        block = render_moments(analyse_chat(messages, duration=40.0,
                                            session_offset=3600.0))
        self.assertIn("01:00:", block)
        self.assertIn("KEKW", block)


if __name__ == "__main__":
    unittest.main()


class WindowSweepTests(unittest.TestCase):
    """The window scan is a two-pointer sweep over an offset-sorted index.

    It replaced a rescan of every message for every window, which cost ~19s of
    pure Python on a 60k-message two-hour chunk (1.2s after). The sweep visits
    messages in offset order, but `_score_window` is order-sensitive --
    `_samples` dedupes on first occurrence and sorts stably, and the copypasta
    tally breaks ties on insertion order -- so each window's members are put
    back into their original order before scoring.

    The contract is therefore *not* that output is independent of input order
    (it never was). It is that the sweep returns exactly what the rescan
    returned for the same input, whatever order that input arrives in.
    """

    @staticmethod
    def rescan(messages, *, duration, session_offset=0.0,
               window=WINDOW_SECONDS, hop=HOP_SECONDS):
        """The pre-sweep implementation, kept here as the reference."""
        items = [i for i in messages if i.offset >= -0.05]
        if not items or duration <= 0:
            return []
        window = max(4.0, float(window))
        hop = max(1.0, float(hop))
        duration = max(duration, max(i.offset for i in items) + 0.1)
        classified = [_classify(i) for i in items]
        baseline = len(items) / duration if duration else 0.0
        candidates = []
        start = 0.0
        while start < duration:
            end = min(duration, start + window)
            bucket = [(it, fl) for it, fl in classified
                      if start <= it.offset < end]
            moment = _score_window(bucket, start, end, baseline,
                                   session_offset=session_offset)
            if moment is not None:
                candidates.append(moment)
            start += hop
            if end >= duration:
                break
        return _suppress(candidates)[:MAX_MOMENTS]

    def build(self, rnd, count):
        texts = ["KEKW", "clip that", "W", "same copypasta line here",
                 "actually insane", "LULW", "", "+2", "OMEGALUL"]
        return [ChatMessage(
            message_id=str(i), offset=rnd.uniform(-1.0, 900.0), created_at=0.0,
            user=f"u{i % 13}", user_id=str(i % 13),
            text=rnd.choice(texts), bits=rnd.choice([0, 0, 0, 100]),
        ) for i in range(count)]

    def test_sweep_matches_the_rescan_it_replaced(self):
        import random

        for trial in range(12):
            rnd = random.Random(trial)
            count = (0, 1, 6, 90, 700)[trial % 5]
            messages = self.build(rnd, count)
            if trial % 3 == 0:
                rnd.shuffle(messages)            # unsorted arrival
            elif trial % 3 == 1:
                messages.sort(key=lambda m: m.offset)
            with self.subTest(trial=trial, count=count):
                self.assertEqual(
                    [m.to_dict() for m in self.rescan(messages, duration=900.0)],
                    [m.to_dict() for m in analyse_chat(messages, duration=900.0)],
                )

    def test_duplicate_offsets_do_not_drop_a_message_from_its_window(self):
        """Ties in the sort key must not lose a message."""
        messages = [msg(10.0, f"line {i}", user=f"u{i}") for i in range(8)]
        moments = analyse_chat(messages, duration=40.0)
        self.assertTrue(moments)
        self.assertTrue(any(m.messages == 8 for m in moments),
                        [m.messages for m in moments])

    def test_a_message_past_the_declared_duration_still_scores(self):
        """`duration` is widened to the last offset; the sweep must follow."""
        messages = [msg(500.0 + i, "KEKW", user=f"u{i}") for i in range(6)]
        self.assertTrue(analyse_chat(messages, duration=10.0))
