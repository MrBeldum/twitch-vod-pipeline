# Twitch VOD → Premiere Pipeline

Records Twitch streams live in configurable chunks (default 2h) — or downloads a past VOD
through the identical pipeline — writes Premiere-ready masters + proxies, and after each
chunk emits an objective `.md` rundown plus a Premiere-importable transcript for text-based
editing, SRT captions, a censor list, and — one level down in `source/` — the Deepgram word
stream everything derives from and the provider's verbatim responses beside it. Also supports pulling a segment out mid-stream
without waiting for the chunk to finish, and routing all Twitch traffic through an
HTTP/SOCKS proxy.

**It records and transcribes; it does not cut.** The automatic edited cut was built and
then retracted on 2026-08-17 by a deliberate product decision — see the note below. It now lives in
a separate, unpublished repository: this same pipeline plus the cut and the model that
decides it. The two were split on 2026-08-18 because they are meant for different jobs,
not because either is a draft of the other: this one hands over footage to cut by hand,
that one makes a first pass at the cut. **This repository is the maintained one**, and it
is the whole pipeline minus the cut.

Status: **built, audited, hardened, and proven against real 8-hour recordings** -- three
live tests, each of which found defects no fixture could have. Two audits
were worked through (neither document is retained in this tree; `tests/test_audit_20260814.py`
and `test_confirmed_audit_fixes.py` are what they left behind); every finding was verified
and closed or confirmed already-handled. The full suite is
`python -m unittest discover -s tests -t .`. See `README.md` for operation and rationale.

**First live test, 2026-08-16 (examplechannel, 4 chunks, ~7 hours).** Capture, chunking, remux
and masters were flawless. Four defects surfaced that no fixture could have caught, all
fixed and locked in by `tests/test_live_failures_20260816.py` plus additions to
`test_media_asr_coverage_contracts.py` and `test_confirmed_audit_fixes.py`. Each is written
up in the notes below under a **CORRECTED 2026-08-16** heading; in short, three of the four
were *validators that were stricter than reality*, which is the failure mode to watch for
in this codebase:

| Symptom in the log | Root cause | Cost |
|---|---|---|
| `deepgram word 'x' ends beyond the response audio duration` | word end times are estimates and `metadata.duration` is rounded to the second | roughly half of all rolling passes failed |
| `transcript is NOT complete (7105s of 7201s)` | 1 ms / 34 ms measurement noise treated as a short read | every chunk lost its last 68–96 s, and every rundown with it |
| `needs about 319.3 GB above the 10.0 GB reserve` | proxy reservation bounded video at *uncompressed* size | every proxy refused; the one that ran was 538 MB |
| `rundown failed: resource is owned by another process` | the proxy encode held the chunk transcript-mutation lock for its whole run | the only complete transcript lost its rundown |

**Second live test, same day (otherchannel, and the examplechannel backlog).** All four examplechannel
chunks reached complete masters, proxies and transcripts. Three further defects, all
fixed and covered by `tests/test_live_failures_20260816.py`:

| Symptom | Root cause | Cost |
|---|---|---|
| `ffmpeg exited 4294967274 … Error muxing a packet` ×3 in 15 min | `-map 0` captured Twitch's `timed_id3`, whose DTS is not discontinuity-corrected | every network stall killed the recording |
| `claude -p failed (1):` — nothing after the colon | detail read stderr only; a `--print` CLI explains itself on stdout | the one diagnostic message carried no information |
| a rundown lost to one transient engine failure | the CLI transport had no retry while the API transport had four | c000's rundown was permanently `error` |

**Third live test, 2026-08-18 (examplechannel ~7h, then otherchannel ~7h, 8 chunks).** Capture and
transcription were flawless across both sessions -- 8 chunks, ~110,000 words, every seam
stitched. Three defects, all in what happens to a chunk *after* it closes, and every one
of them reported as something it was not. Fixed and locked in by
`tests/test_live_failures_20260818.py`:

| Symptom in the log | Root cause | Cost |
|---|---|---|
| `remux failed: Assertion next_dts <= 0x7fffffff failed at movenc.c:1236` | one sample's duration overflowed a 32-bit field, i.e. a DTS gap of >6h inside a 2h file; **not reproducible** -- re-running the identical command over the identical bytes now produces a perfect master | c002 kept only a `.ts`; its proxy then failed with "master is missing" |
| `h264_amf failed on real media (covers 2765s but expected 7202s)` ×2 chunks ×2 encoders | the *master* was damaged, not the encoder: one `stsc` entry misaddressed every sample after it, one `co64` entry had a stray high bit and the demuxer stopped dead at 44% | 2 masters silently corrupt, their `.ts` already deleted; 20 minutes spent proving libx264 could not read them either |
| `claude -p failed (1): You've hit your session limit · resets 6:10am` ×3 | the only engines on offer were a Claude Code subscription and an Anthropic key | c000's rundown lost; nothing to fall back to. The pluggable engine this produced was itself withdrawn on 2026-08-19 -- see the engine note below |

**The corruption itself is unexplained and precisely characterised.** Both bad masters
carry byte-level damage in the `moov`, in different tables, hours apart, from a process
whose input was provably clean (streamlink logged zero sequence gaps and zero ad breaks
across the whole recording; the surviving `.ts` re-remuxes perfectly today). otherchannel c001's
`co64[136075]` reads `2**45 + 3242408537` -- a **single flipped bit**. otherchannel c000's
`stsc[47056].first_chunk` reads `8192` where `59839` belongs. Five other masters from the
same two sessions are perfect. No WHEA, disk or NTFS errors are logged, which is expected:
non-ECC memory does not report a flip. **Do not go looking for a logic bug in the muxer to
explain this** -- a logic bug is systematic, and this is one wrong value in 310,000. If it
recurs, run a memory test before reading any more code. What the pipeline can do about it
is detect it, which is what it now does.

**Fourth live test, 2026-09-02 (examplechannel, 5 chunks, ~8.5 hours).** The capture half was
perfect and the delivery half produced nothing: five masters, five proxies, five complete
transcripts (~62,000 words, every seam stitched), chat for all five -- and **zero
reports**, fourteen attempts, every one of them `grok -p failed (1): Max turns reached`.
Both defects are in `tests/test_live_failures_20260902.py`:

| Symptom in the log | Root cause | Cost |
|---|---|---|
| `grok -p failed (1): Max turns reached` ×14 | `grok -p` offloads a prompt over ~24 KB to a file the model must `read_file`; `--max-turns 1` spent the only turn on the read | every report of every chunk, for the whole recording |
| two reports queued per chunk, eight seconds apart | a boundary stitch re-queued through `_queue_summary`, bypassing the chat gate `_maybe_queue_summary` applies | a doubled engine bill, and a chat-less report whenever the second attempt lost |

