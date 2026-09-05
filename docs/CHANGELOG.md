# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.1.0] — 2026-09-05

macOS and Linux, and what a full-codebase audit found.

### Added

- **macOS and Linux run the pipeline and dashboard.** "Show in folder" uses Finder
  (`open -R`) or `xdg-open`; tool discovery checks Homebrew, MacPorts, `~/.local/bin` and
  `~/.grok/bin` when PATH does not have the tool; `h264_videotoolbox` is probed for proxies
  after the AMD/NVIDIA/Intel encoders and can be chosen explicitly. On macOS the app
  command opens the system browser rather than a Chrome window, because Chrome there
  outlives its last window and the pipeline would keep recording behind a closed one.
  CI runs the full suite on `macos-latest` and `ubuntu-latest`.
- **A pip install keeps its data in the per-user data directory** (`%LOCALAPPDATA%\vodpipe`,
  `~/Library/Application Support/vodpipe`, `~/.local/share/vodpipe`) instead of inside
  `site-packages`. A clone still keeps `config.json` beside the code, and an existing
  install that already has one there is left alone. `VODPIPE_HOME` overrides either.

### Fixed

- **The chunk progress bar never rendered.** The dashboard's Content Security Policy
  blocks inline `style` attributes, and the bar's width was one; it is set through the
  CSSOM now, and a test refuses any `style:` attribute in `app.js`.
- **The dashboard poll re-read every transcript every two seconds.** Working out whether
  a chunk is eligible for a report loaded and rendered its whole `words.json` — for every
  chunk of every session, on every 2 s state poll. The verdict is cached against the
  size and mtime of the files it is read from, and the model input is only rendered for
  the report job itself.
- **A half-open chat socket was never noticed.** The IRC reader's 30 s receive timeout
  just looped, so a connection the network had silently dropped stayed "connected" for the
  rest of the recording with no chat captured. Twitch pings every five minutes; after
  330 s of silence the reader pings once, and after 400 s it drops the socket so the
  existing reconnect loop takes over.
- **A corrupt `config.json` produced a traceback** instead of the one-line message every
  other configuration error gets; the CLI now reports it and exits 2.
- **`vodpipe app --verbose` was not verbose.** Adding the log file re-initialised logging
  at INFO.
- `index.md`'s session table had a separator one column short of its header, so markdown
  renderers did not draw it as a table.
- `snapshot.py` referenced `Sequence` without importing it (masked by postponed
  annotations); unused imports and locals removed across five modules.

## [1.0.2] — 2026-09-02

The report engine, which had never once produced a report through `grok -p`, and the
icons, which could not be replaced.

### Fixed

