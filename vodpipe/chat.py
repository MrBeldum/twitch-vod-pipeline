"""Twitch chat capture for live broadcasts and VOD archives.

Live chat is IRC over TLS (`irc.chat.twitch.tv:6697`), the same path a viewer
client uses, so messages land as they appear rather than after Twitch has
finished building a VOD. Past broadcasts use the undocumented GraphQL comments
API that TwitchDownloader uses (`VideoCommentsByOffsetOrCursor`); there is no
documented Helix equivalent that returns word-level chat with offsets.

Anonymous `justinfan` joins public chat. A configured OAuth token is used when
present so subscriber-only rooms are visible. Chat is never allowed to fail a
recording: a dropped IRC socket is retried, a VOD comments 404 is an artifact
error, and the master/transcript still complete.
"""

from __future__ import annotations

import json
import random
import socket
import ssl
import threading
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

from .net import ProxyError, open_tcp, wrap_tls
from .util import LOG, atomic_write_json, atomic_write_text, fmt_clock

# Twitch's web player Client-ID. The comments persisted-query is registered to
# it; a made-up id is refused. Same value TwitchDownloader ships.
TWITCH_CLIENT_ID = "kd1unb4b3q4t58fwlpcbzcbnm76a8fp"
GQL_URL = "https://gql.twitch.tv/gql"
COMMENTS_HASH = "b70a3591ff0f4e0313d126c6a1502d79a1c02baebb288227c582044aa76adf6a"
VIDEO_HASH = "300ab9310c6bb3c36b9f7a6b45ef0d9b87af26862f6fadf1d4c2b1294921f5c3"

IRC_HOST = "irc.chat.twitch.tv"
IRC_PORT = 6697

CHAT_JSON = "chat.json"
CHAT_TEXT = "chat.txt"
LIVE_JSONL = "live.jsonl"
VOD_JSON = "vod.json"

# How far a VOD comment may sit past the requested end and still be kept. The
# GraphQL page is not a precise trim.
COMMENT_END_SLACK = 1.0


class ChatError(RuntimeError):
    """Chat could not be fetched or parsed. Capture continues without it."""


@dataclass(frozen=True)
class ChatMessage:
    """One chat line, offset from the start of *our* media."""

    message_id: str
    offset: float
    created_at: float
    user: str
    user_id: str
    text: str
    emotes: tuple[str, ...] = ()
    bits: int = 0
    badges: tuple[str, ...] = ()
    kind: str = "privmsg"       # privmsg | usernotice | system
    source: str = "irc"         # irc | vod

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["emotes"] = list(self.emotes)
        data["badges"] = list(self.badges)
        data["t"] = fmt_clock(self.offset)
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ChatMessage":
        return cls(
            message_id=str(data.get("message_id") or data.get("id") or ""),
            offset=float(data.get("offset") or 0.0),
            created_at=float(data.get("created_at") or 0.0),
            user=str(data.get("user") or ""),
            user_id=str(data.get("user_id") or ""),
            text=str(data.get("text") or ""),
            emotes=tuple(str(item) for item in (data.get("emotes") or ())),
            bits=int(data.get("bits") or 0),
            badges=tuple(str(item) for item in (data.get("badges") or ())),
            kind=str(data.get("kind") or "privmsg"),
            source=str(data.get("source") or "irc"),
        )


# --------------------------------------------------------------------- IRC parse


def unescape_tag(value: str) -> str:
    # IRCv3 tag escapes. Order matters: `\\` must be last so a decoded backslash
    # is not then treated as the start of another escape.
    return (
        value.replace("\\:", ";")
             .replace("\\s", " ")
             .replace("\\r", "\r")
             .replace("\\n", "\n")
             .replace("\\\\", "\\")
    )


def parse_tags(raw: str) -> dict[str, str]:
    tags: dict[str, str] = {}
    if not raw:
        return tags
    if raw.startswith("@"):
        raw = raw[1:]
    for item in raw.split(";"):
        if not item:
            continue
        key, _, value = item.partition("=")
        tags[key] = unescape_tag(value)
    return tags