**The lesson is the one this codebase keeps relearning from the other side.** The
standing failure mode here has been *a validator stricter than reality* -- three of the
four 2026-08-16 defects were that. This is its mirror: **a caller whose model of the tool
was simpler than the tool.** `--max-turns 1`, `--tools ""` and `--output-format plain`
all describe a completion endpoint, and `grok -p` is an agent runtime; each setting was
not merely suboptimal but actively wrong, and the first of them made success impossible
rather than unlikely. Nothing in a fixture would have caught it, because the fixture
would have been written against the same wrong model. What catches it is asking the tool
what it actually does -- the tool list came from a `--output-format streaming-json`
handshake, and the offload came from probing where a prompt stops arriving intact.

**Editing the output in Premiere, 2026-08-17.** A finished chunk was imported into
Premiere, and one feature did not work: *delete all fillers* reported nothing to delete
against a transcript carrying 397 valid `filler` tags. Filler tagging was **cancelled**
rather than debugged further; see the `tags` note below. `IMPORT.md` was dropped in
the same pass as bloat. Together this removed two modules (`fillers.py`, `review.py`,
~1,400 lines), a job type, seven config keys and three published files per chunk.

**The edited cut: built, then retracted, 2026-08-17.** The pipeline was extended to do
the editing itself, and then — having seen it work — the feature was taken back out so the
tool would stay the recorder and transcriber it started as. Both decisions were right and
neither cancels the other:

- it *worked*. On the reference chunk it took 58:52 to 46:15, clipped none of its 8,022
  words, and its 478 joins measured quieter than ordinary speech (median single-sample
  step 3 of ±32,768 against 1,188). The verification is what made the decision possible;
- and it was still the wrong deliverable. An automatic cut has to be checked, and checking
  a 46-minute file means watching it. What it saved — finding the dead air — is not the
  part of editing that is slow, and it cost forty minutes of encoding and several GB per
  chunk for a derivative one generation removed from a stream copy.

The work is not lost — `edit.py`, `audio.py`, `render.py` and the model-decided
`aiedit.py` were kept, with their tests and their measurements, in the separate editor
repository. **This repository is the core pipeline**, and the notes below are written
for it.

**What the retraction had to get right** is the part worth reading before removing
anything else from this codebase. Two readers here are deliberately strict, and both would
have turned "delete the feature" into "the application will not start on the machine that
had it":

- `schema._walk` raises on an unknown config path. The `edit.*` keys are present in any
  `config.json` installed while the feature existed. Retiring the **section** (`"edit"` in `RETIRED_PATHS`) rather
  than its thirty keys individually is what works — `_walk` only recurses into a branch
  that still has a rule underneath it, so once the last `edit.*` rule went the *container*
  became unknown and retiring the keys one by one did not help. Retiring the section also
  covers the model branch's keys, which this build never knew.
- `state._known_fields` raises on an unknown manifest field, and `edit_status`/
  `edit_error`/`edit_name` are in every `session.json` written while the feature existed.
  `_RETIRED_CHUNK_FIELDS` keeps them *accepted* and drops them on load, so they validate
  today and are gone from the file after the next save. `errors` needed the same treatment
  via `_RETIRED_ERRORS`.

Neither had a test before; `tests/test_retirement.py` is that test, and it is the one to
extend the next time something is removed.

**Also added in the same pass: the verbatim Deepgram archive**
(`transcription.keep_raw_responses`, default on, `transcripts/<chunk>/deepgram/NNNN.json`).
`words.json` is the *normalised* stream — sorted, de-overlapped, same-start collisions
resolved by dropping one of the pair — which is what every export derives from and is
deliberately not what the provider said. The archive answers the question the normalised
file cannot: *did we lose that word, or was it never there?* It hangs off an `on_response`
hook on `DeepgramProvider` and a thread-local destination on the transcriber (one provider
instance is shared across the job pool, so a plain attribute would misfile responses).
**Archiving can never fail a transcription that succeeded** — the hook is wrapped, logged
and swallowed, because an archive is a convenience and a transcript is not.

**Two features were added 2026-08-15 (see the notes below and README):** downloading past
Twitch VODs through the same chunk/remux/proxy/transcript/rundown pipeline as live, and a
configurable network proxy for reaching Twitch from a region it has left (South Korea).

Runtime transcription requires a Deepgram API key; the local key is configured and
`vodpipe doctor` reports `Ready` as of 2026-08-15. A Twitch OAuth token is optional and
enables the ad-free path.

- **Chat is captured for live and VOD, and feeds the editor report.** Live chat is
  IRC over TLS (`irc.chat.twitch.tv:6697`), anonymous `justinfan` or the configured
  OAuth login. VOD chat uses Twitch's persisted GraphQL `VideoCommentsByOffsetOrCursor`
  (the same comments API TwitchDownloader uses; Client-ID
  `kd1unb4b3q4t58fwlpcbzcbnm76a8fp`). Offsets are shifted so t=0 is our media start.
  Per-chunk `source/chat.json` + `moments.json` (laugh emotes, copypasta, clip-calls —
  not messages/second) go into the report prompt. A chat failure never fails a recording.

- **IRCv3 tag escapes are decoded in one left-to-right pass, never by chained
  `str.replace`.** *CORRECTED 2026-08-25 (audit).* The decoder ran five
  sequential replaces with a comment asserting that handling the
  escaped-backslash pair last was the safe order. It is not safe in either
  order. Given an escaped backslash followed by a literal `s` -- three
  characters: backslash, backslash, s -- decoding that pair last lets the
  *second* backslash pair with the `s` first, so it reads as an escaped space
  and yields backslash-space; decoding it first leaves a backslash that is then
  re-read as the opening of another escape. Only a scanner that consumes both
  characters of an escape at once is correct. Reachable through `system-msg`,
  the escaped user-facing text a USERNOTICE with no trailing payload falls back
  to for its body, and `parse_tags` runs every tag through it. Six cases in
  `tests/test_chat.py::TagEscapeTests`, all of which fail against the old
  decoder.

- **The chat-moment window scan is a two-pointer sweep, not a rescan.**
  *CORRECTED 2026-08-25 (audit).* `analyse_chat` re-filtered every message for
  every window: a two-hour chunk at a 2 s hop is 3,600 windows, so a busy
  channel paid ~19 s of pure Python per chunk and the cost grew with chat
  volume. Windows advance monotonically, so the members of one are a contiguous
  slice of an offset-sorted index -- 1.2 s for the same 60,000 messages.
  **The slice is re-sorted back into original order before scoring, and that is
  load-bearing:** `_score_window` is order-sensitive (`_samples` dedupes on
  first occurrence and sorts stably, and the copypasta tally breaks ties on
  insertion order), so visiting in offset order would quietly change which
  sample lines reach the report. The contract is not that output is independent
  of input order -- it never was -- but that the sweep returns exactly what the
  rescan returned for the same input. `tests/test_moments.py::WindowSweepTests`
  keeps the old implementation as the reference and diffs the two.

