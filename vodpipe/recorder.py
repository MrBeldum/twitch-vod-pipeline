"""Live recorder: streamlink -> ffmpeg segment muxer -> keyframe-aligned .ts chunks.

The recorder owns exactly one broadcast. It writes chunks, tracks where the write
head is, notes ad breaks, and enforces the free-space floor. Everything downstream
(remux, proxies, transcription, summaries) is somebody else's thread -- this one
must never block, because the only unrecoverable failure in this system is
dropping the live stream.
"""

from __future__ import annotations

import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable

from .config import Config
from .channels import parse_channel
from .locks import ChannelLock
from .quality import QualityReport, parse_available, parse_opening
from .media import (
    live_duration,
    segment_command,
    streamlink_command,
    vod_download_command,
)
from .state import (
    COMPLETE,
    FAILED,
    RECORDING,
    SOURCE_LIVE,
    SOURCE_VOD,
    STARTING,
    Chunk,
    Session,
    SessionStore,
    new_session_id,
)
from .util import LOG, Tools, free_bytes, human_bytes, popen, redact

GB = 1024 ** 3
STDERR_TAIL_CHARS = 16 * 1024
MIN_STAGNATION_SECONDS = 300.0
CLEAN_STOP_REASONS = frozenset(("stopped by user", "stopped: shutting down"))

ChunkEvent = Callable[[Session, Chunk], None]
SessionEvent = Callable[[Session], None]
FirstMediaEvent = Callable[[Session, str], None]


def _exit_code(code: int) -> str:
    """Render a process exit code the way its own logs do.

    Windows reports a child's negative exit status as an unsigned 32-bit value,
    so ffmpeg's -22 (EINVAL, `Invalid argument`) reached the dashboard as
    ``ffmpeg exited 4294967274`` -- a number that appears nowhere in ffmpeg's own
    output and cannot be searched for. Anything at or above 2^31 is the two's
    complement of the code ffmpeg actually printed.
    """
    value = int(code)
    if value >= 2 ** 31:
        value -= 2 ** 32
    return str(value)


def read_segment_rows(segment_list: Path) -> list[list[str]]:
    """Parse ffmpeg's `-segment_list_type csv` output into complete rows.

    Two traps, both of which cost a whole recorded chunk when hit:

    * ffmpeg quotes a filename containing a comma (`"cha,nnel_c000.ts",0.0,7200.0`),
      so splitting on commas puts the timestamps in the wrong fields. csv.reader
      handles the quoting correctly.
    * We poll this file while ffmpeg appends to it, so the last line may be
      half-written. Only rows terminated by a newline are complete; anything after
      the final newline is deliberately withheld until the next pass.
    """
    import csv
    import io

    try:
        text = segment_list.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []

    complete, _, _ = text.rpartition("\n")
    if not complete:
        return []

    rows = []
    for row in csv.reader(io.StringIO(complete)):
        if row and any(field.strip() for field in row):
            rows.append(row)
    return rows


def parse_segment_row(row: list[str]) -> tuple[str, float, float] | None:
    """(filename, start, end) from a csv row, or None if it is not usable yet."""
    if len(row) < 3:
        return None
    try:
        start = float(row[-2])
        end = float(row[-1])
    except (TypeError, ValueError):
        return None
    if end < start:
        return None
    return ",".join(row[:-2]), start, end