def _emotes_from_tag(emotes_tag: str, text: str) -> tuple[str, ...]:
    """Emote *names* from the `emotes` tag, which only carries id + ranges."""
    if not emotes_tag or not text:
        return ()
    names: list[str] = []
    seen: set[str] = set()
    for spec in emotes_tag.split("/"):
        _, _, ranges = spec.partition(":")
        if not ranges:
            continue
        first = ranges.split(",", 1)[0]
        start_s, _, end_s = first.partition("-")
        try:
            start = int(start_s)
            end = int(end_s) + 1
        except ValueError:
            continue
        if start < 0 or end > len(text) or end <= start:
            continue
        name = text[start:end]
        if name and name not in seen:
            seen.add(name)
            names.append(name)
    return tuple(names)


def _badges_from_tag(badges: str) -> tuple[str, ...]:
    if not badges:
        return ()
    return tuple(part.partition("/")[0] for part in badges.split(",") if part)


def parse_irc_line(line: str, *, origin: float, received_at: float | None = None,
                   source: str = "irc") -> ChatMessage | None:
    """A PRIVMSG or USERNOTICE, or None for pings, joins, and chatter.

    `origin` is the Unix time of t=0 of our recording. Offsets are computed
    from Twitch's `tmi-sent-ts` when it agrees with local receipt to within a
    few seconds -- that is the chat clock an editor wants -- and from local
    time otherwise, so a skewed PC clock cannot put chat minutes off the video.
    """
    line = line.rstrip("\r\n")
    if not line:
        return None
    tags: dict[str, str] = {}
    rest = line
    if rest.startswith("@"):
        tag_part, _, rest = rest.partition(" ")
        tags = parse_tags(tag_part)

    prefix = ""
    if rest.startswith(":"):
        prefix, _, rest = rest[1:].partition(" ")

    command, _, rest = rest.partition(" ")
    command = command.upper()
    if command == "PING":
        return None
    if command not in ("PRIVMSG", "USERNOTICE"):
        return None

    # Channel then optional trailing payload.
    channel_part, sep, trailing = rest.partition(" :")
    text = trailing if sep else ""
    if command == "USERNOTICE" and not text:
        text = tags.get("system-msg") or tags.get("msg-id") or ""

    user = (tags.get("display-name") or tags.get("login")
            or prefix.partition("!")[0]).strip()
    if not user:
        return None
    message_id = tags.get("id") or ""
    user_id = tags.get("user-id") or ""
    bits = 0
    try:
        bits = int(tags.get("bits") or 0)
    except ValueError:
        bits = 0

    received = received_at if received_at is not None else time.time()
    local_offset = received - origin
    tmi_raw = tags.get("tmi-sent-ts") or ""
    offset = local_offset
    created = received
    if tmi_raw.isdigit():
        tmi = int(tmi_raw) / 1000.0
        created = tmi
        tmi_offset = tmi - origin
        if abs(tmi_offset - local_offset) <= 15.0:
            offset = tmi_offset
    if offset < 0:
        offset = 0.0

    kind = "usernotice" if command == "USERNOTICE" else "privmsg"
    if command == "USERNOTICE" and tags.get("msg-id") == "raid":
        kind = "system"
    return ChatMessage(
        message_id=message_id or f"{user}:{offset:.3f}:{text[:40]}",
        offset=offset,
        created_at=created,
        user=user,
        user_id=user_id,
        text=text,
        emotes=_emotes_from_tag(tags.get("emotes") or "", text),
        bits=bits,
        badges=_badges_from_tag(tags.get("badges") or ""),
        kind=kind,
        source=source,
    )


# ---------------------------------------------------------------- live capture


def _anonymous_nick() -> str:
    return f"justinfan{random.randint(10000, 999999)}"


