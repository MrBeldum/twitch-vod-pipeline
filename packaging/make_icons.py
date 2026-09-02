"""Derive every icon the app ships from one source image.

`docs/logo.png` is the source of truth. Everything else -- the Windows icon
compiled into `VODPipeline.exe`, the Start Menu and taskbar icon that comes with
it, the dashboard's favicon and header mark -- is generated from it by this
script.

**Why this exists.** The icons used to be six independent files, hand-exported
and separately committed, and nothing said so. Replacing the logo therefore
changed the README and nothing else: the `.ico` Windows actually displays still
held the previous artwork, and no build step would ever have noticed. "I updated
the icon and it did not take effect" was the correct description of a repository
in which updating the icon genuinely did nothing.

Scaling is done by ffmpeg, which the pipeline already requires, so this stays
inside the stdlib-only rule -- there is no Pillow here and there must not be.
The ICO container is assembled by hand because that is a header and a pixel
buffer, not a reason to take a dependency.

Run it directly (`python packaging/make_icons.py`) or let `vodpipe install` run
it, which it does before every compile.
"""

from __future__ import annotations

import hashlib
import shutil
import struct
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGING = ROOT / "packaging"
STATIC = ROOT / "vodpipe" / "static"
SOURCE = ROOT / "docs" / "logo.png"

# Windows picks from these: 16 in the title bar, 32 on the desktop, 48 in
# Explorer, 256 for the extra-large view and the Start tile. 24/64/128 are
# cheap and stop Windows from producing its own blurry downscale.
ICO_SIZES = (16, 24, 32, 48, 64, 128, 256)

# Above this, an entry goes into the ICO as PNG rather than as a bitmap. A
# 256x256 BGRA bitmap is 256 KB on its own, and every Windows that can show a
# 256px icon reads PNG entries.
PNG_ENTRY_FROM = 128

# Sizes kept as loose PNGs beside the ICO, for the tile manifest and anything
# that wants a plain image rather than an icon container.
LOOSE_PNGS = (16, 32, 48, 256)

# The digest of the source these outputs were generated from. It is what lets a
# test say "the icons are stale" exactly, without re-running ffmpeg and
# comparing pixels -- two ffmpeg builds do not resample identically, and the
# two versions of this project's own logo differ by less than that margin, so a
# perceptual threshold that separated them would be too fine to trust.
STAMP = PACKAGING / "icons.stamp"


class IconError(RuntimeError):
    pass


def source_digest() -> str:
    return hashlib.sha256(SOURCE.read_bytes()).hexdigest()


def find_ffmpeg() -> str:
    found = shutil.which("ffmpeg")
    if not found:
        raise IconError("ffmpeg is not on PATH; it is what scales the icons")
    return found


def _scaled_rgba(ffmpeg: str, size: int) -> bytes:
    """`size`x`size` raw RGBA, top row first, straight out of ffmpeg."""
    argv = [
        ffmpeg, "-v", "error", "-y", "-i", str(SOURCE),
        # `lanczos` keeps the film-strip perforations legible at 16px, where a
        # bilinear downscale turns them into grey mush.
        "-vf", f"scale={size}:{size}:flags=lanczos",
        "-pix_fmt", "rgba", "-f", "rawvideo", "-",
    ]
    done = subprocess.run(argv, capture_output=True)
    if done.returncode != 0:
        raise IconError(f"ffmpeg failed scaling to {size}px: "
                        f"{done.stderr.decode('utf-8', 'replace').strip()}")
    expected = size * size * 4
    if len(done.stdout) != expected:
        raise IconError(f"expected {expected} bytes at {size}px, "
                        f"got {len(done.stdout)}")
    return done.stdout


