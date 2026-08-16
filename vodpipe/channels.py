"""Twitch channel parsing and validation.

Every channel name becomes a directory name, a session id, a filename prefix and
part of a deletion glob. Accepting arbitrary text there means `../..`, `C:\\`,
a UNC path, or a Windows reserved name reaches the filesystem. One parser, used
everywhere, is the containment boundary.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

# Twitch logins are ASCII letters, digits and underscore. Modern minimum is 4;
# a handful of legacy accounts are 3, so the floor is 3.
LOGIN = re.compile(r"^[a-z0-9_]{3,25}$")

# The only hosts a channel address may name. Compared against urlparse's
# .hostname, never against a substring of the raw input.
TWITCH_HOSTS = frozenset({"twitch.tv", "www.twitch.tv", "m.twitch.tv"})

# Device names Windows refuses to treat as ordinary files, at any extension.
RESERVED = {
    "con", "prn", "aux", "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}


class InvalidChannel(ValueError):
    pass


class InvalidVod(ValueError):
    pass


# A Twitch VOD address. Mirrors streamlink's own Twitch-plugin matcher so the two
# agree on what is a VOD: twitch.tv/videos/<id>, and the legacy .../<chan>/v/<id>.
_VOD_URL = re.compile(
    r"^(?:https?://)?(?:[\w-]+\.)?twitch\.tv/(?:[\w-]+/)?v(?:ideos?)?/(?P<id>\d+)"
    r"(?:[/?#].*)?$",
    re.IGNORECASE,
)
_VOD_ID = re.compile(r"^\d{1,20}$")


def parse_vod(text: str) -> tuple[str, str]:
    """Normalise a VOD reference to `(video_id, canonical_url)`, or raise.

    Accepts a full VOD URL (`https://www.twitch.tv/videos/123`, with or without a
    channel segment or query string) or a bare numeric video id. Everything the
    id becomes -- a lock name, a fallback directory component -- is digits only, so
    a rejected input never reaches the filesystem.
    """
    if not isinstance(text, str):
        raise InvalidVod("VOD reference must be text")
    value = text.strip()
    if not value:
        raise InvalidVod("a VOD URL or numeric id is required")
    if len(value) > 300:
        raise InvalidVod("VOD reference is too long")
    if any(ord(character) < 32 for character in value):
        raise InvalidVod("VOD reference contains control characters")

    if _VOD_ID.match(value):
        video_id = value
    else:
        match = _VOD_URL.match(value)
        if not match:
            raise InvalidVod(
                f"not a Twitch VOD address: {text!r} (expected "
                "https://www.twitch.tv/videos/<id> or a numeric id)")
        video_id = match.group("id")
    video_id = video_id.lstrip("0") or "0"
    return video_id, f"https://www.twitch.tv/videos/{video_id}"


def vod_dir_name(author: str, video_id: str) -> str:
    """A safe, channel-shaped directory name for a VOD.

    Prefer the broadcaster's own name so a VOD lands beside that channel's live
    recordings, but only when it normalises to a valid login; otherwise fall back
    to `vod_<id>`, which is always valid because the id is digits. The result is
    fed through the same `parse_channel` gate every other directory name is.
    """
    slug = re.sub(r"[^a-z0-9_]", "", str(author or "").strip().lower())
    if LOGIN.match(slug) and slug not in RESERVED:
        return slug
    return f"vod_{video_id}"


def parse_channel(text: str) -> str:
    """Normalise user input to a bare Twitch login, or raise.

    Accepts `name`, `@name`, `twitch.tv/name`, and full URLs with query strings.
    Rejects everything else rather than trying to sanitise it -- a name that needs
    sanitising is not a name the user meant to type.
    """
    if not isinstance(text, str):
        raise InvalidChannel("channel must be text")

    value = text.strip()
    if not value:
        raise InvalidChannel("channel name is required")
    if len(value) > 200:
        raise InvalidChannel("channel name is too long")
    if any(ord(character) < 32 for character in value):
        raise InvalidChannel("channel name contains control characters")

    # Anything with a path separator or a scheme is an address, and every address
    # goes through one parser. A second, substring-matching branch used to handle
    # the shorthand form and was wrong twice over: `Twitch.TV/SomeOne` matched
    # case-insensitively but then split on the lowercase literal, raising
    # IndexError, and `evil-twitch.tv/someone` also contains "twitch.tv/" and was
    # happily accepted as channel `someone`. urlparse settles the host question.
    if "/" in value or value.lower().startswith(("http:", "https:")):
        parsed = urlparse(value if "//" in value else f"https://{value}")
        # .hostname is already lowercased and strips any userinfo and port, so
        # `https://twitch.tv@evil.com/x` correctly reports evil.com.
        host = (parsed.hostname or "").lower()
        if host not in TWITCH_HOSTS:
            raise InvalidChannel(f"not a twitch.tv address: {text!r}")
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) != 1:
            raise InvalidChannel(
                "a channel URL must have exactly one path component")
        value = parts[0]

    value = value.lstrip("@").split("?")[0].strip().lower()

    if not LOGIN.match(value):
        raise InvalidChannel(
            f"{text!r} is not a valid Twitch channel name (letters, digits and "
            "underscore, 3-25 characters)")
    if value in RESERVED:
        raise InvalidChannel(f"{value!r} is a reserved device name on Windows")
    return value


def is_valid(text: str) -> bool:
    try:
        parse_channel(text)
    except InvalidChannel:
        return False
    return True