def _oauth_login(token: str, proxy: str, timeout: float) -> str:
    """The login the token belongs to, or empty if it cannot be asked."""
    token = token.strip()
    if token.lower().startswith("oauth:"):
        token = token[6:]
    if not token:
        return ""
    request = urllib.request.Request(
        "https://id.twitch.tv/oauth2/validate",
        headers={"Authorization": f"OAuth {token}"},
        method="GET",
    )
    try:
        with _urlopen(request, proxy=proxy, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8", "replace"))
    except Exception as exc:
        LOG.warning("could not validate Twitch OAuth token for chat: %s", exc)
        return ""
    login = str(payload.get("login") or "").strip()
    return login.lower() if login else ""


class LiveChatCapture:
    """Background IRC listener that appends JSONL and keeps a memory buffer.

    The buffer is what chunk slicing reads; the JSONL is what recovery reads
    after a crash. Writes are line-atomic (one message, one newline) so a
    torn last line can be dropped without losing the rest.
    """

    def __init__(
        self,
        channel: str,
        destination: Path,
        *,
        origin: float,
        oauth_token: str = "",
        proxy: str = "",
        login: str = "",
    ) -> None:
        self.channel = channel.lstrip("#").lower()
        self.destination = destination
        self.origin = origin
        self.oauth_token = (oauth_token or "").strip()
        self.proxy = (proxy or "").strip()
        self.login = (login or "").strip().lower()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._guard = threading.Lock()
        self._messages: list[ChatMessage] = []
        self._ids: set[str] = set()
        self._sock: ssl.SSLSocket | socket.socket | None = None

    def start(self) -> None:
        self.destination.parent.mkdir(parents=True, exist_ok=True)
        self._load_existing()
        self._thread = threading.Thread(
            target=self._run, name=f"chat-{self.channel}", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 10.0) -> None:
        self._stop.set()
        sock = self._sock
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)

    def snapshot(self) -> list[ChatMessage]:
        with self._guard:
            return list(self._messages)

    def in_range(self, start: float, end: float) -> list[ChatMessage]:
        return slice_messages(self.snapshot(), start, end)

    def _load_existing(self) -> None:
        if not self.destination.is_file():
            return
        loaded: list[ChatMessage] = []
        ids: set[str] = set()
        try:
            with self.destination.open("r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        message = ChatMessage.from_dict(json.loads(line))
                    except (ValueError, TypeError, json.JSONDecodeError):
                        continue
                    if message.message_id in ids:
                        continue
                    ids.add(message.message_id)
                    loaded.append(message)
        except OSError as exc:
            LOG.warning("could not reload %s: %s", self.destination, exc)
            return
        loaded.sort(key=lambda item: (item.offset, item.created_at))
        with self._guard:
            self._messages = loaded
            self._ids = ids

    def _run(self) -> None:
        delay = 1.0
        while not self._stop.is_set():
            try:
                self._session()
                delay = 1.0
            except Exception as exc:
                if self._stop.is_set():
                    return
                LOG.warning("%s chat: %s; reconnecting in %.0fs",
                            self.channel, exc, delay)
                self._stop.wait(delay)
                delay = min(60.0, delay * 2.0)

    def _session(self) -> None:
        nick = self.login or _anonymous_nick()
        token = self.oauth_token
        if token.lower().startswith("oauth:"):
            token = token[6:]
        sock = open_tcp(IRC_HOST, IRC_PORT, self.proxy, timeout=20.0)
        try:
            tls = wrap_tls(sock, IRC_HOST, timeout=20.0)
        except Exception:
            sock.close()
            raise
        self._sock = tls
        try:
            if token:
                _irc_send(tls, f"PASS oauth:{token}")
            else:
                _irc_send(tls, "PASS SCHMOOPIIE")
            _irc_send(tls, f"NICK {nick}")
            _irc_send(tls, "CAP REQ :twitch.tv/tags twitch.tv/commands")
            _irc_send(tls, f"JOIN #{self.channel}")
            LOG.info("%s: joined Twitch chat as %s", self.channel, nick)
            tls.settimeout(30.0)
            buffer = b""
            while not self._stop.is_set():
                try:
                    chunk = tls.recv(4096)
                except TimeoutError:
                    continue
                except (OSError, ssl.SSLError):
                    if self._stop.is_set():
                        return
                    raise
                if not chunk:
                    raise ChatError("IRC connection closed")
                buffer += chunk
                while b"\n" in buffer:
                    raw, buffer = buffer.split(b"\n", 1)
                    line = raw.decode("utf-8", "replace").rstrip("\r")
                    if _is_ping(line):
                        _irc_send(tls, f"PONG :{_ping_payload(line)}")
                        continue
                    message = parse_irc_line(
                        line, origin=self.origin, received_at=time.time())
                    if message is not None:
                        self._accept(message)
        finally:
            self._sock = None
            try:
                tls.close()
            except OSError:
                pass

    def _accept(self, message: ChatMessage) -> None:
        with self._guard:
            if message.message_id in self._ids:
                return
            self._ids.add(message.message_id)
            self._messages.append(message)
        try:
            with self.destination.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(message.to_dict(), ensure_ascii=False) + "\n")
        except OSError as exc:
            LOG.warning("could not append chat to %s: %s", self.destination, exc)


