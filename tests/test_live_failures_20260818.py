"""Regressions for the three failures of the 2026-08-18 recordings.

Two eight-hour sessions produced eight masters. Three of them went wrong, in
three different ways, and every one of them was reported as something it was
not:

* **examplechannel c002** died in the remux on an ffmpeg assertion --
  ``next_dts <= 0x7fffffff`` in movenc's `get_cluster_duration`, i.e. one
  sample's duration overflowing a 32-bit field, which is a DTS gap of more than
  six hours inside a two-hour file. Re-running the identical command over the
  identical bytes afterwards produced a perfect master, so the number came from
  somewhere other than the recording. There was no retry, so the chunk kept a
  `.ts`, its master was never built, and its proxy failed behind it with
  "master is missing".
* **otherchannel c000** published a master whose sample-to-chunk map sent every sample
  after one entry to the wrong bytes: 31 MB of ``Invalid NAL unit size`` on a
  file whose header was immaculate. `validate_master` passed it and the `.ts`
  was deleted.
* **otherchannel c001** published a master with one chunk offset carrying a spurious
  high bit, so the demuxer stopped delivering packets at 3159s of a 7199s file
  -- in silence, because as far as it was concerned the index simply ended.
  `validate_master` passed that too, and that `.ts` was deleted as well.

The last two then failed twice each in the proxy encoder, which reported
"h264_amf failed on real media", fell back to libx264, and spent another five
minutes proving that software could not read the file either.

The fixtures below reproduce both corruptions byte for byte on real media: a
flipped bit in a chunk offset, and a sample-to-chunk run that no longer
describes the samples that exist.
"""

from __future__ import annotations

import shutil
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from vodpipe.jobs import Job
from vodpipe.media import remux_to_mp4, validate_master, verify_master_readable
from vodpipe.util import resolve_tools, run

from tests.test_remaining_contracts import PipelineFixture

FIXTURE_SECONDS = 6
FIXTURE_FPS = 30


def _find_box(path: Path, want: bytes) -> tuple[int, bytes] | None:
    """(file offset of the box body, the body) for the first `want` in moov."""
    data = path.read_bytes()

    def walk(buf: bytes, base: int):
        pos = 0
        while pos + 8 <= len(buf):
            size = struct.unpack(">I", buf[pos:pos + 4])[0]
            kind = buf[pos + 4:pos + 8]
            header = 8
            if size == 1:
                size = struct.unpack(">Q", buf[pos + 8:pos + 16])[0]
                header = 16
            elif size == 0:
                size = len(buf) - pos
            if size < header or pos + size > len(buf):
                return None
            if kind == want:
                return base + pos + header, buf[pos + header:pos + size]
            if kind in (b"moov", b"trak", b"mdia", b"minf", b"stbl"):
                found = walk(buf[pos + header:pos + size], base + pos + header)
                if found:
                    return found
            pos += size
        return None

    return walk(data, 0)


def _patch_u32(path: Path, offset: int, value: int) -> int:
    previous = struct.unpack(">I", path.read_bytes()[offset:offset + 4])[0]
    with path.open("r+b") as handle:
        handle.seek(offset)
        handle.write(struct.pack(">I", value))
    return previous


