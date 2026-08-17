"""Command line entry point.

`dashboard` is the intended day-to-day surface; the other subcommands exist so the
pieces can be driven or debugged without a browser.
"""

from __future__ import annotations

import argparse
import math

import sys
import time
from pathlib import Path
from urllib.parse import urlparse

from .channels import parse_channel
from .config import CONFIG_PATH, Config
from .pipeline import Pipeline
from .server import serve
from .snapshot import SnapshotRequest
from .util import (
    LOG,
    atomic_write_text,
    find_tool,
    free_bytes,
    human_bytes,
    resolve_tools,
    run,
    safe_name_component,
    setup_logging,
)


# Every subcommand the parser knows. Used to decide whether an argv already
# names one before defaulting to the dashboard; keep in step with build_parser().
SUBCOMMANDS = frozenset({
    "dashboard", "record", "vod", "snapshot", "transcribe", "doctor", "sessions",
    "republish",
})


def _positive(text: str) -> float:
    """A finite duration greater than zero.

    AUD2-056: these were plain `float`, so `--minutes 0` recorded indefinitely
    (the deadline is built with a truthiness test), `--minutes -5` stopped
    immediately, and `nan`/`inf` defeated the comparison entirely -- `nan` fails
    every `>=` so the deadline never arrives.
    """
    value = float(text)
    if not math.isfinite(value):
        raise argparse.ArgumentTypeError(f"{text!r} is not a finite number")
    if value <= 0:
        raise argparse.ArgumentTypeError(f"{text!r} must be greater than zero")
    return value


def _non_negative(text: str) -> float:
    """A finite offset at or after zero."""
    value = float(text)
    if not math.isfinite(value):
        raise argparse.ArgumentTypeError(f"{text!r} is not a finite number")
    if value < 0:
        raise argparse.ArgumentTypeError(f"{text!r} must not be negative")
    return value


def _time_spec(text: str) -> float:
    """Seconds from a plain number or an `HH:MM:SS` / `MM:SS` clock string."""
    cleaned = text.strip()
    if not cleaned:
        raise argparse.ArgumentTypeError("a time is required")
    try:
        parts = [float(part) for part in cleaned.split(":")]
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"{text!r} is not a number or an HH:MM:SS clock")
    if len(parts) > 3 or any(not math.isfinite(part) or part < 0 for part in parts):
        raise argparse.ArgumentTypeError(
            f"{text!r} is not a valid time")
    seconds = 0.0
    for part in parts:
        seconds = seconds * 60.0 + part
    return seconds


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vodpipe",
        description="Record Twitch streams into Premiere-ready masters, proxies, "
                    "transcripts and rundowns.",
    )
    parser.add_argument("--config", type=Path, default=CONFIG_PATH,
                        help="path to config.json")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command")

    dashboard = sub.add_parser("dashboard", help="run the local web dashboard")
    dashboard.add_argument("--port", type=int)
    dashboard.add_argument("--no-browser", action="store_true")

    record = sub.add_parser("record", help="record one channel, no dashboard")
    record.add_argument("channel")
    record.add_argument("--minutes", type=_positive,
                        help="stop after this long (default: until the stream ends)")
    record.add_argument("--now", action="store_true",
                        help="start immediately instead of waiting for the "
                             "channel to go live")

    vod = sub.add_parser(
        "vod", help="download a Twitch VOD through the full pipeline")
    vod.add_argument("url", help="VOD URL (twitch.tv/videos/<id>) or numeric id")
    vod.add_argument("--start", type=_time_spec, metavar="TIME",
                     help="download from this offset (seconds or HH:MM:SS)")
    vod.add_argument("--duration", type=_time_spec, metavar="TIME",
                     help="download only this much (seconds or HH:MM:SS)")

    snapshot = sub.add_parser("snapshot", help="cut a range out of a session")
    snapshot.add_argument("session_id")
    snapshot.add_argument("--last", type=_positive, metavar="MINUTES")
    snapshot.add_argument("--start", type=_non_negative, metavar="SECONDS")
    snapshot.add_argument("--end", type=_positive, metavar="SECONDS")
    snapshot.add_argument("--precise", action="store_true",
                          help="frame-exact start; re-encodes, so slower")
    snapshot.add_argument("--no-transcript", action="store_true")

    transcribe = sub.add_parser("transcribe", help="transcribe any media file")
    transcribe.add_argument("path", type=Path)
    transcribe.add_argument("--out", type=Path,
                            help="output directory (default: alongside the file)")

    republish = sub.add_parser(
        "republish",
        help="rebuild transcript exports from stored words, without re-transcribing")
    republish.add_argument("session_id", nargs="?",
                           help="one session; omit to rebuild every session")

    sub.add_parser("doctor", help="check the environment")
    sub.add_parser("sessions", help="list known sessions")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    if argv is None:
        argv = sys.argv[1:]
    # `python -m vodpipe` with no subcommand means the dashboard. Normalising here
    # rather than defaulting later means args always carries the dashboard flags;
    # otherwise cmd_dashboard() dies on a missing `args.port`.
    #
    # Testing "is there any non-flag argument" was not enough: `vodpipe --config
    # PATH` has one -- PATH -- so nothing was appended, `args.command` stayed
    # None, and the dashboard fell over on the missing flags. Look for an actual
    # subcommand instead.
    if not _has_subcommand(argv):
        argv = list(argv) + ["dashboard"]
    args = parser.parse_args(argv)
    setup_logging(verbose=args.verbose)

    config = Config.load(args.config)
    command = args.command or "dashboard"

    try:
        if command == "doctor":
            return cmd_doctor(config)
        if command == "dashboard":
            return cmd_dashboard(config, args)
        if command == "record":
            return cmd_record(config, args)
        if command == "vod":
            return cmd_vod(config, args)
        if command == "snapshot":
            return cmd_snapshot(config, args)
        if command == "republish":
            return cmd_republish(config, args)
        if command == "transcribe":
            return cmd_transcribe(config, args)
        if command == "sessions":
            return cmd_sessions(config)
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130
    except Exception as exc:
        LOG.error("%s", exc)
        return 1

    parser.print_help()
    return 2