def _irc_send(sock: ssl.SSLSocket, line: str) -> None:
    sock.sendall((line + "\r\n").encode("utf-8"))


def _is_ping(line: str) -> bool:
    payload = line
    if payload.startswith("@"):
        _, _, payload = payload.partition(" ")
    if payload.startswith(":"):
        _, _, payload = payload.partition(" ")
    return payload.upper().startswith("PING")


def _ping_payload(line: str) -> str:
    if " :" in line:
        return line.split(" :", 1)[1] or "tmi.twitch.tv"
    return "tmi.twitch.tv"


# -------------------------------------------------------------- VOD comments


def download_vod_chat(
    video_id: str,
    *,
    start: float = 0.0,
    end: float | None = None,
    proxy: str = "",
    oauth_token: str = "",
    threads: int = 4,
    timeout: float = 300.0,
) -> list[ChatMessage]:
    """Every comment in `[start, end)` on a VOD, session-offset by `start`.

    `start`/`end` are VOD-relative seconds (the same numbers streamlink's
    `--hls-start-offset` / `--hls-duration` use). Stored offsets are shifted
    so they line up with our chunked media, whose t=0 is that start.
    """
    video_id = str(video_id).lstrip("0") or "0"
    start = max(0.0, float(start or 0.0))
    deadline = time.monotonic() + max(5.0, timeout)
    info = _gql_video_info(video_id, proxy=proxy, oauth_token=oauth_token,
                           deadline=deadline)
    length = float(info.get("length") or 0.0)
    created_at = float(info.get("created_at") or 0.0)
    # `end` is an exclusive VOD-relative timestamp, not a duration. The
    # pipeline helper `download_vod_chat_range` is what adds `vod_start +
    # vod_duration` before calling here.
    if end is None:
        video_end = length if length > 0 else start + 86400.0
    else:
        video_end = max(start, float(end))
        if length > 0:
            video_end = min(video_end, length)
    comments = _download_comment_range(
        video_id, int(start), int(video_end) + 1,
        created_at=created_at, proxy=proxy, oauth_token=oauth_token,
        threads=threads, deadline=deadline,
        run_to_end=end is None,
    )
    shifted: list[ChatMessage] = []
    seen: set[str] = set()
    for comment in comments:
        if comment.message_id in seen:
            continue
        if comment.offset < start - 0.05:
            continue
        if comment.offset >= video_end + COMMENT_END_SLACK:
            continue
        seen.add(comment.message_id)
        shifted.append(ChatMessage(
            message_id=comment.message_id,
            offset=max(0.0, comment.offset - start),
            created_at=comment.created_at,
            user=comment.user,
            user_id=comment.user_id,
            text=comment.text,
            emotes=comment.emotes,
            bits=comment.bits,
            badges=comment.badges,
            kind=comment.kind,
            source="vod",
        ))
    shifted.sort(key=lambda item: (item.offset, item.created_at, item.message_id))
    return shifted