- **Recovery probes the lock the recorder actually holds.** A VOD download locks
  `vod-{id}`, not the broadcaster's channel. Recovery used to probe the channel for
  both, so a second process could remux and delete a `.ts` the VOD downloader was
  still writing. `_session_lock_key` is the single source of that identity.

- **VODs reuse the live path.** `Pipeline.download_vod(url)` resolves the broadcaster and
  title with `streamlink --json`, then runs a `Recorder` in `source_kind="vod"` mode:
  `media.vod_download_command()` pipes the VOD to the *same* ffmpeg segmenter as live, so
  the chunk/remux/proxy/rolling-ASR/rundown/snapshot machinery is untouched. A VOD locks
  its own id (`vod-<id>`), not the channel, so a download and a live recording of the same
  channel — or two different VODs — run concurrently. Session provenance is recorded in
  `Session.source_kind`/`source_url` (both in the strict manifest schema). VOD downloaders
  live in `Pipeline._vod_recorders` (keyed by lock id), kept entirely apart from the
  live-channel `_recorders`/arming/watcher machinery. CLI: `vodpipe vod <url|id>
  [--start] [--duration]`; dashboard: the "Download a VOD" panel.
- **The network proxy is `network.proxy`** (a config key; empty = direct). It becomes
  streamlink `--http-proxy`, which in 8.4 covers every HTTP/HTTPS request, so one setting
  routes live capture, VOD download, and the live-status probe. Built via
  `media.proxy_args()`; validated in `schema.py` (`_proxy_url`, closed scheme set). This is
  the light-weight alternative to a full VPN for the Korea geo-block; see README.

Implementation notes worth knowing before changing anything:

- Chunks are recorded as **MPEG-TS and remuxed to MP4 on close**. An MP4 has no index
  until it is closed, so a live MP4 could not be transcribed or snapshotted. This one
  decision is what makes rolling transcription and mid-chunk snapshots possible.
- **Capture maps video and audio only — never `-map 0`.** *CORRECTED 2026-08-16.*
  Twitch's HLS carries a `data:timed_id3` stream and `-map 0` copied it into every
  recording. When the network stalls, streamlink logs `Sequence gap of N segments … will
  result in incoherent output data` and the timestamps jump; the mpegts demuxer corrects
  the audio for that (`timestamp discontinuity … new offset=`) but the id3 stream gets no
  such correction, so its DTS runs backwards, the segment muxer refuses it —
  `Application provided invalid, non monotonically increasing dts to muxer in stream 2` —
  and ffmpeg dies with -22, ending the recording. A live otherchannel capture died this way
  three times in fifteen minutes. The stream was never wanted: `plan_remux_maps` already
  dropped it at remux, and settled decision (the 2026-08-13 ad correction) rules out
  reading Twitch metadata anyway. Verified by A/B capture against the live channel:
  `-map 0` yields `0 aac / 1 h264 / 2 timed_id3`, the corrected command yields video and
  audio only. `+discardcorrupt` covers the other half of the same event
  (`Packet corrupt (stream = 1)` in the same log) — dropping a corrupt packet costs a
  frame, refusing it costs the broadcast.
- **The rundown engine is `claude -p`, and it is the only one.** *2026-08-19, at the
  user's request.* For exactly one day this was pluggable across eight names -- added
  2026-08-18 after `claude -p` hit its session limit mid-recording with nothing to fall
  back to, removed the next day after the paid APIs failed on the ordinary case twice in
  one night: Kimi refused one rundown for exceeding an organization concurrency of
  **one** (against a pipeline whose whole shape is background jobs) and refused the next
  as `high risk` content, having been handed a Twitch transcript. **A fallback that fails
  on the ordinary case is not a fallback**, and what the fallback was insuring against
  costs one rundown that the dashboard can re-queue against the stored transcript once
  the limit resets. Do not re-add a paid API without reading this paragraph; it is the
  thing that was tried. Four things the removal had to get right:
  - **`summary.provider` is a retired *value*, not a retired path,** so `RETIRED_PATHS`
    does not reach it. An installed `config.json` naming a removed engine is rewritten to
    `claude-cli` by `schema._summary_provider`/`RETIRED_PROVIDERS` rather than refused --
    an installed file could name `kimi-api` from the day before, and refusing it would be
    exactly the "will not start on the machine that had it" failure retirement exists to
    prevent.
    Rewriting is only safe because the engines were interchangeable: same prompt, same
    kind of answer. Do not copy this for a setting whose values mean different outcomes.
  - **`summary.base_url`, `summary.cli_command` and the five API keys are retired keys,**
    one by one rather than by section -- unlike `edit`. `summary` and `secrets` both
    still carry live rules, so `_walk` goes on recursing and neither container becomes
    unknown; retiring the `secrets` section would have taken `deepgram_api_key` with it.
    Retiring the keys is also what erases the operator's Kimi key from the file.
  - **`summary.model` now means "leave it blank".** `claude -p` knows which models the
    subscription covers and this repository does not. It is kept as a setting only
    because naming one is occasionally useful.
  - **`PROVIDER_SECRETS` is gone, and with it the idea that capability means a key.**
    `_summary_capability` is one question again -- is the `claude` executable there --
    read by the dashboard, recovery, the API and the job, which must not disagree.
  What survived is the retry below, which is the part that was earning its keep.
  **`grok-cli` is a second subscription CLI, added later.** Blank `summary.model`
  means the Grok CLI default, which as of CLI 1.0.13 is **Grok 4.6** (usage id
  `grok-4.6-build`). The old TUI alias `grok-build` is an unknown model id and is
  rewritten to blank by `schema.RETIRED_MODELS`. Do not pass `-m grok-build`.
