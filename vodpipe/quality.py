"""What resolution did we actually capture, and was that the best on offer?

A capture that silently lands at 720p when the operator expected 1080p is worse
than one that is refused outright: it is discovered hours later, after the
broadcast has ended and the only chance to record it is gone. That happened --
four consecutive two-hour chunks were written at 1280x720 and nothing in the
product said so.

streamlink announces both halves of the story on stderr:

    [cli][info] Available streams: audio_only, 160p (worst), 360p, 480p, 720p60 (best)
    [cli][info] Opening stream: 720p60 (hls)

Parsing both is what separates the two very different causes of a low-resolution
master, and the distinction is the entire point of this module:

* **We chose badly.** A better rendition was on the ladder and we did not take
  it. That is our bug or a misconfiguration, and it is fixable.
* **Twitch offered nothing better.** The ladder itself topped out below the
  floor. Nothing in this codebase can improve it; recording anyway is right, but
  saying so plainly is mandatory.

Verified 2026-08-14, and the cause is regional. From a **South Korean IP** Twitch
serves no `source` rendition at all -- every variant on every channel tested came
back marked `IVS-VARIANT-SOURCE="transcode"`, and the master playlist carried
`USER-COUNTRY="KR"`. What is left is the channel's transcode ladder, so the
ceiling depends on which stack it is on: `2025-Transcode-ELT-V1` tops out at
720p60, a custom stack can reach 1080p60. Connecting from Japan over a VPN
restores the `source` rendition and with it true source quality.

So a low capture has two independent causes worth telling apart, and neither is
a bad setting:

* **No source rendition offered** (Korean IP). Fixed by connecting from another
  region, not by any value in this config.
* **The transcode ladder itself is short.** Even with source available, some
  channels simply have no tall transcode.

See README, "Why a recording can be 720p".
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# `Available streams: audio_only, 160p (worst), 360p, 480p, 720p60 (best)`
_AVAILABLE = re.compile(r"available streams:\s*(?P<list>.+?)\s*$", re.IGNORECASE)
# `Opening stream: 720p60 (hls)` -- the trailing parenthetical is the stream type.
_OPENING = re.compile(r"opening stream:\s*(?P<name>[^\s(]+)", re.IGNORECASE)
# streamlink annotates the ends of the ladder: `160p (worst)`, `720p60 (best)`,
# and on a single-rendition ladder both at once: `720p60 (worst, best)`. That
# last form contains a comma, so annotations are stripped from the whole listing
# *before* it is split -- splitting first tears `(worst, best)` in half and loses
# the rendition it was attached to.
_ANNOTATION = re.compile(
    r"\s*\(\s*(?:worst|best)(?:\s*,\s*(?:worst|best))*\s*\)", re.IGNORECASE)
# `1080p60` -> (1080, 60); `480p` -> (480, 0). Twitch's newer ladders omit the
# frame rate on the lower renditions, so fps is genuinely optional.
_RENDITION = re.compile(r"^(?P<height>\d{2,5})p(?P<fps>\d{1,3})?$", re.IGNORECASE)

# Renditions that carry no video. Never treated as a shortfall -- they are not a
# degraded picture, they are the absence of one, and `audio_only` on the ladder
# is normal and expected.
AUDIO_ONLY = {"audio_only", "audio"}

# streamlink resolves these against the ladder rather than naming a rendition, so
# their height is not knowable from the name alone.
_ALIASES = {"best", "worst", "source", "live"}

LOW_QUALITY_POLICIES = ("warn", "refuse")


def parse_available(line: str) -> list[str] | None:
    """Rendition names from a streamlink `Available streams:` line.

    Returns None when the line is not one, which is how the log pump tells
    "nothing to do" apart from "an empty ladder".
    """
    match = _AVAILABLE.search(line or "")
    if not match:
        return None
    listing = _ANNOTATION.sub("", match.group("list"))
    names = []
    for raw in listing.split(","):
        name = raw.strip()
        # Anything still carrying a bracket is an annotation shape we do not
        # recognise; dropping it beats inventing a rendition name from it.
        if not name or "(" in name or ")" in name:
            continue
        names.append(name)
    return names


def parse_opening(line: str) -> str | None:
    """The rendition name from a streamlink `Opening stream:` line."""
    match = _OPENING.search(line or "")
    return match.group("name") if match else None


def rendition_height(name: str) -> int:
    """Vertical resolution implied by a rendition name.

    0 for audio-only and for names whose height cannot be known from the name
    alone (`best`, `source`). Callers must not read 0 as "low quality".
    """
    key = (name or "").strip().lower()
    if not key or key in AUDIO_ONLY or key in _ALIASES:
        return 0
    match = _RENDITION.match(key)
    return int(match.group("height")) if match else 0


def rendition_fps(name: str) -> int:
    """Frame rate implied by a rendition name, or 0 when it does not carry one."""
    key = (name or "").strip().lower()
    match = _RENDITION.match(key)
    if not match:
        return 0
    return int(match.group("fps") or 0)


def best_height(names: list[str]) -> int:
    """Tallest rendition on a ladder. 0 when none of them state a height."""
    return max((rendition_height(name) for name in names), default=0)


@dataclass
class QualityReport:
    """What was on offer, what we took, and whether that clears the floor."""

    selected: str = ""
    available: list[str] = field(default_factory=list)
    floor: int = 0

    @property
    def height(self) -> int:
        return rendition_height(self.selected)

    @property
    def fps(self) -> int:
        return rendition_fps(self.selected)

    @property
    def best_available(self) -> int:
        return best_height(self.available)

    @property
    def known(self) -> bool:
        """True once we can actually judge the capture."""
        return self.height > 0

    @property
    def meets_floor(self) -> bool:
        return self.floor <= 0 or not self.known or self.height >= self.floor

    @property
    def capped_by_twitch(self) -> bool:
        """The ladder itself topped out below the floor.

        When this is true no configuration change can help, so the message must
        not suggest one. When it is false a better rendition was available and
        we failed to take it -- a genuine defect on our side.
        """
        return (not self.meets_floor
                and self.best_available > 0
                and self.best_available < self.floor)

    def describe(self) -> str:
        """One line for the log, the dashboard and `index.md`. Empty when fine."""
        if self.meets_floor or not self.known:
            return ""
        ladder = ", ".join(self.available) or "unknown"
        if self.capped_by_twitch:
            return (
                f"recording at {self.selected} ({self.height}p): Twitch offered "
                f"nothing better for this channel (ladder: {ladder}). No setting "
                f"here can raise it. From a South Korean IP Twitch withholds the "
                f"source rendition entirely, so only transcodes are on offer -- "
                f"connecting from another region (a VPN to Japan was confirmed to "
                f"work) restores source quality. See README, 'Why a recording can "
                f"be 720p'."
            )
        return (
            f"recording at {self.selected} ({self.height}p) but "
            f"{self.best_available}p was available (ladder: {ladder}). Check "
            f"recording.quality."
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "selected": self.selected,
            "available": list(self.available),
            "height": self.height,
            "fps": self.fps,
            "best_available": self.best_available,
            "floor": self.floor,
            "meets_floor": self.meets_floor,
            "capped_by_twitch": self.capped_by_twitch,
            "warning": self.describe(),
        }