def download_vod_chat_range(
    video_id: str,
    *,
    vod_start: float | None,
    vod_duration: float | None,
    proxy: str = "",
    oauth_token: str = "",
    threads: int = 4,
    timeout: float = 300.0,
) -> list[ChatMessage]:
    start = float(vod_start or 0.0)
    end = None if vod_duration is None else start + float(vod_duration)
    return download_vod_chat(
        video_id, start=start, end=end, proxy=proxy, oauth_token=oauth_token,
        threads=threads, timeout=timeout,
    )


def _gql_video_info(video_id: str, *, proxy: str, oauth_token: str,
                    deadline: float) -> dict[str, Any]:
    payload = {
        "operationName": "VideoMetadata",
        "variables": {"videoID": video_id, "channelLogin": ""},
        "extensions": {
            "persistedQuery": {"version": 1, "sha256Hash": VIDEO_HASH},
        },
    }
    try:
        data = _gql(payload, proxy=proxy, oauth_token=oauth_token, deadline=deadline)
        video = ((data.get("data") or {}).get("video") or {})
    except ChatError:
        return {}
    created = video.get("createdAt") or video.get("publishedAt") or ""
    created_at = _parse_iso(created)
    try:
        length = float(video.get("lengthSeconds") or 0.0)
    except (TypeError, ValueError):
        length = 0.0
    return {"created_at": created_at, "length": length,
            "title": str(video.get("title") or "")}


def _download_comment_range(
    video_id: str, start: int, end: int, *,
    created_at: float, proxy: str, oauth_token: str,
    threads: int, deadline: float, run_to_end: bool,
) -> list[ChatMessage]:
    duration = max(1, end - start)
    workers = max(1, min(int(threads), duration))
    if workers == 1:
        return _download_section(
            video_id, start, end, created_at=created_at, run_to_end=run_to_end,
            proxy=proxy, oauth_token=oauth_token, deadline=deadline)

    chunk = int((duration + workers - 1) / workers)
    ranges: list[tuple[int, int, bool]] = []
    cursor = start
    while cursor < end:
        stop = min(end, cursor + chunk)
        tail = bool(run_to_end and stop >= end)
        ranges.append((cursor, stop, tail))
        cursor = stop

    collected: list[ChatMessage] = []
    errors: list[BaseException] = []
    guard = threading.Lock()

    def work(lo: int, hi: int, tail: bool) -> None:
        try:
            part = _download_section(
                video_id, lo, hi, created_at=created_at, run_to_end=tail,
                proxy=proxy, oauth_token=oauth_token, deadline=deadline)
        except BaseException as exc:
            with guard:
                errors.append(exc)
            return
        with guard:
            collected.extend(part)

    jobs = [threading.Thread(target=work, args=(lo, hi, tail), daemon=True)
            for lo, hi, tail in ranges]
    for job in jobs:
        job.start()
    for job in jobs:
        remaining = deadline - time.monotonic()
        job.join(timeout=max(0.1, remaining))
    if errors:
        raise ChatError(str(errors[0])) from errors[0]
    return collected


