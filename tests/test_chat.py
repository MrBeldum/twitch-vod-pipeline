"""Twitch chat capture: IRC parse, VOD GQL parse, slicing, persistence."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from vodpipe.chat import (
    ChatMessage,
    load_messages,
    parse_irc_line,
    parse_tags,
    slice_messages,
    write_chat_exports,
    _comments_from_edges,
    _is_ping,
    _ping_payload,
)


ORIGIN = 1_700_000_000.0


class IrcParseTests(unittest.TestCase):
    def test_tagged_privmsg_uses_tmi_timestamp_when_it_agrees(self):
        tmi = int((ORIGIN + 12.5) * 1000)
        line = (
            f"@badge-info=;badges=broadcaster/1;color=#FF0000;display-name=Alice;"
            f"emotes=25:0-4;id=abc-123;mod=0;room-id=1;subscriber=0;"
            f"tmi-sent-ts={tmi};turbo=0;user-id=9;user-type= "
            f":alice!alice@alice.tmi.twitch.tv PRIVMSG #someone :Kappa hello"
        )
        message = parse_irc_line(line, origin=ORIGIN, received_at=ORIGIN + 12.6)
        self.assertIsNotNone(message)
        self.assertEqual(message.user, "Alice")
        self.assertEqual(message.text, "Kappa hello")
        self.assertEqual(message.emotes, ("Kappa",))
        self.assertAlmostEqual(message.offset, 12.5, places=2)
        self.assertEqual(message.message_id, "abc-123")

    def test_local_clock_wins_when_tmi_is_minutes_off(self):
        tmi = int((ORIGIN + 3600) * 1000)
        line = (
            f"@display-name=Bob;id=x;tmi-sent-ts={tmi};user-id=2 "
            f":bob!bob@bob.tmi.twitch.tv PRIVMSG #chan :hi"
        )
        message = parse_irc_line(line, origin=ORIGIN, received_at=ORIGIN + 3.0)
        self.assertAlmostEqual(message.offset, 3.0, places=2)

    def test_usernotice_is_kept_as_its_own_kind(self):
        line = (
            "@display-name=RaidBot;id=r1;msg-id=raid;system-msg=10\\sraiders;"
            "tmi-sent-ts=1700000010000 "
            ":tmi.twitch.tv USERNOTICE #chan"
        )
        message = parse_irc_line(line, origin=ORIGIN, received_at=ORIGIN + 10)
        self.assertIsNotNone(message)
        self.assertEqual(message.kind, "system")
        self.assertIn("raiders", message.text)

    def test_ping_and_join_are_ignored(self):
        self.assertIsNone(parse_irc_line("PING :tmi.twitch.tv", origin=ORIGIN))
        self.assertIsNone(parse_irc_line(
            ":bob!bob@bob.tmi.twitch.tv JOIN #chan", origin=ORIGIN))
        self.assertTrue(_is_ping("PING :tmi.twitch.tv"))
        self.assertTrue(_is_ping("@tag=1 :tmi.twitch.tv PING :tmi.twitch.tv"))
        self.assertEqual(_ping_payload("PING :tmi.twitch.tv"), "tmi.twitch.tv")

    def test_bits_and_tag_unescape(self):
        self.assertEqual(parse_tags(r"system-msg=hello\syou\:yes")["system-msg"],
                         "hello you;yes")
        line = (
            "@display-name=Cheer;bits=100;id=b;tmi-sent-ts=1700000001000 "
            ":c!c@c.tmi.twitch.tv PRIVMSG #chan :Cheer100 pog"
        )
        message = parse_irc_line(line, origin=ORIGIN, received_at=ORIGIN + 1)
        self.assertEqual(message.bits, 100)


class SliceAndPersistTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="vodpipe-chat-"))

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_slice_is_half_open_and_sorted(self):
        messages = [
            ChatMessage("2", 10.0, 0, "b", "", "second"),
            ChatMessage("1", 1.0, 0, "a", "", "first"),
            ChatMessage("3", 20.0, 0, "c", "", "late"),
        ]
        sliced = slice_messages(messages, 0.0, 15.0)
        self.assertEqual([item.text for item in sliced], ["first", "second"])

    def test_round_trip_chat_json(self):
        messages = [
            ChatMessage("1", 1.5, ORIGIN, "alice", "9", "KEKW",
                        emotes=("KEKW",), bits=0, kind="privmsg", source="irc"),
        ]
        write_chat_exports(
            self.tmp, messages, channel="chan", session_id="sess",
            chunk="c000", session_offset=0.0, duration=10.0, source="irc")
        loaded = load_messages(self.tmp / "chat.json")
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0].text, "KEKW")
        self.assertTrue((self.tmp / "chat.txt").is_file())
        text = (self.tmp / "chat.txt").read_text(encoding="utf-8")
        self.assertIn("alice", text)
        self.assertIn("KEKW", text)


class VodCommentParseTests(unittest.TestCase):
    def test_fragments_become_body_and_emote_names(self):
        edges = [{
            "cursor": "aaa",
            "node": {
                "id": "c1",
                "contentOffsetSeconds": 42.2,
                "createdAt": "2024-01-01T00:00:42Z",
                "commenter": {"id": "1", "displayName": "Eve", "login": "eve"},
                "message": {
                    "fragments": [
                        {"text": "LUL", "emote": {"emoteID": "425618"}},
                        {"text": " no way"},
                    ],
                    "userBadges": [{"setID": "subscriber", "version": "1"}],
                },
            },
        }]
        messages = _comments_from_edges(edges, created_at=0.0)
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].text, "LUL no way")
        self.assertEqual(messages[0].emotes, ("LUL",))
        self.assertEqual(messages[0].user, "Eve")
        self.assertAlmostEqual(messages[0].offset, 42.2, places=2)

    def test_deleted_commenter_is_skipped(self):
        edges = [{"cursor": "x", "node": {
            "id": "gone", "contentOffsetSeconds": 1,
            "commenter": None,
            "message": {"fragments": [{"text": "hi"}]},
        }}]
        self.assertEqual(_comments_from_edges(edges, created_at=0.0), [])


if __name__ == "__main__":
    unittest.main()