- **`grok -p` could not write a report at all, and the failure was certain rather than
  intermittent.** Grok's CLI offloads any `--prompt-file` over roughly 24 KB: the prompt
  never enters the conversation, and the model is handed a stub it has to `read_file` its
  way out of. Every real transcript is around 104 KB, so `--max-turns 1` spent the only
  turn on the read and the run was cancelled before an answer existed. Three further
  faults sat behind it: `--tools ""` restricts nothing (an empty allowlist reads as
  "unset", leaving all 26 built-ins live along with any MCP servers imported from another
  application's configuration), `--output-format plain` prints every assistant message so
  the model's narration would have been published as the opening of the report, and the
  shared prompt's "Write `report.md`" read to an agent as a file-writing task. Grok is now
  given a working directory holding `transcript.md`, an instruction small enough to arrive
  inline, four tools and a `summary.max_turns` budget; it writes `report.md` and the
  pipeline reads that back. `claude -p` is unchanged — its empty tool list genuinely does
  disable tools — and the two argvs are deliberately not alike.
- **Every chunk was queued for two reports.** Stitching a chunk boundary changes the
  transcript generation of both chunks it touches, and the re-queue bypassed the chat gate
  that ordinary finalisation uses. The newer chunk's chat was still downloading, so it was
  reported once without the audience and again with it.
- **Replacing the app icon did nothing.** Three independent reasons, all now fixed: the
  icons were six hand-exported files with no stated source, so changing `docs/logo.png`
  changed only the README; `ensure_host` compared the compiled exe against `host.cs`
  alone, so a new icon never triggered a rebuild; and nothing told the Windows shell to
  drop its cached icon afterwards. Every icon is now generated from `docs/logo.png` by
  `packaging/prebuild.py`, which runs before every compile.
- **The Windows host reported version 1.0.0 two releases on.** The version was a literal
  in `host.cs`; it is now generated from `vodpipe.__version__` into `version.g.cs`, so
  Apps & Features and the exe's own properties follow the package.
- **The dashboard's overload 503 is now actually delivered on Windows.** The rejection
  wrote the response and closed a socket whose inbound request had never been read, and
  Windows resets such a socket rather than closing it, discarding the response already
  queued for send — so a client saw `WinError 10053` instead of
  `dashboard is busy; retry shortly`. Rejection now happens on bounded worker threads
  that drain the request before closing, so the accept loop pays nothing for it; when the
  rejection queue is full the connection is dropped immediately, exactly as before.

## [1.0.1] — 2026-09-01

Packaging and repository presentation only. No change to the pipeline; the test suite is
unchanged at 961 tests.

### Fixed

- The README is the PyPI project description, and its logo was a repository-relative path
  that PyPI cannot resolve, so the project page opened with a broken image. It is now an
  absolute URL, which renders identically on GitHub.

### Changed

- The repository root holds nine files instead of fourteen. `CONTRIBUTING.md`,
  `SECURITY.md` and `CODE_OF_CONDUCT.md` moved to `.github/`, where GitHub reads them the
  same way; `CHANGELOG.md` and `DESIGN.md` moved to `docs/`. `LICENSE`, `README.md` and
  `CLAUDE.md` stay at the root because their location is what makes them work.
- `Publish to PyPI` runs on demand rather than on every release, so it cannot record a
  failed deployment before its one-time trusted-publisher setup is done.

## [1.0.0] — 2026-09-01

First public release. The pipeline has been proven against real multi-hour recordings and
the full suite of ~960 tests passes.

### Recording

- Live Twitch capture in configurable chunks (default 2 hours), recorded as MPEG-TS and
  remuxed to MP4 on close so a chunk can be transcribed and snapshotted while it is still
  being written.
- Past VOD download through the identical chunk / remux / proxy / transcript / report
  path, locking its own VOD id so it runs concurrently with a live recording.
- Stream-copied Premiere-ready masters, plus hardware-encoded proxies named for Premiere's
  **Attach Proxies**.
- Mid-recording snapshots: pull any range out of a broadcast without waiting for the chunk
  to close.
- A channel watcher that starts recording on its own when a channel goes live.
- Optional HTTP/SOCKS proxy for every Twitch request, for recording from a region Twitch
  has withdrawn from.

### Transcription and exports

- Rolling Deepgram transcription that keeps up with the recording, with overlapping slices
  and a third transcription across each chunk boundary so no word is lost at a seam.
- Premiere Static Transcript JSON with per-word timings — the format that enables
  text-based editing — validated against Adobe's own published schema.
- SRT captions, a plain-text transcript, a censor list drawn from your own master word
  list, and the verbatim Deepgram responses archived beside the normalised word stream.
- An editor report per chunk (`report.md`) via `claude -p` or `grok -p`, backed by
  captured Twitch chat for evidence of what actually landed.

### Chat

- Live chat over IRC and VOD chat over Twitch's persisted comments API, with per-chunk
  moment detection. A chat failure never fails a recording.

### Reliability

- Masters are verified by reading every packet before the source `.ts` is discarded —
  both silent-corruption modes seen in testing are caught.
- Failed remuxes and failed report generations are retried under a deadline.
- Per-artifact error state, so one failure cannot erase another's success.
- Transactional config validation: a bad save cannot leave the app unable to start.
- Retirement machinery for config keys, manifest fields and published files, so removing
  a feature never bricks an existing install.

### Interface

- Local dashboard (loopback-only, `Origin`/`Host` checked) and a compiled Windows host
  with Start Menu registration and an uninstall entry.
- Full CLI: `doctor`, `record`, `vod`, `snapshot`, `transcribe`, `republish`, `sessions`,
  `dashboard`, `app`, `install`.

[1.1.0]: https://github.com/MrBeldum/twitch-vod-pipeline/releases/tag/v1.1.0
[1.0.2]: https://github.com/MrBeldum/twitch-vod-pipeline/releases/tag/v1.0.2
[1.0.0]: https://github.com/MrBeldum/twitch-vod-pipeline/releases/tag/v1.0.0