def _has_subcommand(argv: list[str]) -> bool:
    """Find a command token without mistaking a global option value for one."""
    index = 0
    while index < len(argv):
        argument = argv[index]
        if argument == "--config":
            index += 2
            continue
        if argument.startswith("--config=") or argument in ("-v", "--verbose"):
            index += 1
            continue
        return argument in SUBCOMMANDS
    return False


# --------------------------------------------------------------------- commands


def cmd_doctor(config: Config) -> int:
    """Report on the environment, judged against what is actually switched on.

    Feature-aware on purpose. It used to demand a Deepgram key even with
    transcription disabled, and stayed silent about a summary provider that was
    enabled but unconfigured -- so it failed for a working setup and passed for a
    broken one.
    """
    ok = True
    transcribing = bool(config.get("transcription.enabled", True))
    provider = (config.get("summary.provider") or "claude-cli").lower()
    summarising = bool(config.get("summary.enabled", True)) and provider != "none"

    print("Tools")
    claude_needed = summarising and provider == "claude-cli"
    for name in ("ffmpeg", "ffprobe", "streamlink", "claude"):
        override = (config.get(f"tools.{name}") or "").strip()
        path = find_tool(name, override or None)
        required = name != "claude" or claude_needed
        mark = "ok " if path else ("MISSING" if required else "absent")
        if not path and required:
            ok = False
        note = ""
        if name == "claude" and not path and claude_needed:
            note = "  <- summary.provider is claude-cli"
        print(f"  {name:<12} {mark:<8} {path or ''}{note}")

    print("\nSecrets")
    for key, note, needed in (
        ("deepgram_api_key", "required for transcription", transcribing),
        ("twitch_oauth_token", "optional, enables the ad-free path", False),
        ("anthropic_api_key", "only for the anthropic-api summariser",
         summarising and provider == "anthropic-api"),
    ):
        present = bool(config.secret(key))
        state = "set" if present else ("MISSING" if needed else "not set")
        print(f"  {key:<20} {state:<8} ({note})")
        if needed and not present:
            ok = False

    print("\nFeatures")
    print(f"  transcription {'on' if transcribing else 'off'}")
    print(f"  rundowns     {provider if summarising else 'off'}")
    print(f"  proxies      {'on' if config.get('proxies.enabled', True) else 'off'}")

    print("\nCapture quality")
    floor = int(config.get("recording.min_height", 0) or 0)
    print(f"  requested    {config.get('recording.quality', 'best')}")
    print(f"  floor        {str(floor) + 'p' if floor else 'no minimum'}"
          f"  ({config.get('recording.on_low_quality', 'warn')} if below)")
    # Measured 2026-08-14: from a Korean IP Twitch withholds the source rendition
    # on every channel, leaving only transcodes -- 720p60 on many large channels.
    # A VPN to another region restores it. This is worth saying unconditionally
    # because the symptom is a silently mediocre master, not an error.
    print("  note         a Korean IP is served transcode-only ladders (no source")
    print("               rendition); a VPN or the network proxy below restores")
    print("               source quality. See README, 'Recording from South Korea'.")

    print("\nNetwork")
    proxy = str(config.get("network.proxy", "") or "").strip()
    if proxy:
        parsed = urlparse(proxy)
        host = parsed.hostname or "?"
        port = f":{parsed.port}" if parsed.port else ""
        print(f"  proxy        {parsed.scheme}://{host}{port}  "
              "(streamlink routes live capture, VOD download and probes through it)")
    else:
        print("  proxy        none  (set network.proxy to reach Twitch from a "
              "region it has left, e.g. South Korea)")

    print("\nStorage")
    root = config.masters_root
    free = free_bytes(root)
    floor = float(config.get("recording.free_space_floor_gb", 50)) * 1024 ** 3
    print(f"  masters      {root}")
    print(f"  free         {human_bytes(free)} (floor {human_bytes(floor)})")
    if free < floor:
        print("  WARNING: below the floor -- new chunks will be refused")
        ok = False

    censor = Path(config.get("paths.censor_master_list", ""))
    print(f"  censor list  {censor} {'ok' if censor.exists() else 'MISSING'}")

    print("\nEncoder")
    try:
        from .media import probe_encoder
        tools = resolve_tools(
            {k: v for k, v in (config.get("tools") or {}).items() if v},
            need=("ffmpeg", "ffprobe"))
        print(f"  proxy        {probe_encoder(tools, config.get('proxies.encoder', 'auto'))}")
    except Exception as exc:
        print(f"  proxy        could not probe: {exc}")

    print("\n" + ("Ready." if ok else "Not ready -- see the items above."))
    return 0 if ok else 1


