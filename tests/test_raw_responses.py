"""Keeping what Deepgram actually said.

`words.json` is the *normalised* word stream: sorted, de-overlapped, and with
same-start collisions resolved by dropping one of the pair. That is what every
export derives from, and it is deliberately not identical to the response it
came from -- so a question about a derivation ("did we lose that word, or did
Deepgram never say it?") has no answer unless the response itself was kept.

The rule that matters here is the last one: archiving is a convenience, and a
transcription that succeeded must never be lost to it.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from vodpipe.asr import DeepgramProvider
from vodpipe.config import DEFAULTS, Config, deep_merge
from vodpipe.state import SessionStore
from vodpipe.transcribe import RollingTranscriber
from vodpipe.util import Tools

RESPONSE = {
    "metadata": {"request_id": "abc-123", "duration": 12.5, "models": ["nova-3"]},
    "results": {"channels": [{"alternatives": [{
        "transcript": "hello there",
        "words": [
            {"word": "hello", "start": 0.0, "end": 0.4, "confidence": 0.99,
             "punctuated_word": "Hello"},
            {"word": "there", "start": 0.4, "end": 0.8, "confidence": 0.98,
             "punctuated_word": "there."},
        ],
    }]}]},
}


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="vodpipe-raw-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.config = Config(deep_merge(DEFAULTS, {
            "paths": {"masters_root": str(self.tmp / "masters"),
                      "work_root": str(self.tmp / "work")},
        }), self.tmp / "config.json")
        self.config.masters_root.mkdir(parents=True)
        self.transcriber = RollingTranscriber(
            self.config, Tools(ffmpeg="ffmpeg", ffprobe="ffprobe",
                               streamlink="", claude=""),
            SessionStore(self.config.masters_root))
        self.directory = self.tmp / "out"

    def test_a_response_is_written_verbatim(self):
        with self.transcriber.archiving_to(self.directory):
            self.transcriber._archive_response(RESPONSE)
        written = sorted(self.directory.glob("*.json"))
        self.assertEqual([path.name for path in written], ["0001.json"])
        self.assertEqual(json.loads(written[0].read_text(encoding="utf-8")),
                         RESPONSE)

    def test_the_fields_normalisation_drops_are_still_there(self):
        """The reason for keeping it at all: `words.json` has no request id, no
        raw `word` beside the punctuated one, and no provider metadata."""
        with self.transcriber.archiving_to(self.directory):
            self.transcriber._archive_response(RESPONSE)
        stored = json.loads((self.directory / "0001.json").read_text(encoding="utf-8"))
        self.assertEqual(stored["metadata"]["request_id"], "abc-123")
        word = stored["results"]["channels"][0]["alternatives"][0]["words"][0]
        self.assertEqual((word["word"], word["punctuated_word"]),
                         ("hello", "Hello"))

    def test_responses_accumulate_rather_than_overwrite(self):
        with self.transcriber.archiving_to(self.directory):
            for _ in range(3):
                self.transcriber._archive_response(RESPONSE)
        self.assertEqual(len(list(self.directory.glob("*.json"))), 3)

    def test_nothing_is_written_outside_an_archiving_block(self):
        self.transcriber._archive_response(RESPONSE)
        self.assertFalse(self.directory.exists())

    def test_the_block_restores_whatever_was_set_before_it(self):
        with self.transcriber.archiving_to(self.directory):
            with self.transcriber.archiving_to(self.tmp / "inner"):
                self.transcriber._archive_response(RESPONSE)
            self.transcriber._archive_response(RESPONSE)
        self.assertTrue((self.tmp / "inner" / "0001.json").is_file())
        self.assertTrue((self.directory / "0001.json").is_file())

    def test_every_way_a_provider_is_built_gets_the_archiver(self):
        """There are two construction paths -- one for an identity matching the
        config, one for a one-shot's explicit language or a chunk pinned to an
        older generation -- and only the first had the hook. The result was an
        archive with holes that nothing reported, which is worse than no
        archive: it looks complete."""
        self.config.set("secrets.deepgram_api_key", "test-key")
        matching = self.transcriber.provider()
        self.assertIsNotNone(getattr(matching, "on_response", None))

        pinned = self.transcriber.provider({
            "provider": "deepgram", "model": "nova-2", "language": "de",
            "filler_words": False,
        })
        self.assertIsNotNone(getattr(pinned, "on_response", None),
                             "a pinned generation must archive too")

    def test_the_setting_switches_it_off_entirely(self):
        self.config.set("transcription.keep_raw_responses", False)
        self.assertIsNone(self.transcriber._response_sink())
        self.config.set("transcription.keep_raw_responses", True)
        self.assertIsNotNone(self.transcriber._response_sink())


class NeverFailsTheTranscriptionTests(unittest.TestCase):
    """A full disk must cost the archive, not the transcript."""

    def test_an_archiver_that_raises_does_not_lose_the_words(self):
        def explode(payload):
            raise OSError("no space left on device")

        provider = DeepgramProvider("key", on_response=explode)
        # Drive the real path: only the network is replaced.
        provider._post = lambda payload, *, deadline: RESPONSE
        source = Path(tempfile.mkdtemp(prefix="vodpipe-raw-audio-")) / "a.flac"
        source.write_bytes(b"not really audio, but not empty")
        self.addCleanup(shutil.rmtree, source.parent, True)

        heard = provider.transcribe(source, expected_duration=12.5)
        self.assertEqual([word.text for word in heard], ["Hello", "there."])


if __name__ == "__main__":
    unittest.main()