- **`grok -p` is an agent and is asked as one; `claude -p` is a completion and is
  asked as one. Do not make the two argvs look alike.** *CORRECTED 2026-09-02 --
  the whole reason that recording produced no reports.* Grok's CLI **offloads**
  any `--prompt-file` over roughly 24 KB: the prompt is not in the conversation,
  the model gets a stub (`Full request offloaded to file`) and has to `read_file`
  its way to the request. Every real transcript is ~104 KB, so with `--max-turns 1`
  the one turn went on the read and the run was cancelled before an answer existed.
  Certain, not intermittent. Three further faults sat behind it, each of which
  would have spoiled a run that got past the first:
  - **`--tools ""` restricts nothing for Grok.** An empty allowlist reads as
    "unset", so all 26 built-ins stayed live -- `run_terminal_command`,
    `spawn_subagent`, `image_gen` -- plus **297** Premiere Pro and After Effects
    MCP tools adopted from the Claude Code configuration on this machine. The
    allowlist must be non-empty, and the `GROK_CLAUDE_*_ENABLED` env switches are
    what keep another application's MCP servers out. **`claude --tools ""` is not
    the same and is correct as it stands** -- it answers `NO TOOLS`, verified --
    so do not "fix" the Claude argv by analogy with this one.
  - **`--output-format plain` prints every assistant message.** stdout is the
    model's narration run together with the answer, so a *successful* run under
    the old argv would have published "I'll start by reading..." as the opening of
    the report. `json` carries the same text plus `stopReason`, which is what lets
    a max-turns exit be named instead of reported as a bare non-zero exit.
  - **"Write `report.md`" was prose in a shared prompt and a task to an agent.**
    It now lives in `GrokCliModel.DELIVERY`, where it is true, and
    `summarize.INSTRUCTION` stays transport-neutral.
  So the transport hands Grok a throwaway directory holding `transcript.md`, an
  instruction small enough to arrive inline, four tools (`read_file`, `list_dir`,
  `grep`, `write`) and `summary.max_turns` (default 40); the model writes
  `report.md` and the pipeline reads that file back. The reference c000 transcript
  -- 14,431 words, the one that failed fourteen times -- returns a complete
  seven-section report in eight turns and about four minutes. A report already on
  disk is kept even when the CLI then exits non-zero: the expensive part is done,
  and discarding it over a missing "DONE" would throw the whole call away.
- **Every icon is generated from `docs/logo.png`; none of them is edited by hand.**
  *2026-09-02, after replacing the logo changed the README and nothing else.*
  `packaging/prebuild.py` writes `vodpipe.ico`, `icon-16/32/48/256.png`,
  `static/favicon.ico` and `static/icon.png` from that one file, and records the
  source's sha256 in `packaging/icons.stamp` so a test can say "the icons are
  stale" exactly. It also writes `version.g.cs` from `vodpipe.__version__`,
  which used to be a literal `1.0.0` in `host.cs` and stayed that way through
  1.0.1. Both run before every compile, from `vodpipe install` and from
  `build.cmd`. **Four separate faults were in the way of a new icon and not one
  of them said anything** -- they are worth knowing because each is a build
  producing the wrong artifact in silence:
  - the icons were six hand-exported files with no declared source, so the
    `.ico` Windows actually shows was simply older artwork;
  - `ensure_host` compared the exe against `host.cs` alone, so a changed icon
    never triggered a rebuild. `build_inputs()` is now the list, and the icon is
    in it because `/win32icon` makes it a compile input;
  - `build_host` passed csc `/win32icon=...`, which the .NET Framework compiler
    rejects with `fatal error CS2007`, so **`vodpipe install` could not compile
    the host at all**. The exe in the tree had been built by `build.cmd`, which
    used a colon and was right; because `ensure_host` never asked for a rebuild,
    the broken path was never exercised. Two build routes that differ is the
    real defect; they now agree;
  - and `Install()` did not `SHChangeNotify`, so the shell kept drawing the
    cached icon for a path whose exe had changed underneath it.
  `static/icon.svg` was a seventh, hand-drawn mark that matched neither the
  logo nor the `.ico`; it is retired, and the dashboard uses the generated PNG
  and ICO. `tests/test_packaging.py` covers all of this.
- **The desktop window is Chromium or Chrome, never Edge.** `find_app_browser`
  searches a bundled `vendor/chromium`, then Chromium, then Google Chrome.
  `msedge.exe` is not a candidate. The compiled Windows host is `VODPipeline.exe`
  (`packaging/host.cs`, built by `vodpipe install`): AppUserModelID
  `MrBeldum.VODPipeline`, Start Menu shortcut, Apps & Features uninstall key.
  Closing the Chromium window still shuts the pipeline down.
- **The dashboard's design system is `docs/DESIGN.md`, and its palette is tested.**
  Tokens, spacing, component contracts and the accepted-debt list live there;
  read it before restyling anything. A label on a filled button must use the
  matching `--on-*` token rather than a literal `#fff`:
  `--on-live: #ffffff` on `--live: #ff6363` measures **2.91:1**, which fails
  even the 3:1 non-text floor, and it shipped on Record, Stop and the pulsing
  REC stamp -- the three loudest controls in the app. The retired violet
  palette had passed at 4.75:1, so the restyle regressed it silently; nothing
  in a screenshot says "2.91". `tests/test_ui_contract.py::PaletteContrastTests`
  now computes the ratios straight out of `style.css`, so a palette re-picked by
  eye fails the suite instead of shipping. Two known gaps are recorded as
  accepted debt in `docs/DESIGN.md` rather than quietly fixed: there is no focus trap
  on the drawer (it claimed one until the 2026-08-25 audit), and `--line`
  hairlines sit near 1.3:1, so an unfocused input is delimited by little more
  than its fill.
- **A rundown engine gets more than one attempt** (`summary.max_retries`, default 3,
  bounded by `summary.timeout_seconds` overall). *Added 2026-08-16.* An API transport that
  briefly lived next door retried and the `claude -p` transport did not, which was
  backwards: the CLI is the only provider and the one sharing an interactive subscription
  quota, the transient this project has always expected. One blip lost a rundown permanently. A non-zero CLI exit is
  not classifiable from outside — a rate limit, a dropped connection and a bad flag look
  identical — so the retry is unconditional and bounded by the deadline the single attempt
  already had. Failure detail now falls back to stdout, because a `--print` CLI writes its
  reason there and the old stderr-only message was a bare `claude -p failed (1):` with
  nothing after the colon.
- The pipeline is **stdlib-only Python**. Deepgram is called over
  `urllib`; the dashboard is `http.server`; the rundown engine is a subprocess. Nothing
  to pip install, which sidesteps the Python 3.14 wheel gaps flagged below.
- `h264_amf` was verified working on this machine and is used for proxies, with a
  runtime probe and a `libx264` fallback.
- No `.epr` proxy ingest preset is generated — Adobe's format is opaque binary. Proxies
  instead use Adobe's own `<name>_Proxy.mp4` in `Proxies/` naming so Premiere's built-in
  **Attach Proxies** matches them. Documented in README.
- **ffmpeg's input-side `-ss` is file-relative, already measured from the start of the
  file.** An earlier version added the container's `start_time` on top, which on a
  typical MPEG-TS (start ≈1.4s) skipped the first 1.4s of every audio slice and truncated
  tail requests. Verified empirically; `tests/test_media_timeline.py` uses a fixture with
  a deliberately large nonzero PTS. Do not reintroduce that offset.