def cmd_dashboard(config: Config, args) -> int:
    """AUD2-047: the pipeline is owned from construction, not from `serve()`.

    The `try` used to start only after `serve()` returned, so a failed bind --
    the port already in use, the commonest failure there is -- skipped
    `shutdown()` entirely, abandoning recovery work the pipeline had already
    queued and leaving its workers running until the interpreter exited.
    """
    pipeline = Pipeline(config)
    httpd = None
    try:
        pipeline.start()
        # Flags override for this run only; not written back to config.json.
        httpd = serve(pipeline, config,
                      port=args.port,
                      open_browser=False if args.no_browser else None)

        # No SIGINT handler. `BaseServer.shutdown()` blocks until
        # `serve_forever()` returns, so calling it from a handler running on this
        # very thread deadlocks -- Ctrl+C would hang instead of quitting. Letting
        # the default handler raise KeyboardInterrupt out of serve_forever() gets
        # us to the `finally` below through ordinary control flow.
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping...", file=sys.stderr)
    finally:
        try:
            if httpd is not None:
                httpd.server_close()
        finally:
            try:
                LOG.info("finishing queued work before exit...")
            finally:
                pipeline.shutdown_until_stopped()
    return 0


def cmd_record(config: Config, args) -> int:
    # AUD2-048: parse once, here, and use the canonical form for every
    # subsequent lookup. `request_recording()` normalises internally, so
    # `vodpipe record https://twitch.tv/SomeOne` armed `someone` while the waiter
    # and the timed stop below asked about the raw URL -- the waiter reported the
    # request cancelled immediately, and the timed stop could not find the
    # recorder it was meant to stop.
    channel = parse_channel(args.channel)
    pipeline = Pipeline(config)
    session = None
    try:
        pipeline.start()

        # Waits for the channel if it is offline rather than launching a
        # streamlink that retries forever against nothing. --now opts out.
        if args.now:
            session = pipeline.start_recording(channel)
        else:
            outcome = pipeline.request_recording(channel)
            if outcome["state"] == "armed":
                print(f"{channel} is not live -- waiting. Ctrl+C to give up.")
            session = _wait_for_session(
                pipeline, outcome["request_id"], channel)
            if session is None:
                return 130

        print(f"recording {channel} -> {session.directory}")
        print("press Ctrl+C to stop")

        deadline = time.time() + args.minutes * 60 if args.minutes else None
        try:
            while session.status in ("starting", "recording"):
                if deadline and time.time() >= deadline:
                    pipeline.stop_recording(channel)
                    break
                time.sleep(2)
        except KeyboardInterrupt:
            print("\nstopping...", file=sys.stderr)
            try:
                pipeline.stop_recording(channel)
            except RuntimeError:
                pass
    finally:
        # shutdown() joins the recorder first -- which is what queues the final
        # chunk's work -- and only then drains the job pool. Checking the queue
        # before the recorder has finished would see it empty and exit early,
        # abandoning the last chunk's transcript, remux and proxy. It runs on
        # every path, including a failed start, so a partially started pipeline
        # is never left with live workers.
        try:
            print("finishing up (transcript, remux, proxy, rundown)...")
        finally:
            pipeline.shutdown_until_stopped()

    if session is None:
        return 1
    failed = [f"{chunk.label}: {errors}" for chunk in session.chunks
              if (errors := chunk.errors)]
    print(f"done -- {session.directory}")
    # A FAILED terminal state is a nonzero exit even when no individual chunk
    # recorded an error -- e.g. the recording never received any media. A script
    # driving `vodpipe record` must be able to tell success from failure.
    if session.status == "failed":
        print(f"the recording failed: {session.error}", file=sys.stderr)
        return 1
    if failed:
        print("with problems:", file=sys.stderr)
        for line in failed:
            print(f"  {line}", file=sys.stderr)
        return 1
    return 0