def _write_png(ffmpeg: str, size: int, dest: Path) -> None:
    argv = [
        ffmpeg, "-v", "error", "-y", "-i", str(SOURCE),
        "-vf", f"scale={size}:{size}:flags=lanczos",
        "-pix_fmt", "rgba", str(dest),
    ]
    done = subprocess.run(argv, capture_output=True)
    if done.returncode != 0:
        raise IconError(f"ffmpeg failed writing {dest.name}: "
                        f"{done.stderr.decode('utf-8', 'replace').strip()}")


def dib_entry(rgba: bytes, size: int) -> bytes:
    """One ICO image in BMP form: BITMAPINFOHEADER, BGRA rows, AND mask.

    Three details that are easy to get wrong and silently produce an icon
    Windows renders as a black square: the height in the header is *doubled*
    because it describes the colour rows plus the mask rows; the rows are
    stored bottom-up; and the mask must be present and padded to four bytes a
    row even for a 32-bit icon whose transparency lives in the alpha channel.
    """
    mask_stride = ((size + 31) // 32) * 4
    mask_length = mask_stride * size
    header = struct.pack(
        "<IiiHHIIiiII",
        40,                 # biSize
        size,               # biWidth
        size * 2,           # biHeight: colour rows + mask rows
        1,                  # biPlanes
        32,                 # biBitCount
        0,                  # biCompression, BI_RGB
        size * size * 4 + mask_length,
        0, 0, 0, 0,
    )
    rows = []
    for y in range(size - 1, -1, -1):          # bottom-up
        row = bytearray(size * 4)
        base = y * size * 4
        for x in range(size):
            r, g, b, a = rgba[base + x * 4: base + x * 4 + 4]
            row[x * 4: x * 4 + 4] = bytes((b, g, r, a))
        rows.append(bytes(row))
    # All-zero mask: "opaque everywhere", leaving the alpha channel in charge.
    return header + b"".join(rows) + bytes(mask_length)


def build_ico(images: list[tuple[int, bytes]]) -> bytes:
    """An ICO file from (size, payload) pairs, in the order given."""
    count = len(images)
    offset = 6 + 16 * count
    directory = bytearray()
    body = bytearray()
    for size, payload in images:
        directory += struct.pack(
            "<BBBBHHII",
            0 if size >= 256 else size,     # 0 means 256
            0 if size >= 256 else size,
            0,                              # colours in palette
            0,                              # reserved
            1,                              # planes
            32,                             # bits per pixel
            len(payload),
            offset,
        )
        body += payload
        offset += len(payload)
    return struct.pack("<HHH", 0, 1, count) + bytes(directory) + bytes(body)


def main() -> int:
    if not SOURCE.is_file():
        print(f"source image missing: {SOURCE}", file=sys.stderr)
        return 1
    try:
        ffmpeg = find_ffmpeg()
        images: list[tuple[int, bytes]] = []
        for size in ICO_SIZES:
            if size >= PNG_ENTRY_FROM:
                staged = PACKAGING / f".icon-{size}.stage.png"
                _write_png(ffmpeg, size, staged)
                images.append((size, staged.read_bytes()))
                staged.unlink(missing_ok=True)
            else:
                images.append((size, dib_entry(_scaled_rgba(ffmpeg, size), size)))

        ico = build_ico(images)
        written: list[Path] = []
        for dest in (PACKAGING / "vodpipe.ico", STATIC / "favicon.ico"):
            dest.write_bytes(ico)
            written.append(dest)

        for size in LOOSE_PNGS:
            dest = PACKAGING / f"icon-{size}.png"
            _write_png(ffmpeg, size, dest)
            written.append(dest)
        shutil.copyfile(PACKAGING / "icon-256.png", STATIC / "icon.png")
        written.append(STATIC / "icon.png")

        STAMP.write_text(f"{source_digest()}  {SOURCE.name}\n", encoding="utf-8")
        written.append(STAMP)
    except IconError as exc:
        print(f"icons not rebuilt: {exc}", file=sys.stderr)
        return 1

    for path in written:
        print(f"  {path.relative_to(ROOT)}  ({path.stat().st_size} bytes)")
    print(f"icons rebuilt from {SOURCE.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