- **A master is read end to end before its `.ts` is deleted, and only then.**
  *2026-08-18.* `validate_master` reads the container header -- stream inventory,
  dimensions, declared durations -- and both corrupt masters of that day satisfied it
  perfectly, so the recording was deleted in favour of a file that could not be played
  past 44% of its length. `verify_master_readable` walks the packets instead, and applies
  **two rules, both required, because each of the two files passed the other**: it must
  read without ffprobe reporting anything (the misaddressed-samples file delivers the full
  packet count at the right timestamps, pointing at rubbish), and its video packets must
  span the duration the file itself declares (the stray-high-bit file reads in perfect
  silence, because as far as the demuxer is concerned the index simply ends). The span is
  compared against the *stream's own* declared duration rather than the recorder's
  measurement of the chunk -- one question, one frame of reference, clear of the
  measurement noise `SHORT_READ_TOLERANCE` exists to absorb.
  **It runs exactly where a `.ts` is about to be discarded** (`_verify_before_reclaim`),
  not on every master recovery walks past: it costs one full pass, 3-14s for a two-hour
  chunk on this machine against a 30-40s remux, and re-reading fifty gigabytes of finished
  masters at every start to learn nothing would turn startup into a disk scrub.
- **A failed remux is tried again** (`recording.remux_attempts`, default 3). The
  precedent is the `claude -p` retry: a failure that cannot be classified from out here is
  still worth repeating when repeating it is bounded and the alternative is losing the
  artifact permanently. The proof it was worth adding is that examplechannel c002's assertion
  does not reproduce -- the same command over the same bytes succeeds now, so a second
  attempt would have saved the master on the night. Each attempt stages its own
  `.partial.mp4`, so nothing is carried between them.
- **A short proxy accuses the master before it accuses the encoder.** The encoder
  fallback (`auto` -> libx264) exists because a hardware encoder can be defeated by real
  media, but an encoder cannot encode frames its input will not hand over. `_master_damage`
  reads the source through on the failure path only, and a damaged master is named as such
  instead of costing another five-minute software encode that fails identically.
- **Chunk state is per-artifact** (`master_error`, `proxy_error`, `transcript_error`,
  `summary_error`). A single shared field let a successful remux erase a transcription
  failure.
- **A transcript is complete only when covered ≥ expected duration.** `advance()` returns
  a structured `AdvanceResult`; `finalize()` loops it with a bound and a no-progress
  check. Never mark done from a single pass.
- **"Expected" means the audio the file actually holds, and comparisons carry a
  measurement tolerance.** *CORRECTED 2026-08-16.* `SHORT_READ_TOLERANCE` was
  `COVERAGE_EPSILON`, i.e. one millisecond, and no real media meets that: an audio frame
  is a quantum (21.3 ms for AAC-LC at 48 kHz, and both the seek and the cut can land on a
  boundary), `-ss`/`-t` reach ffmpeg rounded to the millisecond, and a container's duration
  is its *longest* stream — the video — not the audio track being transcribed. So the
  recorder's 7200.901 s and ffprobe's 7200.867 s are two honest measurements of the same
  chunk. Every chunk of the first live recording was declared incomplete over 1 ms and
  34 ms, which also skipped its rundown and left 68–96 s untranscribed. The tolerance is
  now 0.15 s — several times the physical worst case, well under a spoken word (~0.3 s) —
  and when the shortfall is *within* tolerance `advance()` lowers `expected` to the
  measured figure. Both halves are required: loosening the check alone leaves completion
  arithmetically unreachable and the chunk fails later at `_finish` instead.
  **The tolerance applies to the completion tests too, not just the entry tests.** The
  first attempt at this fix changed only the entry tests; the missing tail then
  transcribed correctly and the chunk failed one step later on `audio read made no
  progress`, because ffmpeg cannot emit a partial audio frame — c000's last extractable
  sample is at 7200.842 s in a container advertising 7200.867 s, and no further pass can
  ever close a gap the decoder will not produce. The rule to apply when touching any of
  this: **`COVERAGE_EPSILON` compares two numbers of the same kind (two cursors, two
  extraction measurements); `SHORT_READ_TOLERANCE` compares a probe-derived duration
  against an extraction-derived cursor.** Every cross-kind comparison needs the tolerance.
- **Deepgram word *end* times are estimates; only *starts* are trusted.** *CORRECTED
  2026-08-16 — do not reinstate a hard end-time bound.* Two separate assumptions were
  wrong. `metadata.duration` is coarse: nova-3 reported it as a whole number of seconds for
  most slices (49.0, 63.0, 77.0, 93.0 …) while the slice was really 63.9 s. And the last
  word of a passage routinely ends after the audio does, by anything from 15 ms to about a
  second. Rejecting on either failed roughly half the rolling passes in one recording. `parse_deepgram`
  now bounds word *starts* — a word beginning after the audio ended describes a different,
  longer recording, which is the actual corruption signal and is still fatal — and clamps
  overrunning ends to the audio. Our own ffprobe measurement of the uploaded file outranks
  the response's self-report; `metadata.duration` is only consulted when the caller passed
  no duration at all.
- **Deepgram word spans may overlap.** `parse_deepgram()` requires valid, start-ordered
  spans and then calls `normalise()` to trim ordinary overlap. Persisted millisecond
  timestamps use a `1e-6` comparison tolerance because adjacent decimal values can differ
  by ~`1e-12` as binary floats. Exact overlap rejection corrupted four valid transcripts
  on 2026-08-15; do not restore it.
- **Channel names are validated in one place** (`channels.parse_channel`) because they
  become directory names, filename prefixes and deletion globs.
- **Config is validated transactionally** against `schema.py` before it replaces the live
  config, so a bad dashboard save cannot leave the app unable to start.
- **Publishing a transcript replaces the whole export set.** `write_exports()` removes any
  file in `PUBLISHED_EXPORTS` it did not produce this time. It is only ever called on a
  *successful* pass, which is what makes the asymmetry safe: a failure leaves the last
  good outputs alone, and `retranscribe` stashes the words file and restores + republishes
  it if the rebuild fails.
- **Chunk boundaries are stitched with a third transcription.** Overlapping slices cannot
  fix a word spoken across a *chunk* boundary — the two chunks are separate files. A short
  passage built from both is transcribed and each word goes to whichever chunk it was
  mostly spoken in (midpoint rule), so a straddler lands in exactly one transcript, whole.
  Only the join itself is replaced unconditionally; further in, a word the seam pass did
  not cover survives. An empty seam pass changes nothing. This is what makes it idempotent
  and safe to re-run from recovery.