def cmd_vod(config: Config, args) -> int:
    """Download a Twitch VOD and run it through the full pipeline.

    Same masters, proxies, per-chunk transcripts and rundowns as a live recording;
    the only difference is the source is an archived VOD instead of a live edge.
    """
    from .channels import InvalidVod, parse_vod

    try:
        video_id, canonical = parse_vod(args.url)
    except InvalidVod as exc:
        LOG.error("%s", exc)
        return 2

    pipeline = Pipeline(config)
    session = None
    try:
        pipeline.start()
        session = pipeline.download_vod(
            canonical, start=args.start, duration=args.duration)
        print(f"downloading VOD {video_id} ({session.channel}) -> "
              f"{session.directory}")
        print("press Ctrl+C to stop (chunks captured so far are kept)")
        try:
            while session.status in ("starting", "recording"):
                time.sleep(2)
        except KeyboardInterrupt:
            print("\nstopping...", file=sys.stderr)
            try:
                pipeline.stop_vod(session.session_id)
            except RuntimeError:
                pass
    finally:
        try:
            print("finishing up (transcript, remux, proxy, rundown)...")
        finally:
            pipeline.shutdown_until_stopped()

    if session is None:
        return 1
    failed = [f"{chunk.label}: {errors}" for chunk in session.chunks
              if (errors := chunk.errors)]
    print(f"done -- {session.directory}")
    if session.status == "failed":
        print(f"the download failed: {session.error}", file=sys.stderr)
        return 1
    if failed:
        print("with problems:", file=sys.stderr)
        for line in failed:
            print(f"  {line}", file=sys.stderr)
        return 1
    return 0


def _wait_for_session(pipeline: Pipeline, request_id: str, channel: str):
    """Block on one request generation's durable completion record."""
    try:
        result = pipeline.wait_for_request(request_id)
        if result is None:
            LOG.error("record request %s is no longer available", request_id)
            return None
        if result["status"] != "complete":
            LOG.error("could not start %s: %s", channel,
                      result.get("error") or result["status"])
            return None
        session = pipeline.store.get(result["session_id"])
        if session is None:
            LOG.error("record request %s completed with missing session %s",
                      request_id, result["session_id"])
        return session
    except KeyboardInterrupt:
        pipeline.cancel_request(request_id)
        print("\nstopped waiting", file=sys.stderr)
        return None


def cmd_snapshot(config: Config, args) -> int:
    request = SnapshotRequest(
        session_id=args.session_id,
        last_minutes=args.last,
        start=args.start,
        end=args.end,
        precise=args.precise,
        transcribe=not args.no_transcript,
    )
    if request.last_minutes is None and request.start is None:
        raise RuntimeError("give either --last or --start")
    # Checked before the Pipeline exists so a contradictory range costs nothing
    # and reads as a usage error rather than a geometry failure deep in ffmpeg.
    if (request.start is not None and request.end is not None
            and request.end <= request.start):
        raise RuntimeError(
            f"--end ({request.end:g}s) must be after --start ({request.start:g}s)")

    pipeline = Pipeline(config)
    exit_code = 0
    try:
        result = pipeline.snapshot(request)
        print(result.path)
        status = pipeline.wait_for_snapshot(
            result.path, require_transcript=request.transcribe)
        if status is None:
            print(
                f"snapshot completion status is unavailable for {result.path.name}",
                file=sys.stderr)
            exit_code = 1
        elif status.get("cut_status", "done") != "done" \
                or not result.path.is_file():
            print(
                f"snapshot cut failed for {result.path.name}: "
                f"{status.get('cut_error') or status.get('cut_status') or 'incomplete'}",
                file=sys.stderr)
            exit_code = 1
        elif request.transcribe and status.get("transcript_status") != "done":
            print(
                f"snapshot transcript failed for {result.path.name}: "
                f"{status.get('transcript_error') or status.get('transcript_status') or 'incomplete'}",
                file=sys.stderr)
            exit_code = 1
    finally:
        # AUD2-047: this used to run only on the happy path, so a failed cut left
        # the pools running and any queued transcription abandoned mid-flight.
        pipeline.shutdown_until_stopped()
    return exit_code


