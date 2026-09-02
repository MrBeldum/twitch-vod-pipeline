"""Contracts for the Windows build, all of them written after a silent failure.

Replacing `docs/logo.png` on 2026-09-01 changed the README and nothing else,
and there was no way to find that out except by looking at the Start Menu. Four
separate faults were in the way, none of which reported anything:

* the icons were six hand-exported files with no declared source, so the `.ico`
  Windows actually shows was simply older artwork;
* `ensure_host` compared the compiled exe against `host.cs` alone, so a changed
  icon never triggered a rebuild;
* `build_host` passed csc `/win32icon=...`, which that compiler rejects with
  `fatal error CS2007`, so `vodpipe install` could not compile the host at all
  -- the exe in the tree had been produced by `packaging/build.cmd`, which used
  a colon and was right;
* and nothing told the shell to drop its cached icon afterwards.

Every one of those is a build that produces the wrong artifact without saying
so, which is the only kind of packaging bug worth a test.
"""

from __future__ import annotations

import importlib.util
import os
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from vodpipe import winapp

ROOT = Path(__file__).resolve().parent.parent
PACKAGING = ROOT / "packaging"
STATIC = ROOT / "vodpipe" / "static"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, PACKAGING / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


make_icons = _load("make_icons")
prebuild = _load("prebuild")


class IconSourceTests(unittest.TestCase):
    """The shipped icons are the current logo, not a copy of it that drifted.

    Stated as a digest of the source rather than as a pixel comparison. The two
    versions of this project's own logo are the same design refined, and they
    differ by less on average than two ffmpeg builds differ when resampling the
    *same* image -- so any perceptual threshold that told them apart would be
    finer than the noise it sits in. The digest is exact and needs no decoder.
    """

    def test_the_icons_were_generated_from_the_current_logo(self):
        stamp = make_icons.STAMP
        self.assertTrue(stamp.is_file(),
                        "packaging/icons.stamp is missing; run "
                        "packaging/prebuild.py")
        recorded = stamp.read_text(encoding="utf-8").split()[0]
        self.assertEqual(
            recorded, make_icons.source_digest(),
            "docs/logo.png has changed since the icons were generated. "
            "Run `python packaging/prebuild.py` and commit the result, or the "
            "app will keep shipping the previous artwork.")

    def test_every_generated_icon_is_present(self):
        expected = [
            PACKAGING / "vodpipe.ico",
            STATIC / "favicon.ico",
            STATIC / "icon.png",
            *(PACKAGING / f"icon-{size}.png" for size in make_icons.LOOSE_PNGS),
        ]
        for path in expected:
            self.assertTrue(path.is_file(), f"{path.name} was never generated")
            self.assertGreater(path.stat().st_size, 0, f"{path.name} is empty")

    def test_the_two_copies_of_each_asset_agree(self):
        """The dashboard and the installer ship the same picture."""
        self.assertEqual((PACKAGING / "vodpipe.ico").read_bytes(),
                         (STATIC / "favicon.ico").read_bytes())
        self.assertEqual((PACKAGING / "icon-256.png").read_bytes(),
                         (STATIC / "icon.png").read_bytes())


class IcoContainerTests(unittest.TestCase):
    """The container is assembled by hand, so its shape is tested by hand."""

    def test_every_declared_size_is_present_and_addressable(self):
        data = (PACKAGING / "vodpipe.ico").read_bytes()
        reserved, kind, count = struct.unpack("<HHH", data[:6])
        self.assertEqual((reserved, kind), (0, 1))
        self.assertEqual(count, len(make_icons.ICO_SIZES))
        seen = []
        for index in range(count):
            offset = 6 + 16 * index
            (width, _height, _colours, _reserved, _planes, bits,
             length, pointer) = struct.unpack("<BBBBHHII",
                                              data[offset:offset + 16])
            seen.append(width or 256)
            self.assertEqual(bits, 32)
            self.assertLessEqual(pointer + length, len(data),
                                 "an entry points past the end of the file")
        self.assertEqual(seen, list(make_icons.ICO_SIZES))

    def test_a_bitmap_entry_declares_double_height_for_its_mask(self):
        """The classic way to ship an icon Windows draws as a black square."""
        rgba = bytes([0, 0, 0, 255]) * (16 * 16)
        entry = make_icons.dib_entry(rgba, 16)
        size, width, height, planes, bits = struct.unpack("<IiiHH", entry[:16])
        self.assertEqual((size, width, height, planes, bits),
                         (40, 16, 32, 1, 32))

    def test_a_bitmap_entry_is_bgra_and_bottom_up(self):
        # One red pixel in the top-left of a 2x2 image.
        rgba = bytes([255, 0, 0, 255]) + bytes([0, 0, 0, 0]) * 3
        entry = make_icons.dib_entry(rgba, 2)
        pixels = entry[40:40 + 16]
        # Bottom-up: the top row is stored last. BGRA: blue byte first.
        self.assertEqual(pixels[8:12], bytes([0, 0, 255, 255]))