- **Three job pools, not one queue.** `jobs` is capture-critical (rolling ASR, chunk
  finalisation, remux), `media_jobs` is heavy and disposable (proxies, rundowns),
  `snapshot_jobs` is user-initiated. A single FIFO let a 15-minute `claude -p` call block
  rolling transcription for a live channel. Anything reading job state must go through
  `pipeline.job_snapshot()` / `active_jobs()`, never `pipeline.jobs` alone.
- **Manual refresh is POST `/api/refresh`; the 2s poll is GET `/api/state`.** Refresh
  queues a forced live probe (the watcher interval is 60s by default, so the dashboard
  can be stale on "is this channel live?" even while it polls). The poll must stay a
  cheap read — folding the two together would hammer streamlink every two seconds.
  F5/Ctrl+R in the Chromium app window are intercepted so they refresh data instead of
  reloading the page.
- **The chunk mutation lock is the *transcript generation* lock, and is only ever held
  briefly.** `_summarize_inner` and `retranscribe` are both written
  around that: they do their expensive work unlocked and take the lock only for a
  generation recheck plus an atomic commit, waiting at most 60 s. *CORRECTED 2026-08-16:*
  the proxy encode did not play by those rules — it inherited finalisation ownership (or
  took the lock itself through a `_run_chunk_mutation` wrapper) and held it for the ten
  minutes a 2-hour encode runs, so the rundown for the first chunk with a complete
  transcript timed out and failed with `resource is owned by another process`. Note that
  message is misleading: Windows byte-range locks are per *handle*, so a second descriptor
  in this same process conflicts exactly as a peer would. A proxy derives from a finished
  master and mutates no transcript, so `_make_proxy_inner` now locks its own output path
  via `media_lock_path` instead — which is the only conflict it really has, two encoders
  staging the same `.partial.mp4`. **Nothing that runs for minutes may hold a chunk
  mutation lock.**
- **A snapshot takes a read lease on the media it opens.** `_reclaim_ts()` defers deletion
  while a lease is held and completes it on release, so a cut cannot be pulled out from
  under by the remux that reclaims the `.ts`.
- **An unrecognised language is never relabelled `en-us`.** `premiere_language()` expands a
  bare code to its usual region, resolves an unlisted regional variant onto the same
  language, and emits Adobe's own `??-??` for a language Premiere cannot do at all.
  Silently claiming English for non-English speech was worse than an unfamiliar tag.
  **Corrected 2026-08-14:** it used to pass any well-formed tag through unchanged, which
  was wrong — Adobe's language list is a *closed enum* and the schema is
  `additionalProperties: false`, so `zh-cn`/`fi-fi`/`uk-ua`/`en-au` were not unfamiliar
  tags but invalid files.
- **The edited cut's implementation notes moved out with it.** What was learned building
  it — the acoustics-propose/transcript-vetoes rule, the outward-only snap, deriving audio
  sample counts from video frame counts, clamping the plan to the frames that exist, the
  balanced `select` tree, the disk estimator anchored on the master's own size — is not
  reproduced here, because none of it is reachable from this codebase. Two of its lessons
  stayed because they are not about editing:
  - **`words_json_text` validates its own output before returning it.** The remap that
    fed the edited transcript produced words that stepped backwards by one video frame,
    and `write_exports` wrote a `words.json` that `load_words` then refused. A writer and
    a reader that disagree produce a file nothing downstream can use, and the damage is
    silent at the point it happens.
  - **Check any new disk estimator against a real file before believing it.** This
    codebase has modelled a reservation wrongly twice — 319 GB for a 538 MB proxy, then
    82 GB for a 13 GB edit. A reservation the drive cannot meet does not protect the
    disk, it turns the feature off.
- **`profanity` is the only `tags` value we emit. Filler tagging is removed — do not
  reinstate it.** *2026-08-17, confirmed against a real Premiere install.* Adobe's enum
  allows `profanity` and `filler`; we wrote both, and Premiere reported **"no filler words
  detected"**
  against a `premiere.json` carrying 397 `filler` tags on a valid, importable, correctly
  attached transcript. The tag is documented and schema-legal, so the reason it is ignored
  is inside Premiere and not observable from here. The decision was to cancel the feature
  rather than keep debugging it, and independently: *delete all fillers* cuts on
  word boundaries, which is audible on a hesitation running into the next word, and one
  wrong verdict in a thousand deletes real speech with no review step. `fillers.py`
  (a tagger with `sounds`/`smart`/`aggressive` modes) and `review.py` (an optional
  model pass over ambiguous cases) were deleted, along with seven config keys, the
  `fillers` job type, `_queue_filler_review`/`_review_fillers`, and `fillers.md`/
  `fillers.json`. `transcription.filler_words` **stays**: it is the Deepgram request
  parameter, and a verbatim transcript is what makes a cut land where the editor expects.
  It is also a required field of the strictly validated `asr_identity`, so removing it
  would invalidate every existing `words.json`.
- **Retiring a config key needs `schema.RETIRED_PATHS`, not just a `SCHEMA` deletion.**
  `_walk` raises on any unknown path, so deleting a key that is present in an already
  installed `config.json` makes the application refuse to start — the exact failure the
  transactional validator exists to prevent. Retired paths are dropped on load and gone
  from the file after the next save.