def cmd_transcribe(config: Config, args) -> int:
    from .transcribe import RollingTranscriber
    from .state import SessionStore

    # Transcribing a file on disk never touches streamlink; requiring it turned a
    # working machine into a failing one for no reason.
    tools = resolve_tools(
        {k: v for k, v in (config.get("tools") or {}).items() if v},
        need=("ffmpeg", "ffprobe"))
    transcriber = RollingTranscriber(config, tools, SessionStore(config.masters_root))
    output = args.out or args.path.with_suffix("")
    words = transcriber.transcribe_file(args.path, output)
    print(f"{len(words)} words -> {output}")
    return 0


def cmd_republish(config: Config, args) -> int:
    """Re-render the export set for chunks that already have a `words.json`.

    The words are the expensive part and they are already paid for, so changing
    something that only affects presentation -- the censor list, the language
    tag -- should not mean sending the audio to Deepgram again.

    Deliberately skips any chunk with no stored words rather than transcribing
    it: this command's contract is that it never calls a provider and never
    costs anything. A chunk that was never transcribed stays that way.
    """
    from .state import SessionStore
    from .transcribe import RollingTranscriber
    from .transcript import load_words, CorruptWordsFile
    from .locks import ResourceBusy, ResourceLock, chunk_lock_path

    store = SessionStore(config.masters_root)
    store.load_from_disk()
    sessions = store.all()
    if args.session_id:
        sessions = [s for s in sessions if s.session_id == args.session_id]
        if not sessions:
            print(f"no session {args.session_id} under {config.masters_root}")
            return 1

    # Nothing here touches media, so do not require ffmpeg, ffprobe, or
    # streamlink just to render stored words.
    tools = resolve_tools(
        {k: v for k, v in (config.get("tools") or {}).items() if v},
        need=())
    transcriber = RollingTranscriber(config, tools, store)
    rebuilt = skipped = failed = 0

    for session in sessions:
        for chunk in session.chunks:
            directory = transcriber.output_dir(session, chunk)
            try:
                mutation = ResourceLock(
                    chunk_lock_path(session.path, chunk.label),
                    timeout=60.0,
                ).acquire()
            except ResourceBusy as exc:
                print(f"  {session.session_id}/{chunk.label}: {exc}")
                failed += 1
                continue
            try:
                try:
                    _, meta = load_words(directory / "words.json")
                except CorruptWordsFile as exc:
                    print(f"  {session.session_id}/{chunk.label}: {exc}")
                    failed += 1
                    continue
                if not meta:
                    skipped += 1
                    continue
                try:
                    count = transcriber.republish(session, chunk)
                except (OSError, ValueError, RuntimeError) as exc:
                    print(f"  {session.session_id}/{chunk.label}: {exc}")
                    failed += 1
                    continue
                print(f"  {session.session_id}/{chunk.label}: {count} words")
                rebuilt += 1
            finally:
                mutation.release()

    print(f"republished {rebuilt} chunk(s); {skipped} without stored words, "
          f"{failed} failed")
    return 1 if failed else 0


def cmd_sessions(config: Config) -> int:
    from .state import SessionStore
    from .util import fmt_clock

    store = SessionStore(config.masters_root)
    store.load_from_disk()
    sessions = store.all()
    if not sessions:
        print("no sessions found under", config.masters_root)
        return 0

    print(f"{'session':<34} {'channel':<18} {'status':<12} chunks  length")
    for session in sessions:
        length = max((chunk.session_offset + chunk.duration
                      for chunk in session.chunks), default=0.0)
        print(f"{session.session_id:<34} {session.channel:<18} {session.status:<12} "
              f"{len(session.chunks):>6}  {fmt_clock(length)}")
    return 0