class MasterIntegrityTests(unittest.TestCase):
    """A master must be readable, not merely well-described."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="vodpipe-integrity-"))
        cls.tools = resolve_tools()
        cls.ts = cls.tmp / "src.ts"
        run([cls.tools.ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
             "-f", "lavfi", "-i", f"testsrc2=size=320x180:rate={FIXTURE_FPS}",
             "-f", "lavfi", "-i", "sine=frequency=440",
             "-t", str(FIXTURE_SECONDS),
             "-c:v", "libx264", "-preset", "ultrafast", "-g", str(FIXTURE_FPS),
             "-c:a", "aac", "-f", "mpegts", str(cls.ts)],
            check=True, timeout=300)
        cls.master = cls.tmp / "master.mp4"
        remux_to_mp4(cls.tools, cls.ts, cls.master, float(FIXTURE_SECONDS))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def copy(self, name: str) -> Path:
        target = Path(tempfile.mkdtemp(dir=self.tmp)) / name
        target.write_bytes(self.master.read_bytes())
        return target

    def test_a_clean_master_passes_both_checks(self):
        validate_master(self.tools, self.master, float(FIXTURE_SECONDS))
        verify_master_readable(self.tools, self.master)

    def test_a_chunk_offset_with_a_stray_high_bit_is_caught(self):
        """otherchannel c001: co64 entry 136075 read 2**45 + its real value.

        The demuxer stopped there and said nothing, so every metadata check
        still described a complete file. Only counting what it actually hands
        over finds this.
        """
        damaged = self.copy("short_index.mp4")
        found = _find_box(damaged, b"stco") or _find_box(damaged, b"co64")
        self.assertIsNotNone(found, "fixture has no chunk offset table")
        offset, body = found
        entries = struct.unpack(">I", body[4:8])[0]
        self.assertGreater(entries, 4, "fixture has too few chunks to damage")
        target = entries // 2
        where = offset + 8 + 4 * target
        previous = _patch_u32(
            damaged, where,
            struct.unpack(">I", damaged.read_bytes()[where:where + 4])[0]
            | (1 << 30))

        # The header still says everything is fine. That is the whole problem.
        validate_master(self.tools, damaged, float(FIXTURE_SECONDS))
        with self.assertRaises(RuntimeError) as caught:
            verify_master_readable(self.tools, damaged)
        message = str(caught.exception)
        self.assertIn("index only reaches", message)
        self.assertIn("damaged", message)
        self.assertGreater(previous, 0)

    def test_a_sample_to_chunk_map_that_lies_is_caught(self):
        """otherchannel c000: one stsc entry, and every sample after it misaddressed.

        This one delivers the full packet count at the right timestamps, so the
        span check passes and only the clean-read rule sees it.
        """
        damaged = self.copy("garbled.mp4")
        offset, body = _find_box(damaged, b"stsc")
        entries = struct.unpack(">I", body[4:8])[0]
        if entries >= 2:
            target = entries // 2
            where = offset + 8 + 12 * target
            first = struct.unpack(">I", damaged.read_bytes()[where:where + 4])[0]
            _patch_u32(damaged, where, max(1, first // 8))
        else:
            where = offset + 8 + 4          # samples_per_chunk of the only run
            spc = struct.unpack(">I", damaged.read_bytes()[where:where + 4])[0]
            _patch_u32(damaged, where, spc + 3)

        validate_master(self.tools, damaged, float(FIXTURE_SECONDS))
        with self.assertRaises(RuntimeError) as caught:
            verify_master_readable(self.tools, damaged)
        self.assertIn("does not read cleanly", str(caught.exception))

    def test_a_failed_verification_publishes_nothing(self):
        """The staged candidate is discarded, so a previous master survives."""
        destination = Path(tempfile.mkdtemp(dir=self.tmp)) / "out.mp4"
        with patch("vodpipe.media.verify_master_readable",
                   side_effect=RuntimeError("index only reaches 3.000s")):
            with self.assertRaisesRegex(RuntimeError, "index only reaches"):
                remux_to_mp4(self.tools, self.ts, destination,
                             float(FIXTURE_SECONDS))
        self.assertFalse(destination.exists())
        self.assertFalse(destination.with_suffix(".partial.mp4").exists())


class RemuxRetryTests(PipelineFixture):
    """examplechannel c002: one unclassifiable ffmpeg abort lost a master for good."""

    def prepare(self, index: int = 0, duration: float = 10.0):
        chunk = self.chunk(index=index, duration=duration)
        source = self.directory / "live" / chunk.ts_name
        source.write_bytes(b"working copy")
        return chunk, source

    def test_a_failed_attempt_is_repeated_and_can_succeed(self):
        chunk, source = self.prepare()
        master = self.directory / "master" / chunk.master_name
        calls = []

        def remux(tools, src, destination, expected, *, verify=True):
            calls.append(destination)
            if len(calls) < 3:
                raise RuntimeError(
                    "remux failed: Assertion next_dts <= 0x7fffffff failed")
            destination.write_bytes(b"master")
            return []

        with patch("vodpipe.pipeline.remux_to_mp4", side_effect=remux), \
                patch("vodpipe.pipeline.video_dimensions", return_value=(1920, 1080)):
            self.pipeline._remux(self.session, chunk)

        self.assertEqual(len(calls), 3)
        self.assertTrue(master.exists())
        self.assertEqual(chunk.master_error, "")
        self.assertFalse(source.exists(), "the .ts is reclaimed once it is safe")

    def test_retries_are_bounded_and_the_last_error_is_reported(self):
        chunk, source = self.prepare()
        calls = []

        def remux(*args, **kwargs):
            calls.append(1)
            raise RuntimeError("remux failed: no")

        with patch("vodpipe.pipeline.remux_to_mp4", side_effect=remux):
            self.pipeline._remux(self.session, chunk)

        self.assertEqual(len(calls),
                         int(self.config.get("recording.remux_attempts")))
        self.assertIn("remux failed", chunk.master_error)
        self.assertTrue(source.exists(), "the .ts is the only copy left")

    def test_one_attempt_can_be_configured(self):
        self.config.set("recording.remux_attempts", 1)
        chunk, _ = self.prepare()
        calls = []
        with patch("vodpipe.pipeline.remux_to_mp4",
                   side_effect=lambda *a, **k: calls.append(1) or (_ for _ in ()).throw(
                       RuntimeError("remux failed: no"))):
            self.pipeline._remux(self.session, chunk)
        self.assertEqual(len(calls), 1)

    def test_verification_is_requested_by_default_and_can_be_switched_off(self):
        seen = []

        def remux(tools, src, destination, expected, *, verify=True):
            seen.append(verify)
            destination.write_bytes(b"master")
            return []

        with patch("vodpipe.pipeline.remux_to_mp4", side_effect=remux), \
                patch("vodpipe.pipeline.video_dimensions", return_value=(1, 1)):
            first, _ = self.prepare(index=0)
            self.pipeline._remux(self.session, first)
            self.config.set("recording.verify_master", False)
            second, _ = self.prepare(index=1)
            self.pipeline._remux(self.session, second)
        self.assertEqual(seen, [True, False])


class ReclaimGuardTests(PipelineFixture):
    """The deep read is paid for exactly when the `.ts` is about to go."""

    def test_an_unreadable_adopted_master_does_not_cost_the_ts(self):
        chunk = self.chunk()
        source = self.directory / "live" / chunk.ts_name
        master = self.directory / "master" / chunk.master_name
        source.write_bytes(b"working copy")
        master.write_bytes(b"x" * 2048)
        rebuilt = []

        def remux(tools, src, destination, expected, *, verify=True):
            rebuilt.append(src)
            raise RuntimeError("remux failed: still broken")

        with patch("vodpipe.pipeline.validate_master"), \
                patch("vodpipe.pipeline.verify_master_readable",
                      side_effect=RuntimeError("index only reaches 3.000s")), \
                patch("vodpipe.pipeline.remux_to_mp4", side_effect=remux):
            self.pipeline._remux(self.session, chunk)

        self.assertTrue(source.exists())
        self.assertTrue(rebuilt, "an unreadable master is rebuilt, not adopted")

    def test_a_master_with_no_ts_left_is_not_re_read(self):
        """Recovery walks every session on disk; re-reading finished masters
        would turn startup into a disk scrub for a verdict nothing can act on."""
        chunk = self.chunk()
        master = self.directory / "master" / chunk.master_name
        master.write_bytes(b"x" * 2048)

        with patch("vodpipe.pipeline.validate_master"), \
                patch("vodpipe.pipeline.verify_master_readable") as deep, \
                patch("vodpipe.pipeline.video_dimensions", return_value=(1, 1)):
            self.pipeline._remux(self.session, chunk)
        deep.assert_not_called()


class ProxyDiagnosisTests(PipelineFixture):
    """"h264_amf failed on real media" was the wrong thing to say twice."""

    def build(self, chunk):
        master = self.directory / "master" / chunk.master_name
        master.write_bytes(b"x" * 2048)
        destination = self.directory / "master" / "Proxies" / "p.mp4"
        destination.parent.mkdir(parents=True, exist_ok=True)
        job = Job("proxy:test", "proxy", "proxy")
        return job, master, destination

    def test_a_damaged_master_is_named_and_no_second_encoder_is_tried(self):
        chunk = self.chunk()
        job, master, destination = self.build(chunk)
        encodes = []

        with patch("vodpipe.pipeline.probe_encoder", return_value="h264_amf"), \
                patch("vodpipe.pipeline.estimate_proxy_peak_bytes", return_value=0), \
                patch("vodpipe.pipeline.make_proxy",
                      side_effect=lambda *a, **k: encodes.append(k.get("encoder"))
                      or (_ for _ in ()).throw(RuntimeError(
                          "proxy candidate video stream 0 covers 2765.483s"))), \
                patch("vodpipe.pipeline.verify_master_readable",
                      side_effect=RuntimeError(
                          "master video stream 0 declares 7201.950s but its "
                          "index only reaches 2765.483s")):
            with self.assertRaises(RuntimeError) as caught:
                self.pipeline._build_proxy(job, self.session, chunk, master,
                                           destination, 540)

        self.assertEqual(encodes, ["h264_amf"], "software fallback was pointless")
        message = str(caught.exception)
        self.assertIn("the master is damaged", message)
        self.assertIn("index only reaches", message)
        self.assertIn("damaged", chunk.proxy_error)

    def test_a_readable_master_still_falls_back_to_libx264(self):
        chunk = self.chunk()
        job, master, destination = self.build(chunk)
        encodes = []

        def encode(tools, src, dst, *, encoder="libx264", **options):
            encodes.append(encoder)
            if encoder != "libx264":
                raise RuntimeError("h264_amf gave up")
            dst.write_bytes(b"proxy")

        with patch("vodpipe.pipeline.probe_encoder", return_value="h264_amf"), \
                patch("vodpipe.pipeline.estimate_proxy_peak_bytes", return_value=0), \
                patch("vodpipe.pipeline.make_proxy", side_effect=encode), \
                patch("vodpipe.pipeline.verify_master_readable"):
            self.pipeline._build_proxy(job, self.session, chunk, master,
                                       destination, 540)

        self.assertEqual(encodes, ["h264_amf", "libx264"])
        self.assertEqual(chunk.proxy_error, "")


if __name__ == "__main__":
    unittest.main()