class Recorder:
    """Runs one recording session on its own thread."""

    def __init__(
        self,
        config: Config,
        tools: Tools,
        store: SessionStore,
        channel: str,
        *,
        source_kind: str = SOURCE_LIVE,
        source_url: str = "",
        lock_key: str = "",
        vod_start: float | None = None,
        vod_duration: float | None = None,
        on_chunk_started: ChunkEvent | None = None,
        on_chunk_finalized: ChunkEvent | None = None,
        on_session_ended: SessionEvent | None = None,
        on_first_media: FirstMediaEvent | None = None,
        request_token: str = "",
    ) -> None:
        self.config = config
        self.tools = tools
        self.store = store
        self.channel = parse_channel(channel)
        if source_kind not in (SOURCE_LIVE, SOURCE_VOD):
            raise ValueError(f"unknown recording source kind: {source_kind!r}")
        self.source_kind = source_kind
        # For a live channel the URL is derived; for a VOD it is the exact address
        # streamlink downloads. Held so the session manifest can record provenance.
        self.source_url = source_url
        self.vod_start = vod_start
        self.vod_duration = vod_duration
        # The cross-process exclusion key. Live recordings lock the channel so a
        # second process cannot record it twice; a VOD locks its own id instead, so
        # a VOD download and a live recording of the same channel can run at once
        # and two downloads of the same VOD cannot.
        self.lock_key = lock_key or self.channel
        self.on_chunk_started = on_chunk_started
        self.on_chunk_finalized = on_chunk_finalized
        self.on_session_ended = on_session_ended
        self.on_first_media = on_first_media
        self.request_token = request_token

        self.session: Session | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._streamlink: subprocess.Popen | None = None
        self._ffmpeg: subprocess.Popen | None = None
        self._lock: ChannelLock | None = None
        # Set on every terminal path, including a natural end-of-stream, which
        # never sets _stop. Watcher threads key off this so they cannot outlive
        # the recorder when the broadcast simply ended.
        self._terminal = threading.Event()
        self._stderr_tail = ""
        self._stderr_lock = threading.Lock()
        self._fatal_lock = threading.Lock()
        self._fatal_error = ""

        # Write-head tracking. ffprobing the growing .ts on every ad log line
        # would be far too expensive, so we probe periodically and interpolate
        # against the wall clock in between.
        self._head_lock = threading.Lock()
        self._head_seconds = 0.0
        self._head_at = time.time()

        self._stop_reason = ""
        # Startup watchdog state: when this attempt began, and when the first
        # byte of video landed. See _startup_expired().
        self._started_at = time.time()
        self._first_bytes_at = 0.0
        self._first_media_lock = threading.Lock()
        self._first_media_persisted = False
        self._first_media_callback_attempted = False
        self._media_sizes: dict[str, int] = {}
        self._media_progress_at: float | None = None

        # What Twitch offered and what we took. Filled in from streamlink's own
        # stderr as soon as it opens the stream; see _note_quality().
        self._quality = QualityReport(
            floor=int(self.config.get("recording.min_height", 0) or 0))

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> Session:
        if self._thread and self._thread.is_alive():
            raise RuntimeError(f"{self.channel} is already recording")

        floor = float(self.config.get("recording.free_space_floor_gb", 50)) * GB
        root = self.config.masters_root
        root.mkdir(parents=True, exist_ok=True)
        available = free_bytes(root)
        if available < floor:
            raise RuntimeError(
                f"refusing to start: {human_bytes(available)} free, "
                f"floor is {human_bytes(floor)}"
            )

        # Held for the whole session so a second process cannot record the same
        # source into the same files. Keyed by lock_key -- the channel for live,
        # the VOD id for a download. Released in _finish().
        self._lock = ChannelLock(root, self.lock_key).acquire()

        session: Session | None = None
        registered = False
        thread: threading.Thread | None = None
        try:
            started = time.time()
            session_id, directory = self._claim_directory(started)
            session = Session(
                session_id=session_id,
                channel=self.channel,
                started_at=started,
                directory=str(directory),
                status=STARTING,
                source_kind=self.source_kind,
                source_url=self.source_url,
            )
            self.session = session
            self.store.add(session)
            registered = True
            self._stop.clear()
            self._terminal.clear()
            self._streamlink = None
            self._ffmpeg = None
            self._started_at = time.time()
            self._first_bytes_at = 0.0
            self._first_media_persisted = False
            self._first_media_callback_attempted = False
            self._media_sizes = {}
            self._media_progress_at = None
            with self._stderr_lock:
                self._stderr_tail = ""
            with self._fatal_lock:
                self._fatal_error = ""
            thread = threading.Thread(
                target=self._run, name=f"rec-{self.channel}", daemon=True)
            self._thread = thread
            thread.start()
            return session
        except Exception as exc:
            # Thread.start() can theoretically raise after the target began. In
            # that case the recorder thread owns process teardown and _finish;
            # wait for it before making the lock available to a successor.
            try:
                if thread is not None and thread.ident is not None:
                    try:
                        self.stop(f"recorder startup failed: {exc}")
                    finally:
                        thread.join()
                else:
                    try:
                        if registered and session is not None:
                            self.store.update(
                                session, status=FAILED, ended_at=time.time(),
                                error=f"recorder startup failed: {exc}")
                    except Exception:
                        LOG.exception("%s: could not persist startup failure",
                                      self.channel)
                    finally:
                        self._terminal.set()
            finally:
                self._release_lock()
            raise

    def _claim_directory(self, started: float) -> tuple[str, Path]:
        """Create the session directory exclusively.

        `exist_ok=True` would silently adopt another recorder's directory, which
        is how two sessions end up overwriting each other's masters.
        """
        for _ in range(5):
            session_id = new_session_id(self.channel, started)
            directory = self.config.channel_root(self.channel) / session_id
            try:
                directory.mkdir(parents=True, exist_ok=False)
            except FileExistsError:
                continue
            for sub in ("live", "master", "snapshots", "transcripts", "logs"):
                (directory / sub).mkdir(parents=True, exist_ok=True)
            return session_id, directory
        raise RuntimeError("could not allocate a unique session directory")

    def stop(self, reason: str = "stopped by user") -> None:
        """Ask for a graceful stop. Only the producer is signalled.

        Terminating ffmpeg here would be a mistake: on Windows `terminate()` is
        `TerminateProcess`, which gives it no chance to flush. Killing only
        streamlink closes the pipe, ffmpeg reads EOF, and it finalises the segment
        it is on and writes the last csv row -- which is how the final chunk keeps
        its tail and its true duration.
        """
        self._stop_reason = reason
        self._stop.set()
        if self._streamlink and self._streamlink.poll() is None:
            try:
                self._streamlink.terminate()
            except OSError:
                pass

    def join(self, timeout: float | None = None) -> None:
        if self._thread:
            self._thread.join(timeout=timeout)

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    # -- main loop ---------------------------------------------------------

    def _run(self) -> None:
        error = ""
        watchers: list[threading.Thread] = []
        try:
            session = self.session
            if session is None:
                raise RuntimeError("recorder started without a session")
            live_dir = session.path / "live"
            pattern = live_dir / f"{self.channel}_{session.session_id}_c%03d.ts"
            segment_list = live_dir / "segments.csv"
            segment_list.write_text("", encoding="utf-8")

            proxy = str(self.config.get("network.proxy", "") or "")
            quality = self.config.get("recording.quality", "best")
            no_config = bool(self.config.get("recording.streamlink_no_config"))
            oauth = self.config.secret("twitch_oauth_token")
            if self.source_kind == SOURCE_VOD:
                sl_cmd = vod_download_command(
                    self.tools, self.source_url, quality,
                    oauth_token=oauth, no_config=no_config, proxy=proxy,
                    start_offset=self.vod_start, duration=self.vod_duration,
                )
            else:
                url = f"https://twitch.tv/{self.channel}"
                sl_cmd = streamlink_command(
                    self.tools, url, quality,
                    oauth_token=oauth,
                    low_latency=bool(self.config.get("recording.twitch_low_latency")),
                    no_config=no_config, proxy=proxy,
                )
            ff_cmd = segment_command(
                self.tools, pattern, segment_list,
                int(self.config.get("recording.chunk_seconds", 7200)),
            )

            self._streamlink = popen(sl_cmd, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE)
            self._ffmpeg = popen(ff_cmd, stdin=self._streamlink.stdout,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            # The parent must drop its handle or ffmpeg never sees EOF.
            if self._streamlink.stdout:
                self._streamlink.stdout.close()

            # AUD2-045: c000 is registered *before* the segment watcher starts.
            # The other way round, ffmpeg could complete its first CSV row and
            # the watcher could register c000 from it before this call ran,
            # leaving two Chunk(index=0) records sharing a label and media name
            # but with different statuses -- duplicate finalisation, and a false
            # failure once one of them reclaimed the shared .ts.
            self._open_chunk(0, 0.0)

            watchers = [
                threading.Thread(target=self._pump_streamlink_log,
                                 name="sl-log", daemon=True),
                threading.Thread(target=self._pump_ffmpeg_log,
                                 name="ff-log", daemon=True),
                threading.Thread(target=self._watch_segments,
                                 args=(segment_list,), name="segments", daemon=True),
                threading.Thread(target=self._watch_disk, name="disk", daemon=True),
            ]
            for thread in watchers:
                thread.start()

            while not self._stop.is_set():
                if self._ffmpeg.poll() is not None:
                    break
                if self._streamlink.poll() is not None:
                    # streamlink retries internally; an exit means the broadcast
                    # is genuinely over or unreachable.
                    LOG.info("%s: streamlink exited (%s)",
                             self.channel, self._streamlink.returncode)
                    break
                if self._startup_expired(live_dir):
                    break
                if self._stagnation_expired(live_dir):
                    break
                self._refresh_head()
                self._stop.wait(2.0)

            error = self._shutdown_processes()
        except Exception as exc:
            error = str(exc)
            LOG.exception("%s: recorder crashed", self.channel)
        finally:
            # Every terminal path lands here, including a failed ffmpeg spawn
            # after streamlink already started, so nothing is left running.
            self._terminal.set()
            try:
                self._reap()
            finally:
                try:
                    for thread in watchers:
                        if thread.ident is None:
                            continue
                        try:
                            thread.join(timeout=15)
                        except RuntimeError:
                            LOG.exception("%s: could not join watcher %s",
                                          self.channel, thread.name)
                    fatal = self._fatal_detail()
                    if fatal:
                        error = f"{fatal}; {error}" if error else fatal
                finally:
                    try:
                        self._close_process_pipes()
                    finally:
                        try:
                            self._finish(error)
                        except Exception:
                            # _finish() releases the lock in its own finally block.
                            # Persistence failures stay visible without escaping
                            # the recorder thread as unhandled exceptions.
                            LOG.exception("%s: recorder finish failed", self.channel)

    def _startup_expired(self, live_dir: Path) -> bool:
        """Give up if not one byte of video has arrived.

        streamlink is configured to retry forever, which is right once a stream is
        established -- a broadcast that drops for a minute should be waited out.
        It is wrong at the very beginning: a channel that was not live after all
        left a session sitting at `recording`, with an empty chunk, for as long as
        the application ran.

        Only zero bytes counts. Once anything has been written the stream existed,
        and from then on retrying is exactly the behaviour we want.
        """
        limit = float(self.config.get("recording.startup_timeout_seconds", 120))
        if self._observe_first_media(live_dir):
            return False

        if limit <= 0:
            return False

        if time.time() - self._started_at < limit:
            return False

        LOG.warning("%s: no video after %.0fs; giving up on this attempt",
                    self.channel, limit)
        self._stop_reason = (
            f"no video arrived within {limit:.0f}s -- the channel was not "
            "actually streaming")
        self._stop.set()
        return True

    def _observe_first_media(self, live_dir: Path) -> bool:
        """Mark first media once; persist and announce it without risking capture."""
        with self._first_media_lock:
            if not self._first_bytes_at:
                received = False
                for path in live_dir.glob("*.ts"):
                    try:
                        if path.stat().st_size > 0:
                            received = True
                            break
                    except OSError:
                        continue
                if not received:
                    return False

                # Media existence is authority. Do this before optional state or
                # control writes so their failure cannot restart the startup
                # watchdog or terminate an already established capture.
                self._first_bytes_at = time.time()
                LOG.info("%s: receiving video", self.channel)

            session = self.session
            if session is not None and not self._first_media_persisted:
                try:
                    if (session.status == STARTING
                            or any(chunk.status == STARTING
                                   for chunk in session.chunks)):
                        self.store.confirm_first_media(session)
                    else:
                        # confirm_first_media may have changed the live object
                        # before its manifest write failed. A later observation
                        # must still retry that write.
                        self.store.flush(session)
                except Exception as exc:
                    self._nonfatal_failed("first-media state update", exc)
                else:
                    self._first_media_persisted = True

            if (session is not None and self.on_first_media is not None
                    and not self._first_media_callback_attempted):
                self._first_media_callback_attempted = True
                try:
                    self.on_first_media(session, self.request_token)
                except Exception as exc:
                    self._nonfatal_failed("first-media callback", exc)
            return True

    def _stagnation_expired(self, live_dir: Path) -> bool:
        """Fail an established capture whose media files stop advancing.

        File growth is checked across every live segment rather than only the
        active chunk. That makes a normal muxer rollover count as progress even
        during the short interval before the segment watcher registers it.
        """
        if not self._first_bytes_at:
            return False

        sizes: dict[str, int] = {}
        for path in live_dir.glob("*.ts"):
            try:
                sizes[path.name] = path.stat().st_size
            except OSError:
                # A closed segment can be reclaimed between glob() and stat().
                continue

        now = time.monotonic()
        if any(size > self._media_sizes.get(name, 0)
               for name, size in sizes.items()):
            self._media_progress_at = now
        elif self._media_progress_at is None:
            self._media_progress_at = now
        self._media_sizes = sizes

        startup = float(self.config.get("recording.startup_timeout_seconds", 120))
        limit = max(MIN_STAGNATION_SECONDS, max(0.0, startup) * 2.0)
        assert self._media_progress_at is not None
        stalled_for = now - self._media_progress_at
        if stalled_for < limit:
            return False

        message = (f"media write head did not advance for {limit:.0f}s after "
                   "recording began")
        LOG.error("%s: %s; stopping stuck capture", self.channel, message)
        self._fail_attempt(message)
        return True

    def _shutdown_processes(self) -> str:
        """Close the pipeline down in producer-then-consumer order.

        Returns a description of any child failure, so a nonzero exit is reported
        as a failed session instead of a clean one.
        """
        if self._streamlink and self._streamlink.poll() is None:
            try:
                self._streamlink.terminate()
            except OSError:
                pass

        grace = float(self.config.get("recording.ffmpeg_grace_seconds", 120))
        if self._ffmpeg:
            try:
                # ffmpeg is finalising the current segment here; it needs the time.
                self._ffmpeg.wait(timeout=grace)
            except subprocess.TimeoutExpired:
                LOG.warning("%s: ffmpeg did not finish within %.0fs, killing",
                            self.channel, grace)
                self._ffmpeg.kill()
                # kill() only signals. Waiting here is what collects the exit
                # code, and _classify_exit() reads it on the very next line.
                self._wait_quietly(self._ffmpeg, "ffmpeg")
        if self._streamlink:
            try:
                self._streamlink.wait(timeout=20)
            except subprocess.TimeoutExpired:
                self._streamlink.kill()
                self._wait_quietly(self._streamlink, "streamlink")

        return self._classify_exit()

    def _wait_quietly(self, proc: subprocess.Popen, what: str,
                      timeout: float = 15.0) -> None:
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            LOG.warning("%s: %s ignored being killed", self.channel, what)

    def _classify_exit(self) -> str:
        """Turn child return codes into a verdict.

        An expected stop makes a nonzero code from *either* child meaningless. We
        terminate streamlink deliberately, and that closes the pipe under ffmpeg
        part way through a transport-stream packet -- so ffmpeg reaches EOF on a
        truncated final packet and exits `AVERROR_INVALIDDATA`. That is the
        consequence of stopping, not evidence of a bad recording.

        Treating it as a failure meant every recording the user stopped by hand,
        and every `--minutes` run, was marked `failed` while holding two complete,
        validated masters. Only a real broadcast ending cleanly scored a pass.

        Nothing is being hidden by this. Each chunk's master is validated
        independently before its `.ts` is reclaimed, and a genuine failure is
        recorded against that chunk, where it belongs.
        """
        problems = []
        expected_stop = self._stop.is_set()

        code = self._streamlink.returncode if self._streamlink else None
        if code not in (None, 0) and not expected_stop:
            problems.append(f"streamlink exited {_exit_code(code)}")

        code = self._ffmpeg.returncode if self._ffmpeg else None
        if code not in (None, 0):
            if expected_stop:
                LOG.info("%s: ffmpeg exited %s after we closed the stream "
                         "(expected when stopping mid-packet)",
                         self.channel, _exit_code(code))
            else:
                # Nobody asked it to stop, so this one is worth surfacing.
                problems.append(f"ffmpeg exited {_exit_code(code)}")

        if problems:
            with self._stderr_lock:
                tail = self._stderr_tail.strip()
            detail = f" -- {tail[-400:]}" if tail else ""
            return "; ".join(problems) + detail
        return ""

    def _fail_attempt(self, message: str) -> None:
        """Record the first fatal asynchronous failure and wake the main loop.

        See `_exit_code` for why process codes are normalised on the way in.
        """
        message = redact(message.strip()) or "recorder worker failed"
        with self._fatal_lock:
            if not self._fatal_error:
                self._fatal_error = message
        self._stop.set()

    def _fatal_detail(self) -> str:
        with self._fatal_lock:
            error = self._fatal_error
        if not error:
            return ""
        with self._stderr_lock:
            tail = self._stderr_tail.strip()
        return f"{error} -- {tail[-400:]}" if tail else error

    def _reap(self) -> None:
        """Make sure no child survives this recorder, on any path."""
        for proc in (self._streamlink, self._ffmpeg):
            if proc is None:
                continue
            try:
                if proc.poll() is None:
                    proc.kill()
            except Exception:
                LOG.exception("%s: could not signal a child process", self.channel)
            try:
                proc.wait(timeout=10)
            except Exception:
                LOG.warning("%s: a child process could not be reaped", self.channel)

    def _close_process_pipes(self) -> None:
        """Close parent-side child pipes after their pump threads have stopped."""
        for proc in (self._streamlink, self._ffmpeg):
            if proc is None:
                continue
            for name in ("stdin", "stdout", "stderr"):
                pipe = getattr(proc, name, None)
                if pipe is None:
                    continue
                try:
                    pipe.close()
                except (OSError, ValueError):
                    pass

    def _finish(self, error: str) -> None:
        session = self.session
        final_error = redact(error.strip())

        def add_error(message: str) -> None:
            nonlocal final_error
            detail = redact(message.strip())
            if detail:
                final_error = f"{final_error}; {detail}" if final_error else detail

        try:
            if not session:
                return

            # Give the segment watcher a moment to pick up the final csv line that
            # ffmpeg writes as it exits.
            try:
                time.sleep(1.5)
                self._finalize_remaining_chunks()
            except Exception as exc:
                add_error(f"final chunk reconciliation failed: {exc}")
                LOG.exception("%s: final chunk reconciliation failed", self.channel)

            if not self._first_bytes_at:
                add_error(self._stop_reason
                          or "recording ended before any media arrived")
            elif not final_error and self._stop_reason not in CLEAN_STOP_REASONS:
                add_error(self._stop_reason)

            status = FAILED if final_error else COMPLETE
            ended_at = time.time()
            # Set terminal truth before touching disk. SessionStore mutates before
            # flushing too, but injected failures and alternate implementations
            # must not leave the live object in STARTING/RECORDING.
            session.status = status
            session.ended_at = ended_at
            session.error = final_error
            try:
                self.store.update(
                    session, status=status, ended_at=ended_at, error=final_error)
            except Exception as exc:
                add_error(f"could not persist final session state: {exc}")
                status = FAILED
                session.status = FAILED
                session.ended_at = ended_at
                session.error = final_error
                LOG.exception("%s: could not persist final session state",
                              self.channel)
                try:
                    # A one-shot update failure should not strand a recording
                    # manifest. This also persists the diagnostic and FAILED
                    # verdict rather than retrying the optimistic verdict.
                    self.store.flush(session)
                except Exception:
                    LOG.exception("%s: final session state retry failed",
                                  self.channel)
        finally:
            try:
                self._release_lock()
            except Exception:
                LOG.exception("%s: could not release the channel lock", self.channel)
            finally:
                if session is not None:
                    LOG.info("%s: session %s ended (%s)", self.channel,
                             session.session_id, session.status)
                    if self.on_session_ended:
                        try:
                            self.on_session_ended(session)
                        except Exception:
                            LOG.exception("session-ended handler failed")

    def _release_lock(self) -> None:
        """Release this recorder's channel lease exactly once."""
        if self._lock is not None:
            self._lock.release()
            self._lock = None

    # -- chunks ------------------------------------------------------------

    def _open_chunk(self, index: int, session_offset: float) -> Chunk:
        session = self.session
        assert session is not None
        chunk = Chunk(
            index=index,
            session_id=session.session_id,
            channel=self.channel,
            started_at=time.time(),
            ts_name=f"{self.channel}_{session.session_id}_c{index:03d}.ts",
            master_name=f"{self.channel}_{session.session_id}_c{index:03d}.mp4",
            session_offset=round(session_offset, 3),
            status=(STARTING if session.status == STARTING
                    and not self._first_bytes_at else RECORDING),
        )
        self.store.add_chunk(session, chunk)
        with self._head_lock:
            self._head_seconds = 0.0
            self._head_at = time.time()
        LOG.info("%s: chunk %s opened", self.channel, chunk.label)
        if self.on_chunk_started:
            try:
                self.on_chunk_started(session, chunk)
            except Exception:
                LOG.exception("chunk-started handler failed")
        return chunk

    def _watch_segments(self, segment_list: Path) -> None:
        """Tail ffmpeg's csv segment list; each complete row means a chunk closed.

        The csv carries the muxer's own start/end times, which is a better source
        of truth for chunk duration than anything we could measure from outside.
        """
        seen = 0
        while True:
            seen = self._drain_segment_rows(segment_list, seen)

            if self._terminal.is_set() or (self._ffmpeg and self._ffmpeg.poll() is not None):
                # ffmpeg writes its last row as it exits, so take one more pass
                # once it has actually gone.
                self._drain_segment_rows(segment_list, seen)
                return
            time.sleep(1.0)

    def _drain_segment_rows(self, segment_list: Path, seen: int) -> int:
        """Handle any newly completed rows. Returns the new consumed-row count."""
        for index, row in enumerate(read_segment_rows(segment_list)):
            if index < seen:
                continue
            parsed = parse_segment_row(row)
            if parsed is None:
                # Never advance past a row we could not read: a row observed
                # mid-append would otherwise be skipped forever, orphaning a
                # recorded chunk. It will parse on a later pass.
                LOG.debug("segment row %d not yet parsable: %r", index, row)
                return seen
            self._handle_segment_row(index, *parsed)
            seen = index + 1
        return seen

    def _handle_segment_row(self, index: int, name: str,
                            start: float, end: float) -> None:
        session = self.session
        if not session:
            return

        duration = max(0.0, end - start)
        chunk = session.chunk(index)
        if chunk is None:
            # ffmpeg can emit a csv row for a successor we never opened
            # (the floor refused it, or Stop landed on the rollover). That
            # chunk is still on disk and must not be registered at offset 0
            # overlapping c000.
            offset = 0.0
            if index > 0:
                previous = session.chunk(index - 1)
                if previous is not None:
                    offset = previous.session_offset + previous.duration
                elif session.chunks:
                    offset = max(item.session_offset + item.duration
                                 for item in session.chunks)
            chunk = self._open_chunk(index, offset)

        ts_path = session.path / "live" / chunk.ts_name
        size = ts_path.stat().st_size if ts_path.exists() else 0
        if size > 0:
            self._observe_first_media(session.path / "live")
        self.store.update_chunk(
            session, chunk,
            status="remuxing",
            duration=round(duration, 3),
            size_bytes=size,
            ended_at=time.time(),
        )
        LOG.info("%s: chunk %s closed (%.1fs, %s)",
                 self.channel, chunk.label, duration, human_bytes(size))

        if self.on_chunk_finalized:
            try:
                self.on_chunk_finalized(session, chunk)
            except Exception:
                LOG.exception("chunk-finalized handler failed")

        # Only open a successor if the muxer is still running.
        if self._stop.is_set() or (self._ffmpeg and self._ffmpeg.poll() is not None):
            return

        if not self._space_for_new_chunk():
            self.stop("stopped: free space below floor")
            return

        self._open_chunk(index + 1, chunk.session_offset + duration)

    def _reconcile_generated_files(self) -> None:
        """Register every .ts ffmpeg actually produced, tracked or not.

        ffmpeg owns segment rollover, so it can open a successor we never learned
        about -- when it exits before writing the csv row, or when we stopped at
        the free-space floor a moment after it rolled over. An unregistered .ts is
        recorded video that would never be remuxed, so it is adopted here.
        """
        session = self.session
        if not session:
            return
        live_dir = session.path / "live"
        if not live_dir.exists():
            return

        known = {chunk.ts_name for chunk in session.chunks}
        prefix = f"{self.channel}_{session.session_id}_c"
        for path in sorted(live_dir.glob(f"{prefix}*.ts")):
            if path.name in known or path.stat().st_size == 0:
                continue
            try:
                index = int(path.stem.rsplit("_c", 1)[1])
            except (IndexError, ValueError):
                continue
            offset = max((chunk.session_offset + chunk.duration
                          for chunk in session.chunks), default=0.0)
            LOG.warning("%s: adopting untracked segment %s", self.channel, path.name)
            chunk = self._open_chunk(index, offset)
            self.store.update_chunk(session, chunk, ts_name=path.name)

    def _finalize_remaining_chunks(self) -> None:
        """Close any chunk ffmpeg never wrote a csv line for (an abrupt exit)."""
        session = self.session
        if not session:
            return
        self._observe_first_media(session.path / "live")
        self._reconcile_generated_files()
        for chunk in session.chunks:
            if chunk.status not in (STARTING, RECORDING):
                continue
            ts_path = session.path / "live" / chunk.ts_name
            if not ts_path.exists() or ts_path.stat().st_size == 0:
                self.store.update_chunk(session, chunk, status=FAILED,
                                        master_error="no data recorded")
                continue
            duration = live_duration(self.tools, ts_path)
            self.store.update_chunk(
                session, chunk,
                status="remuxing",
                duration=round(duration, 3),
                size_bytes=ts_path.stat().st_size,
                ended_at=time.time(),
            )
            LOG.info("%s: chunk %s recovered from an abrupt exit (%.1fs)",
                     self.channel, chunk.label, duration)
            if self.on_chunk_finalized:
                try:
                    self.on_chunk_finalized(session, chunk)
                except Exception:
                    LOG.exception("chunk-finalized handler failed")

    # -- write head --------------------------------------------------------

    def _refresh_head(self) -> None:
        session = self.session
        if not session:
            return
        chunk = session.active_chunk()
        if not chunk:
            return
        ts_path = session.path / "live" / chunk.ts_name
        duration = live_duration(self.tools, ts_path)
        if duration <= 0:
            return
        with self._head_lock:
            self._head_seconds = duration
            self._head_at = time.time()

    def head_position(self) -> float:
        """Best estimate of the current chunk-relative write position, in seconds."""
        with self._head_lock:
            return self._head_seconds + (time.time() - self._head_at)

    def measured_head_position(self) -> float:
        """Last media duration actually observed, without wall-clock extrapolation."""
        with self._head_lock:
            return self._head_seconds

    # -- logs and ads ------------------------------------------------------

    def _pump_streamlink_log(self) -> None:
        session = self.session
        proc = self._streamlink
        if not session or not proc or not proc.stderr:
            self._fail_attempt("streamlink stderr pump started without a pipe")
            return
        patterns = [str(item).lower()
                    for item in self.config.get("ads.event_patterns", [])]
        detect = bool(self.config.get("ads.log_events", True))
        log_path = session.path / "logs" / "streamlink.log"

        def handle(line: str) -> None:
            lowered = line.lower()
            if "available streams:" in lowered or "opening stream:" in lowered:
                self._note_quality(line)
            elif detect and any(pattern in lowered for pattern in patterns):
                self._note_ad_event(line)
            elif " error" in lowered or lowered.startswith("error"):
                LOG.warning("%s streamlink: %s", self.channel, line)

        self._pump_log(proc, "streamlink", log_path, handle)

    def _pump_ffmpeg_log(self) -> None:
        session = self.session
        proc = self._ffmpeg
        if not session or not proc or not proc.stderr:
            self._fail_attempt("ffmpeg stderr pump started without a pipe")
            return
        log_path = session.path / "logs" / "ffmpeg.log"
        self._pump_log(proc, "ffmpeg", log_path)

    def _pump_log(self, proc: subprocess.Popen, source: str, log_path: Path,
                  handle: Callable[[str], None] | None = None) -> None:
        """Drain one stderr pipe even when logging or line handling fails."""
        pipe = proc.stderr
        if pipe is None:
            self._fail_attempt(f"{source} stderr pump lost its pipe")
            return

        sink = None
        try:
            try:
                sink = log_path.open("a", encoding="utf-8", errors="replace")
            except Exception as exc:
                self._pump_failed(source, "open log", exc)

            while True:
                try:
                    raw = pipe.readline()
                except Exception as exc:
                    self._pump_failed(source, "read stderr", exc, fatal=True)
                    return
                if not raw:
                    return

                line = raw.decode("utf-8", "replace").rstrip()
                if not line:
                    continue
                self._append_stderr(source, line)

                if sink is not None:
                    try:
                        sink.write(line + "\n")
                        sink.flush()
                    except Exception as exc:
                        self._pump_failed(source, "write log", exc)
                        try:
                            sink.close()
                        except Exception as close_exc:
                            self._pump_failed(source, "close log", close_exc)
                        sink = None

                if handle is not None:
                    try:
                        handle(line)
                    except Exception as exc:
                        self._pump_failed(source, "process log line", exc)
        finally:
            if sink is not None:
                try:
                    sink.close()
                except Exception as exc:
                    self._pump_failed(source, "close log", exc)
            # This process may run for days across many sessions; a leaked pipe
            # handle per recording adds up.
            try:
                pipe.close()
            except Exception as exc:
                self._pump_failed(source, "close stderr", exc)

    def _append_stderr(self, source: str, line: str) -> None:
        entry = f"[{source}] {redact(line)}\n"
        with self._stderr_lock:
            self._stderr_tail = (self._stderr_tail + entry)[-STDERR_TAIL_CHARS:]

    def _pump_failed(self, source: str, action: str, exc: Exception, *,
                     fatal: bool = False) -> None:
        detail = redact(str(exc)) or exc.__class__.__name__
        message = f"{source} stderr pump could not {action}: {detail}"
        LOG.error("%s: %s", self.channel, message)
        self._append_stderr(source, message)
        if fatal:
            self._fail_attempt(message)

    def _nonfatal_failed(self, what: str, exc: Exception) -> None:
        detail = redact(str(exc)) or exc.__class__.__name__
        message = f"{what} failed: {detail}"
        LOG.error("%s: %s", self.channel, message)
        self._append_stderr("recorder", message)

    def _note_quality(self, line: str) -> None:
        """Learn the ladder Twitch offered and the rendition streamlink took.

        Both arrive on stderr within a second or two of start, and both are
        recorded even when they are unremarkable -- the value is in being able to
        answer "why is this 720p?" months later from `session.json` alone. The
        two lines can also repeat, because streamlink reopens the stream on its
        own retries; updates are therefore idempotent rather than append-only.
        """
        session = self.session
        if session is None:
            return

        available = parse_available(line)
        if available is not None:
            self._quality.available = available
        opened = parse_opening(line)
        if opened:
            self._quality.selected = opened
        if available is None and not opened:
            return

        report = self._quality
        changes: dict[str, Any] = {
            "quality_selected": report.selected,
            "quality_available": list(report.available),
        }
        # Only judge once streamlink has said what it opened; an `Available
        # streams:` line on its own would otherwise raise a false alarm in the
        # window before the rendition is chosen.
        if not report.known:
            try:
                self.store.update(session, **changes)
            except Exception as exc:
                self._nonfatal_failed("quality metadata update", exc)
            return

        warning = report.describe()
        changes["quality_warning"] = warning
        try:
            self.store.update(session, **changes)
        except Exception as exc:
            self._nonfatal_failed("quality metadata update", exc)
        if not warning:
            LOG.info("%s: recording %s", self.channel, report.selected)
            return

        LOG.warning("%s: %s", self.channel, warning)
        policy = str(self.config.get("recording.on_low_quality", "warn")).lower()
        if policy == "refuse":
            message = f"refusing to record below {report.floor}p: {warning}"
            self._stop_reason = message
            self._fail_attempt(message)

    def _note_ad_event(self, line: str) -> None:
        """Record that Twitch served ads. Metadata only -- never an exclusion.

        streamlink filters ad segments out of the stream before it reaches our
        recorder, so no interval of the file we are writing corresponds to this
        event. The session position is stored as a rough "where in the broadcast
        were we told about this", useful for eyeballing a log, and nothing reads
        it as a media boundary.
        """
        session = self.session
        if session is None:
            return
        kind = ("preroll" if "pre-roll" in line.lower() else "break")
        try:
            self.store.add_ad_event(session, kind, line.strip(),
                                    time.time(), self._session_position())
        except Exception as exc:
            self._nonfatal_failed("ad metadata update", exc)
        LOG.info("%s: ad event noted (%s) -- no media is excluded",
                 self.channel, kind)

    def _session_position(self) -> float:
        """Write position relative to the start of the session, not the chunk."""
        session = self.session
        if not session:
            return 0.0
        chunk = session.active_chunk()
        base = chunk.session_offset if chunk else 0.0
        return base + self.head_position()

    # -- disk --------------------------------------------------------------

    def _space_for_new_chunk(self) -> bool:
        floor = float(self.config.get("recording.free_space_floor_gb", 50)) * GB
        available = free_bytes(self.config.masters_root)
        if available < floor:
            LOG.error("%s: %s free is below the %s floor -- not opening a new chunk",
                      self.channel, human_bytes(available), human_bytes(floor))
            return False
        return True

    def _watch_disk(self) -> None:
        reserve = float(self.config.get("recording.hard_reserve_gb", 10)) * GB
        while not self._terminal.wait(30.0):
            if self._stop.is_set():
                return
            available = free_bytes(self.config.masters_root)
            if available < reserve:
                LOG.error("%s: %s free is below the hard reserve -- stopping now",
                          self.channel, human_bytes(available))
                self.stop("stopped: disk space exhausted")
                return
