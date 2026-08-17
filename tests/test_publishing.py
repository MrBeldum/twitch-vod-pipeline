"""Publishing is a replacement, not an addition (AUD-029, AUD-027 fallout).

Two defects, one theme -- what Premiere reads must describe the transcript that
exists now:

* a chunk re-transcribed to a legitimately empty result kept its old
  `premiere.json`, `transcript.srt` and `censor-words.txt`, so text-based editing
  went on offering speech the audio no longer had;
* a rebuild that failed part way deleted the words file and left the exports from
  a run that never finished.

The asymmetry is deliberate and is what these tests pin down: a *successful*
publish replaces everything, a *failed* one changes nothing at all.
"""

from __future__ import annotations

import errno
import json
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from vodpipe.cli import cmd_republish
from vodpipe.config import DEFAULTS, Config, deep_merge
from vodpipe.exports import (
    GENERATION_FILES,
    read_manifest,
    write_export_sets,
    write_exports,
)
from vodpipe.state import Chunk, Session, SessionStore
from vodpipe.transcribe import RollingTranscriber
from vodpipe.transcript import (
    PUBLICATION_MARKER,
    CensorList,
    PublicationRecoveryError,
    Word,
    load_words,
    save_words,
)
from vodpipe.util import Tools, resolve_tools, run

CLIP_SECONDS = 12


def sample_words():
    return [Word("hello", 0.0, 0.4, 0.9), Word("world", 0.6, 0.5, 0.9)]