def _download_section(
    video_id: str, start: int, end: int, *,
    created_at: float, run_to_end: bool,
    proxy: str, oauth_token: str, deadline: float,
) -> list[ChatMessage]:
    comments: list[ChatMessage] = []
    cursor = ""
    first = True
    latest = float(start) - 1.0
    errors = 0
    nulls = 0
    while run_to_end or latest < end:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ChatError("VOD chat download timed out")
        variables: dict[str, Any] = {"videoID": video_id}
        if first:
            variables["contentOffsetSeconds"] = int(start)
        else:
            variables["cursor"] = cursor
        payload = {
            "operationName": "VideoCommentsByOffsetOrCursor",
            "variables": variables,
            "extensions": {
                "persistedQuery": {"version": 1, "sha256Hash": COMMENTS_HASH},
            },
        }
        try:
            data = _gql(payload, proxy=proxy, oauth_token=oauth_token,
                        deadline=deadline)
        except ChatError as exc:
            errors += 1
            if errors > 10:
                raise
            LOG.warning("VOD chat page failed at %ss (%s); retrying", latest, exc)
            time.sleep(min(10.0, errors))
            continue
        video = (data.get("data") or {}).get("video") or {}
        page = (video.get("comments") or {})
        edges = page.get("edges")
        if edges is None:
            nulls += 1
            if nulls > 10:
                raise ChatError("Twitch returned too many empty comment pages")
            time.sleep(min(2.0, 0.1 * nulls))
            continue
        errors = max(0, errors - 1)
        nulls = max(0, nulls - 1)
        converted = _comments_from_edges(edges, created_at=created_at)
        if not converted:
            if not (page.get("pageInfo") or {}).get("hasNextPage"):
                break
        for comment in converted:
            if start - 0.05 <= comment.offset < end + COMMENT_END_SLACK or (
                    run_to_end and comment.offset >= start - 0.05):
                comments.append(comment)
            if comment.offset > latest:
                latest = comment.offset
        if not (page.get("pageInfo") or {}).get("hasNextPage"):
            break
        if edges:
            cursor = str((edges[-1] or {}).get("cursor") or "")
        if not cursor:
            break
        first = False
        if not run_to_end and latest >= end:
            break
    return comments


def _comments_from_edges(edges: Iterable[Any], *,
                         created_at: float) -> list[ChatMessage]:
    messages: list[ChatMessage] = []
    for edge in edges:
        if not isinstance(edge, dict):
            continue
        node = edge.get("node") or {}
        commenter = node.get("commenter") or {}
        if not commenter:
            continue
        message = node.get("message") or {}
        fragments = message.get("fragments") or []
        parts: list[str] = []
        emotes: list[str] = []
        for fragment in fragments:
            if not isinstance(fragment, dict):
                continue
            text = fragment.get("text")
            if not isinstance(text, str) or not text:
                continue
            parts.append(text)
            emote = fragment.get("emote") or {}
            if emote.get("emoteID") and text not in emotes:
                emotes.append(text)
        body = "".join(parts) or str(message.get("body") or "")
        try:
            offset = float(node.get("contentOffsetSeconds") or 0.0)
        except (TypeError, ValueError):
            continue
        created = _parse_iso(node.get("createdAt") or "")
        badges = []
        for badge in message.get("userBadges") or []:
            if not isinstance(badge, dict):
                continue
            set_id = str(badge.get("setID") or "")
            if set_id:
                badges.append(set_id)
        bits = 0
        # Cheer messages put the amount in the body (`Cheer100`); the GQL
        # payload does not always include a dedicated bits field.
        for token in body.split():
            if token[:5].lower() == "cheer" and token[5:].isdigit():
                bits = int(token[5:])
                break
        user = str(commenter.get("displayName") or commenter.get("login") or "").strip()
        messages.append(ChatMessage(
            message_id=str(node.get("id") or ""),
            offset=offset,
            created_at=created,
            user=user,
            user_id=str(commenter.get("id") or ""),
            text=body,
            emotes=tuple(emotes),
            bits=bits,
            badges=tuple(badges),
            kind="privmsg",
            source="vod",
        ))
    if created_at and messages:
        _adjust_old_vod_offsets(messages, created_at)
    return messages


