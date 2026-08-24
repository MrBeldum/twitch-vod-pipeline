"""Chat moment analysis has to read the messages, not just the rate."""

from __future__ import annotations

import unittest

from vodpipe.chat import ChatMessage
from vodpipe.moments import analyse_chat, render_moments


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