- **A chunk folder holds only what you open; `source/` holds the rest.** *2026-08-18, for
  a cleaner layout.* `premiere.json`, `rundown.md`,
  `transcript.srt` and `censor-words.txt` sit in `transcripts/<chunk>/`; `words.json`,
  `transcript.json`, `transcript.txt`, `exports.json` and `deepgram/` sit in
  `transcripts/<chunk>/source/`. The split is by *how often a person opens the file*, not
  by kind — a folder is browsed rather than searched, so everything in it competes for
  attention with the one file that has to be found.

  **A generation still spans both halves and is still committed by one transaction.**
  That is the part not to break. Two designs were considered and one was rejected:
  - *rejected*: let an owned name carry a path (`source/words.json`) inside a single
    publication. `publish_text_sets` refuses that — and so does `_validated_transaction`,
    which requires every journal name to be a bare component specifically so a corrupt or
    crafted journal cannot steer a restore outside the transcript tree (P5/P6). Loosening
    a security control to tidy a folder is the wrong trade;
  - *chosen*: two publications in the same transaction, one per directory. That mechanism
    already exists — it is how boundary stitching rewrites two chunks at once — and
    `_validated_transaction` already allows an entry directory that is a descendant of the
    transaction root, so nothing was relaxed. `exports.split_publication()` is the one
    place that knows the shape; callers holding rendered bytes (the retranscribe rollback,
    the seam's snapshot) go through it so they cannot get the ownership split wrong.

  Two consequences worth knowing. The transaction now stages one level *above* the chunk
  folder, because the two halves' common path is the chunk folder itself and a crash's
  debris must not land in the folder the user opens. And the flat names are in
  `RETIRED_EDIT_EXPORTS`, which is what makes this a *move*: a stale `words.json` beside a
  new `source/words.json` is not clutter, it is an ambiguity about which transcript is
  real, and recovery reaches the republish on its own because the old manifest declares
  names that are no longer canonical.

- **The overload 503 is written by a rejector thread, never by the accept loop.**
  *Found 2026-08-18, fixed 2026-09-01.* `server._reject_overload` used to write the JSON
  503 and close the socket **without reading the request the client had already sent**.
  Windows resets a connection closed with unread inbound data, which discards the response
  already queued for send, so the client got `WinError 10053` instead of
  `dashboard is busy; retry shortly`. It is a race between the RST and the client's read,
  so it reproduced about three CI runs in five and **not at all on the development
  machine** — 12 consecutive passes there against the unfixed code. A test that passes
  everywhere you can watch it is the reason this sat open.

  The repair is the one the earlier note asked for: something with no accept-loop cost.
  Draining before the close is what makes the 503 arrive, and doing it inline would slow
  down accepting during exactly the flood the rejection exists to survive. So
  `_reject_overload` now only hands the socket to a bounded queue and returns, and
  `REJECTOR_THREADS` workers do the rest — send, `shutdown(SHUT_WR)`, drain the request
  under both a deadline and a byte cap, close. **When the queue is full the socket is
  dropped where it stands**, which is what every rejection used to do, so a flood deep
  enough to overrun the queue costs nothing more than it used to.

  **The regression test is `test_overload_rejection_drains_the_request_before_closing`,
  not the admission test.** Whether the client loses the race is timing; whether the
  rejection reads the request is not, so the new test asserts the second over a
  `socketpair` and fails deterministically if the drain is removed (verified by removing
  it). `test_handler_admission_is_bounded_and_overload_is_json` stays exactly as it was —
  it is the end-to-end statement, and it was always right.

- **Retiring a published file needs `exports.RETIRED_EDIT_EXPORTS`, not just removal from
  `PUBLISHED_EXPORTS`.** A publish only deletes files it *owns*, so a name simply dropped
  from the list is orphaned in every existing transcript folder forever. Retired names
  stay owned and are never rendered, which deletes them on the next publish. Recovery
  reaches that publish on its own: an old manifest declares the retired file, `declared`
  is then not a subset of `canonical_names`, `publication_is_consistent` returns False,
  and `_recover_artifacts` republishes. Verified on the real 16,867-word examplechannel c000:
  files removed, 397 filler tags gone, 74 profanity tags kept, 73 "uh"/"um" still present
  as words, **generation id unchanged** — which is what keeps the existing rundowns.
- **Adobe's schema is checked into `reference/`,** taken from the spec they attach to the
  "Import Your Own Transcript" announcement, and `tests/test_premiere_schema.py`
  validates our output against it by reading the enums straight out of that file.
  Paraphrasing the enums is precisely how the invalid language tags got in.
- **A transcript being valid is not the same as it being *attached*.** Premiere binds an
  imported Static Transcript to whatever is in the **Source Monitor** at import time.
  Import it with a sequence active, or against a clip instance already in a timeline, and
  the transcript is not connected to any clip: it reads and filters normally but
  text-based editing has nothing to cut. The JSON is byte-identical either way and
  Premiere reports nothing. This is the single most likely reason a correct transcript
  "does not work". Documented in README. *An `IMPORT.md` used to be written beside every
  transcript saying so; it was removed 2026-08-17 as bloat — the same fixed text in four
  folders per session, restating what README covers once.*
- **Delete/Extract/Lift then require a *sequence*.** Even correctly attached, the edit
  operations are greyed out while a source clip is in view, because they remove frames
  from a sequence. Two separate gates; both documented in README.

---

## Settled decisions — do not re-litigate

| # | Decision | Chosen |
|---|---|---|
| 1 | Transcription | **Deepgram cloud** (user has a key). Not local. |
| 2 | Video output | **Stream-copy H.264 master + auto-generated proxies** using Adobe's proxy naming convention. |
| 3 | Early cut | **Non-destructive snapshot** — recording continues untouched. |
| 4 | Control surface | **Local web dashboard** on localhost. |
| 5 | Summary style | **Editor report.** Written from the angle of an editor cutting Twitch into YouTube (long-form and Shorts): timeline, best moments, skip list, titles. Chat evidence is used for what *landed*, the transcript for what was *said*. The 2026-08-19 "objective rundown only" constraint was withdrawn for this deliverable. |
| 6 | Summary engine | **`claude -p` (Claude Code) or `grok -p` (Grok 4.6 by default).** Paid HTTP APIs (Anthropic/Kimi/DeepSeek/OpenAI) were added 2026-08-18 and removed 2026-08-19 after they failed on the ordinary case. Do not re-add a paid API; a second subscription CLI is the shape that works. `none` switches reports off. Do not pass `-m grok-build` — the CLI rejects it. The two are asked in **different shapes**: Claude is a completion over stdin, Grok is an agent handed `transcript.md` that writes `report.md`. Do not unify the argvs — see the 2026-09-02 note. |
| 7 | Channels | **Arbitrary, added at runtime. Do not hardcode any channel.** The tool is not written for a particular streamer, and no channel name belongs in the source. |
| 8 | Storage | Masters → Desktop, kept until manually cleared. Proxies → auto-deleted after 1 day. All adjustable. |
| 9 | Language | Python for new code. A retired C#/.NET predecessor exists but is not published. |
| 10 | Automatic editing | **None.** Built 2026-08-17 and retracted the same day: it worked, and it was still the wrong deliverable — an automatic cut has to be checked, and checking it means watching it. This repository records and transcribes. |
| 11 | Filler removal | **Manual, from a verbatim transcript.** Adobe's `filler` tag is not emitted (Premiere ignored it), and nothing here removes fillers from the media. `transcription.filler_words` stays on so every one is in the transcript and easy to find. |

---

## Target environment

The pipeline is developed and proven on **Windows 11** with **Python 3.14**, and the
packaging (`VODPipeline.exe`, Start Menu registration, the Chromium app window) is
Windows-specific. The Python core is portable, and since 1.1.0 the seams that were not
take a platform branch: `server._reveal_command` (Explorer / Finder / `xdg-open`),
`app.find_app_browser` (always the system browser on macOS, because Chrome there
outlives its last window and "window closed" cannot be the shutdown signal),
`util._POSIX_HINTS` (Homebrew, MacPorts, `~/.local/bin`, `~/.grok/bin`),
`h264_videotoolbox` in the proxy encoder probe, and `config.DATA_ROOT` (a pip install
keeps `config.json`/`.work`/`logs` in the per-user data directory, a clone beside the
code, `VODPIPE_HOME` overrides both). CI runs the full suite on macOS and Ubuntu as well
as Windows; `tests/test_cross_platform.py` covers the branches. No real recording has
been made on macOS.

Required on `PATH` (or set explicitly under `tools.*` in `config.json`):

- **`streamlink` 8.4+** — capture and VOD download. Older versions differ in
  `--http-proxy` coverage and still expose `--twitch-disable-ads`, which shaped
  decisions recorded below.
- **`ffmpeg` / `ffprobe`** — segmenting, remux, proxies, audio slicing, verification.
- **`python` 3.14** — no third-party packages, so no wheel-availability problem on a
  Python this new. Keep it that way.
- Optional: **`claude`** (Claude Code) and/or **`grok`** (Grok Build) for editor reports.

Hardware notes that shaped the design rather than requirements:

- **ASR is cloud partly because the reference machine has no CUDA.** On an AMD GPU a
  local Whisper cannot reach the throughput rolling transcription needs, and Deepgram
  returns word-level timings anyway, which deletes the forced-alignment stage entirely.
- **Proxies prefer a hardware encoder** (`h264_amf` on AMD) behind a runtime probe, with
  a `libx264` fallback, so a machine without one still works — just slower.
- **Assume a single drive.** Masters at ~7 GB/hr for 1080p60 fill a disk quickly; the
  free-space floor and the reservation logic below exist because of that.

---

## Technical findings that shaped the design

**streamlink 8.4.0 removed `--twitch-disable-ads`.** The Twitch plugin now exposes only
`--twitch-low-latency`, `--twitch-supported-codecs`, `--twitch-api-header`,
`--twitch-access-token-param`, `--twitch-force-client-integrity`, and
`--twitch-purge-client-integrity`. Ad avoidance has one first-class mechanism:

1. **Twitch Turbo + OAuth token**, passed as `--twitch-api-header=Authorization=OAuth <token>`.
   Turbo covers every channel, which matters because the channel list is arbitrary.

**CORRECTED 2026-08-13 — do not restore the second part.** This brief originally called
for "ad-range detection on our own recording": log the ranges, cut them from the master,
exclude them from the transcript. That was implemented and then **removed**, because
reading the installed streamlink 8.4 source shows the premise is false:

- `TwitchHLSStreamWriter.should_filter_segment()` returns `segment.ad`, so the plugin
  **drops ad segments before they are written to our stdout pipe**. Ad content is not in
  the recording, and no interval of our file corresponds to an ad. Every range cut out
  therefore removed legitimate content.
- `log.info("Will skip ad segments")` runs unconditionally in `TwitchHLSStreamReader.__init__`,
  i.e. at startup for every stream. Matching it opened a phantom ad range at t≈0 of every
  recording.
- `"Detected advertisement break of N seconds"` describes segments streamlink already
  filtered. It is wall-clock information about Twitch, not a span of our media.

Ad log lines are now stored as operational metadata only and drive nothing.
`tests/test_ads.py` locks this in. Turbo remains the mechanism; the non-destructive-master
decision is unchanged. If ad exclusion is ever revisited it must identify ads from the
recorded packets themselves.

**Deepgram returns native word-level timings.** That removes the entire forced-alignment
stage. Combined with dropping beam-5 decoding, this is the whole reason the new pipeline
is fast — the old tool's slowness was structural, not a tuning problem.

**Premiere text-based editing requires Static Transcript JSON with per-word timings, not SRT.**
An imported SRT gives captions but does not enable text-based editing. Import path:
load source in Source Monitor → `Window > Text` → **Transcript** tab → `...` menu →
`Import > Import Static Transcript`.

**Premiere censors from a word list you supply**, not from anything inside the transcript
file. The list is a separate output.

**Filler-word detection was data, not analysis — and Premiere ignored the data anyway.**
Premiere does not scan a transcript for "uh"; it reads the `filler` tag on each word, and
Adobe's note that "if you've previously transcribed a clip you'll need to re-transcribe to
add filler words to it" says the tags are attached when the transcript is made. Ours were
attached at export time, so `republish` could add them without touching Deepgram. It made
no difference: Premiere found none of them. The feature is gone; see the
`tags` note above before considering it again.

---

## Lessons from the retired predecessor

A C#/.NET 8 predecessor did the same job locally, with Whisper large-v3-turbo over Vulkan
plus Wav2Vec2 CTC alignment over DirectML. It is not published, and nothing here depends
on it; what mattered is why it was replaced.

**Carried forward:**
- The `premiere.json` Static Transcript exporter — the schema, and the rule that starts a
  new speech segment at every pause longer than 0.4s so pauses stay visible to text-based
  editing.
- The censor-word output. It is fed from a curated master word list you supply
  (`paths.censor_master_list`) rather than regenerating one per recording.

**Deliberately dropped:**
- Beam-5 Whisper decoding.
- The Wav2Vec2 CTC forced-alignment stage.
- The retry/escalation ladder built around them.

These three are exactly what made it too slow, and cloud word timings make all of them moot.

---

## Gotchas to design around

- **Free-space floor.** Refuse to open a new chunk below a configurable threshold
  (default 50 GB). One drive, no second disk.
- **A disk reservation must be one the drive can actually meet.** *CORRECTED 2026-08-16.*
  `estimate_proxy_peak_bytes` bounded the video at every output frame's *uncompressed*
  yuv420p size — a true upper bound, and a useless one: 319 GB for a 2-hour 540p60 proxy
  that really came to 538 MB, so every proxy on an 8-hour recording was refused on a drive
  with 200 GB free. It now models what H.264 emits (a bits-per-pixel ceiling anchored at
  0.15 bpp for CRF 24 and doubling every 6 steps toward lossless, capped at raw), doubled
  for headroom. That is ~8 GB for the same chunk, still 7× the real output. Under-reserving
  is the recoverable direction — `make_proxy` stages a `.partial.mp4` and cleans it up, so
  a full drive costs one failed encode, whereas over-reserving costs every proxy.
- **Chunk boundaries must land on keyframes**, and words straddling a boundary need
  handling so the transcript doesn't lose or duplicate them.
- **Transcribe rolling, not after.** Outputs should land ~1 minute after a chunk closes,
  not 6+. Snapshots should come back in seconds.
- **The pipeline is file-based** — it writes files Premiere imports. It does not need the
  Premiere or After Effects MCP servers, both of which are currently disabled.
- **`claude -p` shares your Claude subscription usage limits.** A heavy recording day can
  bump into them. Buying a way around that was tried and withdrawn (see the engine note
  above); a rundown lost to a limit is re-queued from the dashboard once it resets, and
  the transcript it is built from was never at risk.
- Twitch stream codec is usually H.264 but can vary by channel and broadcast; don't assume
  a stream-copy always yields H.264.