class RetirementTests(unittest.TestCase):
    """write_exports() owns the whole published set, not just what it wrote."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="vodpipe-pub-"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_successful_empty_publish_removes_the_previous_transcript(self):
        write_exports(self.tmp, sample_words(), censor=CensorList(["hello"]))
        self.assertTrue((self.tmp / "premiere.json").exists())

        write_exports(self.tmp, [])

        # The regression: Premiere kept importing this file.
        for name in ("premiere.json", "transcript.srt", "censor-words.txt",
                     "source/transcript.json"):
            self.assertFalse((self.tmp / name).exists(),
                             f"{name} should have been retired")
        self.assertIn("no speech",
                      (self.tmp / "source" / "transcript.txt")
                      .read_text(encoding="utf-8"))

    def test_dropping_the_censor_list_retires_its_export(self):
        write_exports(self.tmp, sample_words(), censor=CensorList(["hello"]))
        self.assertTrue((self.tmp / "censor-words.txt").exists())

        write_exports(self.tmp, sample_words(), censor=None)
        self.assertFalse((self.tmp / "censor-words.txt").exists())
        self.assertTrue((self.tmp / "premiere.json").exists())

    def test_a_normal_publish_keeps_the_whole_set(self):
        write_exports(self.tmp, sample_words(), censor=CensorList(["hello"]))
        written = write_exports(self.tmp, sample_words(),
                                censor=CensorList(["hello"]))
        expected = {"premiere.json", "transcript.srt", "censor-words.txt",
                    "source/words.json", "source/transcript.json",
                    "source/transcript.txt"}
        self.assertEqual(set(written), expected)
        for name in expected:
            self.assertTrue((self.tmp / name).exists(), name)

    def test_the_chunk_folder_holds_only_what_you_open(self):
        """The whole point of the layout: four files, and none of the plumbing.

        A folder is browsed, not queried, so anything sitting in it competes for
        attention with `premiere.json` -- the one file that has to be found.
        """
        write_exports(self.tmp, sample_words(), censor=CensorList(["hello"]))
        (self.tmp / "rundown.md").write_text("a rundown", encoding="utf-8")

        visible = {entry.name for entry in self.tmp.iterdir()
                   if not entry.name.startswith(".")}
        self.assertEqual(visible, {"premiere.json", "transcript.srt",
                                   "censor-words.txt", "rundown.md", "source"})
        self.assertTrue((self.tmp / "source").is_dir())

    def test_files_this_version_retired_are_cleaned_up_by_a_publish(self):
        """A recording made before 2026-08-17 still has them beside its words.

        They are owned but never rendered, so one publish deletes them. Without
        that, `IMPORT.md` and the filler report would sit in every transcript
        folder of every existing session forever.
        """
        write_exports(self.tmp, sample_words())
        for stale in ("IMPORT.md", "fillers.md", "fillers.json"):
            (self.tmp / stale).write_text("left over", encoding="utf-8")

        written = write_exports(self.tmp, sample_words())

        for stale in ("IMPORT.md", "fillers.md", "fillers.json"):
            self.assertFalse((self.tmp / stale).exists(), stale)
            self.assertNotIn(stale, written)
        self.assertTrue((self.tmp / "premiere.json").exists())

    def test_unrelated_files_are_never_touched(self):
        """Only the files this module publishes are its to remove."""
        (self.tmp / "rundown.md").write_text("a rundown", encoding="utf-8")
        (self.tmp / "notes.txt").write_text("mine", encoding="utf-8")
        write_exports(self.tmp, [])
        self.assertTrue((self.tmp / "rundown.md").exists())
        self.assertTrue((self.tmp / "notes.txt").exists())

    def test_the_flat_layout_is_moved_rather_than_duplicated(self):
        """Until 2026-08-18 every export sat directly in the chunk folder.

        Retiring those names is what makes the change a move: one publish writes
        `source/` and deletes the old copies. Leaving them would put a second,
        staler `premiere.json`-era transcript in every existing chunk folder --
        and `words.json` is the file recovery rebuilds from, so two of them is
        not clutter, it is an ambiguity about which transcript is real.
        """
        for stale in ("words.json", "transcript.json", "transcript.txt",
                      "exports.json"):
            (self.tmp / stale).write_text("{}", encoding="utf-8")

        write_exports(self.tmp, sample_words())

        for stale in ("words.json", "transcript.json", "transcript.txt",
                      "exports.json"):
            self.assertFalse((self.tmp / stale).exists(), stale)
            self.assertTrue((self.tmp / "source" / stale).is_file(), stale)


class GenerationTransactionTests(unittest.TestCase):
    """Every canonical file changes as one recoverable generation."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="vodpipe-generation-"))
        write_exports(self.tmp, sample_words(), censor=CensorList(["hello"]),
                      meta={"source": "old.mp4", "complete": True})
        self.before = self.snapshot()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def snapshot(self):
        return {
            name: ((self.tmp / name).read_bytes()
                   if (self.tmp / name).exists() else None)
            for name in GENERATION_FILES
        }

    def replacement(self):
        return [Word("replacement", 0.1, 0.6, 0.73)]

    def test_manifest_names_words_as_part_of_the_generation(self):
        manifest = read_manifest(self.tmp)
        words = json.loads((self.tmp / "source" / "words.json").read_text(encoding="utf-8"))
        self.assertIn("source/words.json", manifest["files"])
        self.assertEqual(words["generation"], manifest["generation"])

    def test_the_two_halves_of_a_generation_commit_as_one(self):
        """The chunk folder and `source/` are one generation, not two.

        Splitting the layout put `premiere.json` and the `words.json` it was
        rendered from in different directories. If they could commit
        independently, a crash between them would leave Premiere importing a
        transcript that recovery would rebuild differently -- and nothing would
        report it, because each directory would be internally valid.

        Failing the commit *after* the chunk folder has been replaced is the
        exact interleaving that would expose it.
        """
        from vodpipe import transcript
        original = transcript._replace_published_file
        source_dir = (self.tmp / "source").resolve()

        def fail_entering_source(staged, target):
            if Path(target).resolve().parent == source_dir:
                raise OSError("injected failure after the chunk folder committed")
            return original(staged, target)

        with patch("vodpipe.transcript._replace_published_file",
                   side_effect=fail_entering_source):
            with self.assertRaises(OSError):
                write_exports(self.tmp, self.replacement())

        # Not "source/ was left alone" -- the whole generation is back, including
        # the premiere.json that had already been overwritten.
        self.assertEqual(self.snapshot(), self.before)
        words, meta = load_words(self.tmp / "source" / "words.json")
        self.assertEqual([word.text for word in words], ["hello", "world"])
        premiere = json.loads(
            (self.tmp / "premiere.json").read_text(encoding="utf-8"))
        spoken = [word["text"] for segment in premiere["segments"]
                  for word in segment["words"]]
        self.assertNotIn("replacement", spoken)
        self.assertEqual(read_manifest(self.tmp)["generation"], meta["generation"])

    def test_staging_write_failure_changes_nothing(self):
        from vodpipe import transcript
        original = transcript._write_staged_file
        calls = 0

        def fail_third(path, text):
            nonlocal calls
            calls += 1
            if calls == 3:
                raise OSError("injected staging write failure")
            return original(path, text)

        with patch("vodpipe.transcript._write_staged_file", side_effect=fail_third):
            with self.assertRaises(OSError):
                write_exports(self.tmp, self.replacement())
        self.assertEqual(self.snapshot(), self.before)

    def test_enospc_from_staged_file_fsync_changes_nothing(self):
        with patch("vodpipe.transcript.os.fsync",
                   side_effect=OSError(errno.ENOSPC, "fsync disk full")):
            with self.assertRaises(OSError):
                write_exports(self.tmp, self.replacement())
        self.assertEqual(self.snapshot(), self.before)
        self.assertFalse((self.tmp / PUBLICATION_MARKER).exists())

    def test_replace_failure_rolls_the_whole_generation_back(self):
        from vodpipe import transcript
        original = transcript._replace_published_file
        calls = 0

        def fail_third(staged, target):
            nonlocal calls
            calls += 1
            if calls == 3:
                raise OSError("injected replace failure")
            return original(staged, target)

        with patch("vodpipe.transcript._replace_published_file",
                   side_effect=fail_third):
            with self.assertRaises(OSError):
                write_exports(self.tmp, self.replacement())
        self.assertEqual(self.snapshot(), self.before)

    def test_retirement_failure_rolls_replacements_back_too(self):
        with patch("vodpipe.transcript._retire_published_file",
                   side_effect=OSError("injected retirement failure")):
            with self.assertRaises(OSError):
                write_exports(self.tmp, [])
        self.assertEqual(self.snapshot(), self.before)

    def test_enospc_during_rollback_keeps_marker_and_every_file_intact(self):
        from vodpipe import transcript
        original_replace = transcript._replace_published_file
        calls = 0

        def fail_commit(staged, target):
            nonlocal calls
            calls += 1
            if calls == 3:
                raise OSError(errno.EIO, "injected canonical failure")
            return original_replace(staged, target)

        def no_space(backup, target):
            raise OSError(errno.ENOSPC, "disk full during rollback")

        with patch("vodpipe.transcript._replace_published_file",
                   side_effect=fail_commit), \
                patch("vodpipe.transcript._restore_backup",
                      side_effect=no_space):
            with self.assertRaises(OSError):
                write_exports(self.tmp, self.replacement())
            self.assertTrue((self.tmp / PUBLICATION_MARKER).exists())
            for name in GENERATION_FILES:
                path = self.tmp / name
                if path.exists():
                    self.assertGreater(path.stat().st_size, 0, name)
            with self.assertRaises(PublicationRecoveryError):
                load_words(self.tmp / "source" / "words.json")

        # Once space is available, the retained journal restores the exact old
        # generation rather than exposing the marker-covered mixed set.
        load_words(self.tmp / "source" / "words.json")
        self.assertFalse((self.tmp / PUBLICATION_MARKER).exists())
        self.assertEqual(self.snapshot(), self.before)

    def test_rollback_never_copies_directly_onto_a_canonical_file(self):
        from vodpipe import transcript
        original_copy = transcript.shutil.copyfile
        original_replace = transcript._replace_published_file
        destinations = []
        calls = 0

        def copy(source, destination):
            destinations.append(Path(destination))
            return original_copy(source, destination)

        def fail_commit(staged, target):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("injected commit failure")
            return original_replace(staged, target)

        with patch("vodpipe.transcript.shutil.copyfile", side_effect=copy), \
                patch("vodpipe.transcript._replace_published_file",
                      side_effect=fail_commit):
            with self.assertRaises(OSError):
                write_exports(self.tmp, self.replacement())

        canonical = {self.tmp / name for name in GENERATION_FILES}
        self.assertTrue(destinations)
        self.assertTrue(canonical.isdisjoint(destinations))
        self.assertEqual(self.snapshot(), self.before)

    def test_two_directory_partial_rollback_is_marker_covered_and_retryable(self):
        from vodpipe import transcript
        other = self.tmp.parent / f"{self.tmp.name}-other"
        other.mkdir()
        self.addCleanup(shutil.rmtree, other, True)
        write_exports(other, sample_words(), meta={"chunk": "c001"})

        def snapshot(directory):
            return {name: ((directory / name).read_bytes()
                           if (directory / name).exists() else None)
                    for name in GENERATION_FILES}

        before = {self.tmp: self.snapshot(), other: snapshot(other)}
        original_replace = transcript._replace_published_file
        original_restore = transcript._restore_backup
        failed = False

        def fail_second_directory(staged, target):
            nonlocal failed
            if target.parent == other and not failed:
                failed = True
                raise OSError(errno.EIO, "second directory commit failed")
            return original_replace(staged, target)

        def partial_restore(backup, target):
            if target.parent == other:
                raise OSError(errno.ENOSPC, "second directory rollback full")
            return original_restore(backup, target)

        publications = [
            (self.tmp, self.replacement(), {"meta": {"chunk": "c000"}}),
            (other, self.replacement(), {"meta": {"chunk": "c001"}}),
        ]
        with patch("vodpipe.transcript._replace_published_file",
                   side_effect=fail_second_directory), \
                patch("vodpipe.transcript._restore_backup",
                      side_effect=partial_restore):
            with self.assertRaises(OSError):
                write_export_sets(publications)
            self.assertTrue((self.tmp / PUBLICATION_MARKER).exists())
            self.assertTrue((other / PUBLICATION_MARKER).exists())
            with self.assertRaises(PublicationRecoveryError):
                load_words(self.tmp / "source" / "words.json")
            with self.assertRaises(PublicationRecoveryError):
                load_words(other / "source" / "words.json")

        load_words(other / "source" / "words.json")
        self.assertEqual(self.snapshot(), before[self.tmp])
        self.assertEqual(snapshot(other), before[other])
        self.assertFalse((self.tmp / PUBLICATION_MARKER).exists())
        self.assertFalse((other / PUBLICATION_MARKER).exists())

    def test_read_reconciles_a_crash_during_commit(self):
        from vodpipe import transcript
        original = transcript._replace_published_file
        calls = 0

        class Crash(BaseException):
            pass

        def crash_third(staged, target):
            nonlocal calls
            calls += 1
            if calls == 3:
                raise Crash("simulated process death")
            return original(staged, target)

        with patch("vodpipe.transcript._replace_published_file",
                   side_effect=crash_third):
            with self.assertRaises(Crash):
                write_exports(self.tmp, self.replacement())

        # read_manifest is a canonical read boundary and restores every backup
        # from the prepared journal before returning anything.
        read_manifest(self.tmp)
        self.assertEqual(self.snapshot(), self.before)

    def test_second_process_waits_for_an_active_prepared_transaction(self):
        barrier = self.tmp / "publisher-prepared.signal"
        release = self.tmp / "publisher-release.signal"
        script = r'''
import sys
import time
from pathlib import Path
from vodpipe import transcript
from vodpipe.exports import write_exports
from vodpipe.transcript import Word

directory = Path(sys.argv[1])
barrier = Path(sys.argv[2])
release = Path(sys.argv[3])
original = transcript._replace_published_file
paused = False

def replace(staged, target):
    global paused
    if not paused:
        paused = True
        barrier.write_text("prepared", encoding="utf-8")
        deadline = time.time() + 30
        while not release.exists() and time.time() < deadline:
            time.sleep(0.02)
        if not release.exists():
            raise RuntimeError("test barrier timed out")
    return original(staged, target)

transcript._replace_published_file = replace
write_exports(directory, [Word("new", 0.0, 0.4, 0.9)],
              meta={"complete": True})
'''
        process = subprocess.Popen(
            [sys.executable, "-c", script, str(self.tmp), str(barrier),
             str(release)],
            cwd=Path(__file__).resolve().parents[1],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        constructor_errors = []

        def construct_peer():
            try:
                config = Config(deep_merge(DEFAULTS, {
                    "paths": {
                        "masters_root": str(self.tmp),
                        "work_root": str(self.tmp / "work"),
                        "censor_master_list": str(self.tmp / "none.txt"),
                    },
                }), self.tmp / "config.json")
                RollingTranscriber(config, None, SessionStore(self.tmp))
            except Exception as exc:  # pragma: no cover - asserted below
                constructor_errors.append(exc)

        try:
            deadline = time.time() + 20
            while not barrier.exists() and time.time() < deadline:
                if process.poll() is not None:
                    break
                time.sleep(0.02)
            self.assertTrue(barrier.exists(), "child never reached prepared state")

            peer = threading.Thread(target=construct_peer)
            peer.start()
            time.sleep(0.3)
            self.assertTrue(peer.is_alive(),
                            "peer reconciled an actively owned transaction")

            release.write_text("continue", encoding="utf-8")
            stdout, stderr = process.communicate(timeout=30)
            peer.join(timeout=30)
            self.assertFalse(peer.is_alive(), "peer did not resume after commit")
            self.assertEqual(process.returncode, 0, stdout + stderr)
            self.assertEqual(constructor_errors, [])
            words, _ = load_words(self.tmp / "source" / "words.json")
            self.assertEqual([word.text for word in words], ["new"])
        finally:
            release.touch(exist_ok=True)
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)


class RepublishCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="vodpipe-republish-cli-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.config = Config(deep_merge(DEFAULTS, {
            "paths": {
                "masters_root": str(self.tmp / "masters"),
                "work_root": str(self.tmp / "work"),
                "censor_master_list": str(self.tmp / "none.txt"),
            },
            "tools": {
                "ffmpeg": str(self.tmp / "missing-ffmpeg.exe"),
                "ffprobe": str(self.tmp / "missing-ffprobe.exe"),
                "streamlink": str(self.tmp / "missing-streamlink.exe"),
            },
            "summary": {"provider": "none"},
        }), self.tmp / "config.json")

    def test_republish_preserves_complete_coverage_and_asr_identity(self):
        session_dir = self.config.masters_root / "chan" / "sess"
        session = Session(
            session_id="sess", channel="chan", started_at=1.0,
            directory=str(session_dir), status="complete")
        chunk = Chunk(
            index=0, session_id="sess", channel="chan", started_at=1.0,
            ts_name="chan_c000.ts", master_name="chan_c000.mp4",
            duration=7200.0, status="complete", session_offset=3600.0)
        session.chunks.append(chunk)
        SessionStore(self.config.masters_root).add(session)

        output = session_dir / "transcripts" / chunk.label
        identity = {
            "provider": "deepgram", "model": "nova-3", "language": "en",
            "filler_words": True,
        }
        original = {
            "channel": "chan",
            "session_id": "sess",
            "chunk": "c000",
            "session_offset": 3600.0,
            "language": "en",
            "asr_identity": identity,
            "updated_at": 1234.5,
            "covered_seconds": 7200.0,
            "expected_seconds": 7200.0,
            "complete": True,
        }
        save_words(output / "source" / "words.json",
                   [Word("ending", 7000.0, 0.5, 0.9)], original)

        result = cmd_republish(
            self.config, SimpleNamespace(session_id="sess"))

        self.assertEqual(result, 0)
        words, metadata = load_words(output / "source" / "words.json")
        self.assertEqual(words[-1].end, 7000.5)
        self.assertEqual(metadata["covered_seconds"], 7200.0)
        self.assertEqual(metadata["expected_seconds"], 7200.0)
        self.assertTrue(metadata["complete"])
        self.assertEqual(metadata["asr_identity"], identity)
        for key in ("channel", "session_id", "chunk", "session_offset",
                    "language", "updated_at"):
            self.assertEqual(metadata[key], original[key])

    def test_republish_never_requires_media_tools(self):
        """It re-renders stored words; ffmpeg has nothing to do with that.

        The contract is that `republish` costs nothing and touches no media, so
        it must run on a machine where ffmpeg/ffprobe/streamlink are absent.
        """
        session_dir = self.config.masters_root / "chan" / "sess"
        session = Session(
            session_id="sess", channel="chan", started_at=1.0,
            directory=str(session_dir), status="complete")
        chunk = Chunk(
            index=0, session_id="sess", channel="chan", started_at=1.0,
            ts_name="chan_c000.ts", master_name="chan_c000.mp4",
            duration=10.0, status="complete")
        session.chunks.append(chunk)
        SessionStore(self.config.masters_root).add(session)
        save_words(session_dir / "transcripts" / "c000" / "source" / "words.json",
                   [Word("hello", 0.0, 0.5, 0.9)], {
                       "complete": True, "covered_seconds": 10.0,
                       "expected_seconds": 10.0, "language": "en",
                   })
        tools = Tools(ffmpeg="", ffprobe="", streamlink="",
                      claude="configured-claude")

        with patch("vodpipe.cli.resolve_tools", return_value=tools) as resolve, \
                patch.object(RollingTranscriber, "republish", autospec=True,
                             return_value=1) as republish:
            result = cmd_republish(
                self.config, SimpleNamespace(session_id="sess"))

        self.assertEqual(result, 0)
        self.assertEqual(resolve.call_args.kwargs["need"], ())
        instance = republish.call_args.args[0]
        self.assertEqual(instance.tools.claude, "configured-claude")

    def test_cli_holds_the_chunk_resource_lock_across_republish(self):
        session_dir = self.config.masters_root / "chan" / "sess"
        session = Session(
            session_id="sess", channel="chan", started_at=1.0,
            directory=str(session_dir), status="complete")
        chunk = Chunk(
            index=0, session_id="sess", channel="chan", started_at=1.0,
            master_name="chan_c000.mp4", duration=10.0, status="complete")
        session.chunks.append(chunk)
        SessionStore(self.config.masters_root).add(session)
        save_words(session_dir / "transcripts" / "c000" / "source" / "words.json",
                   [Word("hello", 0.0, 0.5, 0.9)], {
                       "complete": True, "covered_seconds": 10.0,
                       "expected_seconds": 10.0, "language": "en",
                   })

        state = {"locked": False}

        class Lock:
            def __init__(self, *args, **kwargs):
                pass

            def acquire(self):
                state["locked"] = True
                return self

            def release(self):
                state["locked"] = False

        def republish(instance, target_session, target_chunk, **kwargs):
            self.assertTrue(state["locked"])
            return 1

        with patch("vodpipe.locks.ResourceLock", Lock), \
                patch.object(RollingTranscriber, "republish", autospec=True,
                             side_effect=republish):
            result = cmd_republish(
                self.config, SimpleNamespace(session_id="sess"))

        self.assertEqual(result, 0)
        self.assertFalse(state["locked"])