class HostBuildTests(unittest.TestCase):
    def test_the_icon_is_a_build_input(self):
        """It is compiled in with /win32icon, so it decides staleness."""
        names = {path.name for path in winapp.build_inputs()}
        self.assertIn("vodpipe.ico", names)
        self.assertIn("host.cs", names)
        self.assertIn("version.g.cs", names)

    def test_a_newer_icon_rebuilds_the_host(self):
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            exe = tmp / "VODPipeline.exe"
            icon = tmp / "vodpipe.ico"
            source = tmp / "host.cs"
            for path in (source, exe, icon):
                path.write_bytes(b"x")
            source_time, exe_time, icon_time = 1000.0, 2000.0, 3000.0
            os.utime(source, (source_time, source_time))
            os.utime(exe, (exe_time, exe_time))
            os.utime(icon, (icon_time, icon_time))

            with patch.object(winapp, "host_path", return_value=exe), \
                    patch.object(winapp, "build_inputs",
                                 return_value=[source, icon]), \
                    patch.object(winapp, "build_host",
                                 return_value=exe) as build:
                winapp.ensure_host()
            build.assert_called_once()

    def test_an_unchanged_tree_does_not_rebuild(self):
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            exe = tmp / "VODPipeline.exe"
            source = tmp / "host.cs"
            for path in (source, exe):
                path.write_bytes(b"x")
            os.utime(source, (1000.0, 1000.0))
            os.utime(exe, (2000.0, 2000.0))
            with patch.object(winapp, "host_path", return_value=exe), \
                    patch.object(winapp, "build_inputs", return_value=[source]), \
                    patch.object(winapp, "build_host") as build:
                winapp.ensure_host()
            build.assert_not_called()

    def test_csc_options_are_separated_by_a_colon(self):
        """`/win32icon=...` is `fatal error CS2007` on the .NET Framework csc.

        `packaging/build.cmd` always used a colon and `build_host` did not, so
        the two build paths disagreed and only the one nobody ran was broken.
        """
        captured = {}

        class Done:
            returncode = 0
            stdout = ""
            stderr = ""

        def fake_run(argv, **kwargs):
            captured["argv"] = argv
            return Done()

        with patch.object(winapp, "find_csc", return_value="csc.exe"), \
                patch.object(winapp, "run_prebuild"), \
                patch.object(winapp.subprocess, "run", side_effect=fake_run):
            winapp.build_host()

        argv = captured["argv"]
        for option in ("/win32icon", "/win32manifest", "/out"):
            matched = [part for part in argv if part.startswith(option)]
            self.assertTrue(matched, f"{option} missing from the csc command")
            self.assertTrue(
                matched[0].startswith(option + ":"),
                f"{option} must be separated by ':', got {matched[0][:40]!r}")
        self.assertTrue(any(part.endswith("version.g.cs") for part in argv),
                        "the generated version file is not compiled in")


class GeneratedVersionTests(unittest.TestCase):
    def test_the_generated_version_matches_the_package(self):
        from vodpipe import __version__
        self.assertEqual(prebuild.package_version(), __version__)
        text = (PACKAGING / "version.g.cs").read_text(encoding="utf-8")
        self.assertIn(f'public const string Version = "{__version__}";', text)
        self.assertIn(f'[assembly: AssemblyInformationalVersion("{__version__}")]',
                      text)

    def test_a_four_part_file_version_is_produced(self):
        self.assertEqual(prebuild.four_part("1.0.2"), "1.0.2.0")
        self.assertEqual(prebuild.four_part("2.1"), "2.1.0.0")
        self.assertEqual(prebuild.four_part("1.2.3.4"), "1.2.3.4")

    def test_writing_the_version_twice_does_not_touch_the_file(self):
        """It is a build input; rewriting it would rebuild the host every run."""
        path = PACKAGING / "version.g.cs"
        before = path.stat().st_mtime_ns
        prebuild.write_version()
        self.assertEqual(path.stat().st_mtime_ns, before)


class RetiredAssetTests(unittest.TestCase):
    def test_the_dashboard_does_not_reference_the_retired_svg(self):
        """`icon.svg` was a seventh, hand-drawn mark that matched nothing."""
        self.assertFalse((STATIC / "icon.svg").exists())
        for name in ("index.html", "manifest.webmanifest"):
            text = (STATIC / name).read_text(encoding="utf-8")
            self.assertNotIn("icon.svg", text, f"{name} still asks for icon.svg")


if __name__ == "__main__":
    unittest.main()