def _adjust_old_vod_offsets(messages: list[ChatMessage],
                            video_created_at: float) -> None:
    """Some old VODs report comment offsets an hour off. TwitchDownloader's fix.

    If the first comment's content offset disagrees with createdAt - vodCreated
    by more than five seconds, shift every offset by that delta. The messages
    are frozen dataclasses, so this replaces them in the list.
    """
    first = messages[0]
    if first.created_at <= 0 or video_created_at <= 0:
        return
    estimated = first.offset - (first.created_at - video_created_at)
    if abs(estimated) < 5.0:
        return
    LOG.info("VOD comments look offset by %.0fs; adjusting", estimated)
    for index, comment in enumerate(messages):
        messages[index] = ChatMessage(
            message_id=comment.message_id,
            offset=comment.offset - estimated,
            created_at=comment.created_at,
            user=comment.user,
            user_id=comment.user_id,
            text=comment.text,
            emotes=comment.emotes,
            bits=comment.bits,
            badges=comment.badges,
            kind=comment.kind,
            source=comment.source,
        )


def _parse_iso(value: str) -> float:
    if not value or not isinstance(value, str):
        return 0.0
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        from datetime import datetime
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return 0.0


def _gql(payload: dict[str, Any], *, proxy: str, oauth_token: str,
         deadline: float) -> dict[str, Any]:
    body = json.dumps(payload).encode("utf-8")
    headers = {
        "Client-ID": TWITCH_CLIENT_ID,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    token = (oauth_token or "").strip()
    if token.lower().startswith("oauth:"):
        token = token[6:]
    if token:
        headers["Authorization"] = f"OAuth {token}"
    request = urllib.request.Request(GQL_URL, data=body, headers=headers, method="POST")
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ChatError("VOD chat download timed out")
    try:
        with _urlopen(request, proxy=proxy, timeout=min(60.0, remaining)) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        detail = b""
        try:
            detail = exc.read()[:400]
        finally:
            exc.close()
        raise ChatError(f"Twitch GQL HTTP {exc.code}: {detail.decode('utf-8', 'replace')}") from exc
    except (urllib.error.URLError, TimeoutError, ProxyError, OSError) as exc:
        raise ChatError(f"Twitch GQL failed: {exc}") from exc
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ChatError(f"Twitch GQL returned unparsable JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ChatError("Twitch GQL returned a non-object")
    if parsed.get("errors"):
        raise ChatError(f"Twitch GQL error: {parsed['errors']!r}"[:400])
    return parsed


def _urlopen(request: urllib.request.Request, *, proxy: str, timeout: float):
    proxy = (proxy or "").strip()
    if not proxy:
        return urllib.request.urlopen(request, timeout=timeout)
    scheme = (urlparse(proxy).scheme or "").lower()
    if scheme in ("http", "https"):
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
        return opener.open(request, timeout=timeout)
    # SOCKS: urllib has no handler in the stdlib. Tunnel and speak HTTP/1.1.
    return _socks_urlopen(request, proxy=proxy, timeout=timeout)


class _SockHTTPResponse:
    def __init__(self, sock, body: bytes, status: int) -> None:
        self._sock = sock
        self._body = body
        self.status = status

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "_SockHTTPResponse":
        return self

    def __exit__(self, *args: object) -> None:
        try:
            self._sock.close()
        except OSError:
            pass


def _socks_urlopen(request: urllib.request.Request, *, proxy: str,
                   timeout: float) -> _SockHTTPResponse:
    parsed = urlparse(request.full_url)
    host = parsed.hostname or ""
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"
    sock = open_tcp(host, port, proxy, timeout=timeout)
    if parsed.scheme == "https":
        sock = wrap_tls(sock, host, timeout=timeout)
    headers = request.header_items()
    payload = request.data or b""
    lines = [f"{request.get_method()} {path} HTTP/1.1", f"Host: {host}"]
    for key, value in headers:
        if key.lower() == "host":
            continue
        lines.append(f"{key}: {value}")
    lines.append(f"Content-Length: {len(payload)}")
    lines.append("Connection: close")
    lines.append("")
    sock.sendall("\r\n".join(lines).encode("ascii") + b"\r\n" + payload)
    buf = bytearray()
    sock.settimeout(timeout)
    while True:
        chunk = sock.recv(65536)
        if not chunk:
            break
        buf.extend(chunk)
    head, _, rest = bytes(buf).partition(b"\r\n\r\n")
    status_line = head.split(b"\r\n", 1)[0].decode("ascii", "replace")
    try:
        status = int(status_line.split(" ", 2)[1])
    except (IndexError, ValueError):
        status = 0
    # Chunked bodies are rare on this endpoint; handle the simple form.
    lowered = head.lower()
    if b"transfer-encoding: chunked" in lowered:
        rest = _unchunk(rest)
    if status >= 400:
        try:
            sock.close()
        except OSError:
            pass
        raise ChatError(f"Twitch GQL HTTP {status}: {rest[:400]!r}")
    return _SockHTTPResponse(sock, rest, status)


def _unchunk(body: bytes) -> bytes:
    out = bytearray()
    view = body
    while view:
        line, _, rest = view.partition(b"\r\n")
        try:
            size = int(line.split(b";", 1)[0], 16)
        except ValueError:
            return body
        if size == 0:
            break
        out.extend(rest[:size])
        view = rest[size:]
        if view.startswith(b"\r\n"):
            view = view[2:]
    return bytes(out)


# ---------------------------------------------------------------- persistence


def slice_messages(messages: Iterable[ChatMessage], start: float,
                   end: float) -> list[ChatMessage]:
    """Messages whose offset sits in `[start, end)`, sorted."""
    selected = [item for item in messages
                if start - 1e-6 <= item.offset < end + 1e-6]
    selected.sort(key=lambda item: (item.offset, item.created_at, item.message_id))
    return selected


def load_messages(path: Path) -> list[ChatMessage]:
    if not path.is_file():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ChatError(f"could not read {path.name}: {exc}") from exc
    if isinstance(raw, dict):
        items = raw.get("messages") or []
    elif isinstance(raw, list):
        items = raw
    else:
        raise ChatError(f"{path.name} is not a chat document")
    messages = []
    for item in items:
        if isinstance(item, dict):
            try:
                messages.append(ChatMessage.from_dict(item))
            except (TypeError, ValueError):
                continue
    return messages


def load_jsonl(path: Path) -> list[ChatMessage]:
    if not path.is_file():
        return []
    messages: list[ChatMessage] = []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    messages.append(ChatMessage.from_dict(json.loads(line)))
                except (ValueError, TypeError, json.JSONDecodeError):
                    continue
    except OSError as exc:
        raise ChatError(f"could not read {path.name}: {exc}") from exc
    return messages


def write_chat_exports(directory: Path, messages: list[ChatMessage], *,
                       channel: str, session_id: str, chunk: str,
                       session_offset: float, duration: float,
                       source: str) -> None:
    """`chat.json` + `chat.txt` in `directory` (the chunk's `source/`)."""
    directory.mkdir(parents=True, exist_ok=True)
    document = {
        "version": 1,
        "channel": channel,
        "session_id": session_id,
        "chunk": chunk,
        "session_offset": session_offset,
        "duration": duration,
        "source": source,
        "count": len(messages),
        "messages": [item.to_dict() for item in messages],
    }
    atomic_write_json(directory / CHAT_JSON, document)
    lines = [
        f"# {channel} {chunk} chat — {len(messages)} messages",
        f"# {fmt_clock(session_offset)}-{fmt_clock(session_offset + duration)} of the broadcast",
        "",
    ]
    for item in messages:
        prefix = fmt_clock(item.offset + session_offset)
        kind = "" if item.kind == "privmsg" else f" [{item.kind}]"
        bits = f" ({item.bits} bits)" if item.bits else ""
        lines.append(f"[{prefix}] {item.user}{kind}{bits}: {item.text}")
    atomic_write_text(directory / CHAT_TEXT, "\n".join(lines) + "\n")


def session_chat_dir(session_directory: Path) -> Path:
    return session_directory / "chat"


def live_jsonl_path(session_directory: Path) -> Path:
    return session_chat_dir(session_directory) / LIVE_JSONL


def vod_json_path(session_directory: Path) -> Path:
    return session_chat_dir(session_directory) / VOD_JSON