class ScriptedProvider:
    """Returns canned words, or raises, in a fixed order."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = 0

    def transcribe(self, audio: Path):
        self.calls += 1
        response = self.responses[min(self.calls - 1, len(self.responses) - 1)]
        if isinstance(response, Exception):
            raise response
        return [Word(text, start, 0.4, 0.9) for text, start in response]


class RollbackFixture(unittest.TestCase):
    """A closed chunk with a complete transcript already published."""

    @classmethod
    def setUpClass(cls):
        cls.tools = resolve_tools()
        cls.media_root = Path(tempfile.mkdtemp(prefix="vodpipe-pub-media-"))
        cls.media = cls.media_root / "chunk.mp4"
        run([cls.tools.ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
             "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=30",
             "-f", "lavfi", "-i", "sine=frequency=440",
             "-t", str(CLIP_SECONDS), "-c:v", "libx264", "-preset", "ultrafast",
             "-c:a", "aac", str(cls.media)], check=True, timeout=240)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.media_root, ignore_errors=True)

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="vodpipe-roll-"))
        self.config = Config(deep_merge(DEFAULTS, {
            "paths": {"masters_root": str(self.tmp / "m"),
                      "work_root": str(self.tmp / "w"),
                      "censor_master_list": str(self.tmp / "none.txt")},
            "transcription": {"slice_seconds": 30, "min_slice_seconds": 5,
                              "stitch_chunk_boundaries": False},
            "secrets": {"deepgram_api_key": "test-key"},
            "summary": {"provider": "none"},
        }), self.tmp / "config.json")

        self.session_dir = self.tmp / "m" / "chan" / "sess"
        (self.session_dir / "master").mkdir(parents=True)
        shutil.copy(self.media, self.session_dir / "master" / "chan_c000.mp4")

        self.store = SessionStore(self.config.masters_root)
        self.session = Session(session_id="sess", channel="chan",
                               started_at=time.time(),
                               directory=str(self.session_dir), status="complete")
        self.chunk = Chunk(index=0, session_id="sess", channel="chan",
                           started_at=time.time(), ts_name="chan_c000.ts",
                           master_name="chan_c000.mp4",
                           duration=float(CLIP_SECONDS), status="complete")
        self.session.chunks.append(self.chunk)
        self.store.add(self.session)

        self.transcriber = RollingTranscriber(self.config, self.tools, self.store)
        self.outputs = self.session_dir / "transcripts" / "c000"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def publish_a_good_transcript(self):
        self.transcriber.provider_override = ScriptedProvider(
            [("original", 1.0), ("transcript", 2.0)])
        result = self.transcriber.finalize(self.session, self.chunk)
        self.assertTrue(result.complete, result.detail)
        self.assertTrue((self.outputs / "premiere.json").exists())


class RollbackTests(RollbackFixture):
    """Driven through Pipeline.retranscribe(), not a hand-rolled equivalent.

    The rollback only means anything as part of that whole sequence -- stash,
    reset the cursor, rebuild, restore on failure -- so the tests exercise it
    rather than the pieces.
    """

    def pipeline(self):
        from vodpipe.pipeline import Pipeline

        pipeline = Pipeline(self.config)
        self.addCleanup(lambda: [pool.stop(timeout=10, drain=False)
                                 for pool in pipeline.pools])
        pipeline.store = self.store
        pipeline.transcriber = self.transcriber
        return pipeline

    def retranscribe(self, pipeline):
        job = pipeline.retranscribe(self.session, self.chunk)
        self.assertIsNotNone(job)
        deadline = time.time() + 120
        while job.status in ("queued", "running") and time.time() < deadline:
            time.sleep(0.05)
        return job

    def test_a_failed_rebuild_restores_the_previous_transcript(self):
        from vodpipe.asr import TranscriptionError

        self.publish_a_good_transcript()
        before = (self.outputs / "premiere.json").read_text(encoding="utf-8")

        pipeline = self.pipeline()
        self.transcriber.provider_override = ScriptedProvider(
            TranscriptionError("provider is down"))
        job = self.retranscribe(pipeline)
        self.assertEqual(job.status, "failed", job.error)

        words, meta = self.transcriber_words()
        self.assertEqual([word.text for word in words],
                         ["original", "transcript"])
        self.assertTrue(meta["complete"])
        # And, crucially, the file Premiere actually reads.
        self.assertEqual((self.outputs / "premiere.json").read_text(encoding="utf-8"),
                         before)
        self.assertEqual(self.chunk.word_count, 2)
        self.assertFalse(
            self.transcriber.words_path(self.session, self.chunk)
            .with_name("words.json.previous").exists(),
            "the stash should not survive the restore")

    def test_a_successful_rebuild_replaces_the_transcript(self):
        self.publish_a_good_transcript()
        pipeline = self.pipeline()
        self.transcriber.provider_override = ScriptedProvider([("rebuilt", 1.0)])
        job = self.retranscribe(pipeline)
        self.assertEqual(job.status, "done", job.error)

        words, _ = self.transcriber_words()
        self.assertEqual([word.text for word in words], ["rebuilt"])
        self.assertFalse(
            self.transcriber.words_path(self.session, self.chunk)
            .with_name("words.json.previous").exists())

    def test_restoring_when_there_was_nothing_to_stash_is_a_no_op(self):
        stash = self.transcriber.stash_words(self.session, self.chunk)
        self.assertIsNone(stash)
        self.transcriber.restore_words(self.session, self.chunk, stash)

    def test_a_rebuild_that_comes_back_empty_retires_the_old_exports(self):
        """Successful-empty, the case that is meant to delete."""
        self.publish_a_good_transcript()
        pipeline = self.pipeline()
        self.transcriber.provider_override = ScriptedProvider([])
        job = self.retranscribe(pipeline)
        self.assertEqual(job.status, "done", job.error)

        self.assertFalse((self.outputs / "premiere.json").exists())
        self.assertIn("no speech",
                      (self.outputs / "source" / "transcript.txt")
                      .read_text(encoding="utf-8"))

    def test_retranscribing_a_recording_chunk_is_refused(self):
        self.chunk.status = "recording"
        with self.assertRaises(RuntimeError):
            self.pipeline().retranscribe(self.session, self.chunk)

    def transcriber_words(self):
        from vodpipe.transcript import load_words
        return load_words(self.transcriber.words_path(self.session, self.chunk))


class RundownRetirementTests(RollbackFixture):
    """A rundown must not outlive the transcript it describes."""

    def pipeline(self):
        from vodpipe.pipeline import Pipeline

        pipeline = Pipeline(self.config)
        self.addCleanup(lambda: [pool.stop(timeout=5, drain=False)
                                 for pool in pipeline.pools])
        pipeline.store = self.store
        pipeline.transcriber = self.transcriber
        return pipeline

    def test_an_empty_transcript_retires_its_rundown(self):
        self.outputs.mkdir(parents=True, exist_ok=True)
        (self.outputs / "rundown.md").write_text("# describes speech that is gone",
                                                 encoding="utf-8")
        (self.outputs / "transcript.txt").write_text(
            "# no speech was transcribed for this chunk\n", encoding="utf-8")

        pipeline = self.pipeline()
        self.config.set("summary.provider", "claude-cli")
        pipeline._summarize(self.session, self.chunk)

        self.assertFalse((self.outputs / "rundown.md").exists())
        self.assertEqual(self.chunk.summary_status, "skipped")

    def test_turning_summaries_off_does_not_delete_existing_work(self):
        """Disabling a feature is not a request to destroy what it produced."""
        self.outputs.mkdir(parents=True, exist_ok=True)
        (self.outputs / "rundown.md").write_text("# a real rundown", encoding="utf-8")
        (self.outputs / "transcript.txt").write_text("[00:00:00] plenty of speech "
                                                     * 20, encoding="utf-8")

        pipeline = self.pipeline()
        self.config.set("summary.provider", "none")
        pipeline._summarize(self.session, self.chunk)

        self.assertTrue((self.outputs / "rundown.md").exists())
        self.assertEqual(self.chunk.summary_status, "skipped")

    def test_a_missing_transcript_retires_the_rundown(self):
        self.outputs.mkdir(parents=True, exist_ok=True)
        (self.outputs / "rundown.md").write_text("# orphan", encoding="utf-8")

        pipeline = self.pipeline()
        self.config.set("summary.provider", "claude-cli")
        pipeline._summarize(self.session, self.chunk)

        self.assertFalse((self.outputs / "rundown.md").exists())


class SessionIndexRefreshTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="vodpipe-index-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.config = Config(deep_merge(DEFAULTS, {
            "paths": {
                "masters_root": str(self.tmp / "masters"),
                "work_root": str(self.tmp / "work"),
                "censor_master_list": str(self.tmp / "none.txt"),
            },
            "summary": {"enabled": True, "provider": "claude-cli"},
        }), self.tmp / "config.json")

        from vodpipe.pipeline import Pipeline

        self.pipeline = Pipeline(self.config)
        self.addCleanup(lambda: [pool.stop(timeout=5, drain=False)
                                 for pool in self.pipeline.pools])
        directory = self.config.masters_root / "chan" / "sess"
        for child in ("master", "transcripts/c000"):
            (directory / child).mkdir(parents=True, exist_ok=True)
        self.session = Session(
            session_id="sess", channel="chan", started_at=1.0,
            directory=str(directory), status="complete")
        self.chunk = Chunk(
            index=0, session_id="sess", channel="chan", started_at=1.0,
            master_name="chan_c000.mp4", duration=65.0, size_bytes=1536,
            status="complete", width=1920, height=1080)
        self.session.chunks.append(self.chunk)
        self.pipeline.store.add(self.session)

    def index_cells(self):
        text = (self.session.path / "index.md").read_text(encoding="utf-8")
        row = next(line for line in text.splitlines()
                   if line.startswith("| c000 |"))
        return text, [cell.strip() for cell in row.strip("|").split("|")]

    def test_index_has_separate_human_size_and_resolution_columns(self):
        self.pipeline._refresh_session_index(self.session)
        text, cells = self.index_cells()

        self.assertIn("| Size | Resolution |", text)
        self.assertEqual(cells[3], "1.5 KB")
        self.assertEqual(cells[4], "1920x1080")

    def test_proxy_success_and_failure_refresh_the_final_index(self):
        def succeed(_job, session, chunk):
            self.pipeline.store.update_chunk(
                session, chunk, proxy_status="done",
                proxy_name="chan_c000_Proxy.mp4", proxy_error="")

        with patch.object(self.pipeline, "_make_proxy_inner", side_effect=succeed):
            self.pipeline._make_proxy(SimpleNamespace(), self.session, self.chunk)
        self.assertEqual(self.index_cells()[1][6], "chan_c000_Proxy.mp4")

        self.pipeline.store.update_chunk(
            self.session, self.chunk, proxy_status="pending", proxy_name="",
            proxy_error="")
        with patch.object(self.pipeline, "_make_proxy_inner",
                          side_effect=RuntimeError("encoder failed")):
            with self.assertRaisesRegex(RuntimeError, "encoder failed"):
                self.pipeline._make_proxy(
                    SimpleNamespace(), self.session, self.chunk)
        self.assertEqual(self.index_cells()[1][6], "error")

    def test_rundown_success_failure_and_retirement_refresh_the_final_index(self):
        rundown = (self.session.path / "transcripts" / "c000" / "rundown.md")

        def succeed(session, chunk, _generation):
            rundown.write_text("# current rundown", encoding="utf-8")
            self.pipeline.store.update_chunk(
                session, chunk, summary_status="done", summary_error="")

        with patch.object(self.pipeline, "_summarize_inner", side_effect=succeed):
            self.pipeline._summarize(self.session, self.chunk, "generation")
        self.assertEqual(self.index_cells()[1][8], "yes")

        rundown.unlink()
        self.pipeline.store.update_chunk(
            self.session, self.chunk, summary_status="pending", summary_error="")
        source = SimpleNamespace(generation="generation")
        with patch.object(self.pipeline, "_summary_source",
                          return_value=(source, "")), \
                patch.object(self.pipeline, "_summarize_inner",
                             side_effect=RuntimeError("provider failed")):
            with self.assertRaisesRegex(RuntimeError, "provider failed"):
                self.pipeline._summarize(
                    self.session, self.chunk, "generation")
        self.assertEqual(self.index_cells()[1][8], "error")

        rundown.write_text("# obsolete rundown", encoding="utf-8")
        self.pipeline.store.update_chunk(
            self.session, self.chunk, summary_status="done", summary_error="")
        self.pipeline._recover_summary_state(
            self.session, self.chunk, complete=False)
        self.assertFalse(rundown.exists())
        self.assertEqual(self.index_cells()[1][8], "skipped")

    def test_index_failure_does_not_change_proxy_success(self):
        def succeed(_job, session, chunk):
            self.pipeline.store.update_chunk(
                session, chunk, proxy_status="done",
                proxy_name="chan_c000_Proxy.mp4", proxy_error="")

        with patch.object(self.pipeline, "_make_proxy_inner", side_effect=succeed), \
                patch("vodpipe.pipeline.atomic_write_text",
                      side_effect=OSError("index disk failed")):
            self.pipeline._make_proxy(SimpleNamespace(), self.session, self.chunk)

        self.assertEqual(self.chunk.proxy_status, "done")
        self.assertEqual(self.chunk.proxy_name, "chan_c000_Proxy.mp4")


if __name__ == "__main__":
    unittest.main()
