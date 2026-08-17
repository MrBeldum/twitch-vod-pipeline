# Twitch VOD → Premiere Pipeline

Records Twitch streams live in configurable chunks (default 2h) — or downloads a past VOD
through the identical pipeline — writes Premiere-ready masters + proxies, and after each
chunk emits an objective `.md` rundown plus a Premiere-importable transcript for text-based
editing. Also supports pulling a segment out mid-stream without waiting for the chunk to
finish, and routing all Twitch traffic through an HTTP/SOCKS proxy.

Status: **built, audited, hardened, and proven against real 8-hour recordings.** Two audits
were worked through (neither document is retained in this tree; `tests/test_audit_20260814.py`
and `test_confirmed_audit_fixes.py` are what they left behind); every finding was verified
and closed or confirmed already-handled. The full suite is
`python -m unittest discover -s tests -t .`. See `README.md` for operation and rationale.

**First live test, 2026-08-16 (hasanabi, 4 chunks, ~7 hours).** Capture, chunking, remux
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

**Second live test, same day (zy0xxx, and the hasanabi backlog).** All four hasanabi
chunks reached complete masters, proxies and transcripts. Three further defects, all
fixed and covered by `tests/test_live_failures_20260816.py`:

| Symptom | Root cause | Cost |
|---|---|---|
| `ffmpeg exited 4294967274 … Error muxing a packet` ×3 in 15 min | `-map 0` captured Twitch's `timed_id3`, whose DTS is not discontinuity-corrected | every network stall killed the recording |
| `claude -p failed (1):` — nothing after the colon | detail read stderr only; a `--print` CLI explains itself on stdout | the one diagnostic message carried no information |
| a rundown lost to one transient engine failure | the CLI transport had no retry while the API transport had four | c000's rundown was permanently `error` |

**Editing the output in Premiere, 2026-08-17.** The user imported a finished chunk and
found the one feature that does not work: *delete all fillers* reported nothing to delete
against a transcript carrying 397 valid `filler` tags. Filler tagging was **cancelled** —
the user's call, and the right one; see the `tags` note below. `IMPORT.md` was dropped in
the same pass as bloat. Together this removed two modules (`fillers.py`, `review.py`,
~1,400 lines), a job type, seven config keys and three published files per chunk.

**The edited cut, 2026-08-17.** The user then asked for the pipeline to do the editing
itself — "no silences, repeats, filler, and censored" — with the explicit condition that
cuts must not be sudden or clip words. That is `edit.py` (what to remove), `audio.py`
(sample-exact assembly, crossfades, mutes), `render.py` (plan → encode → mux) and an
`edit` job on `media_jobs`. It is not a re-run of the cancelled feature: Premiere's tag
was cancelled because we could neither control nor see the cut, and this controls the
boundary, blends across it, and writes `edit.md` listing every decision with a timecode.

Every threshold was measured against the 7-hour reference recording before being chosen,
because the failure mode that matters here is a confident wrong cut:

| Signal | Measurement | Decision |
|---|---|---|
| loudness | bimodal: p25 −72 dB, p50 −30 dB; −35 dB→21.0%, −41 dB→19.8%, −50 dB→19.0% | the threshold is a setting, not a ritual; `vodpipe calibrate` prints the table |
| acoustic cuts alone | clipped 37 of 701 words, worst 230 ms | the transcript veto (`_cover_words`) |
| adjacent identical words | 825 pairs; 147 of the 185 deliberate ones are punctuated ("No. No.", "money, money, money"), stutters are not and sit at a 0.000 s gap | punctuation + a 0.150 s gap bound → 640 cut, 0 false positives in a 25-sample review |
| phrase restarts | 144 ("I can I can", "that could be that could be") | removed in the `restarts` tier |
| repeated *numbers* | 4, and all four were figures: "fifty fifty" (a 50/50 split), "twenty twenty eight" (a year), "ten ten thousand", "one one point" | never cut — dropping a copy changes the fact, not the phrasing. A capitalisation test for proper nouns was measured and **rejected**: it blocked 12 correct cuts to prevent 2 questionable ones and missed the case that prompted it |
| vocalisations | 257 "uh", 121 "um", 125 s total; "mhmm" (27) and "uh-huh" (7) also present | `sounds` tier removes the first, never the backchannels — those are answers |
| discourse markers | "like" 456/916 parenthetical, "you know" 166/262, "I mean" 71/112; "so"/"actually"/"basically" only 3–6%, all sentence-initial | `smart` tier covers the first three only, and only mid-clause |

**Two features were added 2026-08-15 (see the notes below and README):** downloading past
Twitch VODs through the same chunk/remux/proxy/transcript/rundown pipeline as live, and a
configurable network proxy for reaching Twitch from a region it has left (South Korea).

Runtime transcription requires a Deepgram API key; the local key is configured and
`vodpipe doctor` reports `Ready` as of 2026-08-15. A Twitch OAuth token is optional and
enables the ad-free path.

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
  and ffmpeg dies with -22, ending the recording. A live zy0xxx capture died this way
  three times in fifteen minutes. The stream was never wanted: `plan_remux_maps` already
  dropped it at remux, and settled decision (the 2026-08-13 ad correction) rules out
  reading Twitch metadata anyway. Verified by A/B capture against the live channel:
  `-map 0` yields `0 aac / 1 h264 / 2 timed_id3`, the corrected command yields video and
  audio only. `+discardcorrupt` covers the other half of the same event
  (`Packet corrupt (stream = 1)` in the same log) — dropping a corrupt packet costs a
  frame, refusing it costs the broadcast.
- **A rundown engine gets more than one attempt** (`summary.max_retries`, default 3,
  bounded by `summary.timeout_seconds` overall). *Added 2026-08-16.* The API transport
  retried and the `claude -p` transport did not, which was backwards: the CLI is the
  default provider and the one sharing the user's subscription quota, the transient this
  project has always expected. One blip lost a rundown permanently. A non-zero CLI exit is
  not classifiable from outside — a rate limit, a dropped connection and a bad flag look
  identical — so the retry is unconditional and bounded by the deadline the single attempt
  already had. Failure detail now falls back to stdout, because a `--print` CLI writes its
  reason there and the old stderr-only message was a bare `claude -p failed (1):` with
  nothing after the colon.
- The pipeline is **stdlib-only Python**. Deepgram and the Anthropic API are called over
  `urllib`; the dashboard is `http.server`. Nothing to pip install, which sidesteps the
  Python 3.14 wheel gaps flagged below.
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
- **The edited cut is where filler removal actually happens** (`edit.py`, `audio.py`,
  `render.py`). *Added 2026-08-17, on the user's request, and not a reversal of the note
  below:* Premiere's `filler` tag stays dead because Premiere ignored it and because
  *delete all fillers* cuts on word boundaries with no review. What replaced it is our
  own cut, where we control the boundary, the crossfade and the audit trail. The two
  design rules:
  - **Acoustics propose, the transcript vetoes.** Silence is measured from the audio
    (`astats` RMS envelope at a 10 ms hop) because the transcript cannot see it — Deepgram
    pads each word to abut its neighbour, and the median gap between one word's end and
    the next word's start is 0.000s across the 60,693-word reference recording. But the
    acoustics alone clipped **37 of 701 words** on a 5-minute sample, worst case 230 ms,
    for the same reason: a padded word reaches into audio that really is silent. So every
    keep range is grown until it fully contains each word it touches (`_cover_words`).
    That removed all 37 and cost 1.1 points of running time (20.5% → 19.4%). **Never
    make a cut decision from the transcript's gaps, and never let one land inside a word.**
  - **A word-derived cut edge may only grow outward, into a gap.** `_snap_removal` looks
    for the quietest instant near a boundary, bounded by the neighbouring words' own
    edges. It cannot shrink and it cannot cross a word: a stop consonant is quieter than
    the room, so an unbounded energy search walks straight into the next word. Where the
    words genuinely abut there is no gap and the cut stays where the transcript put it.
- **Audio and video are cut by different mechanisms, and only the arithmetic keeps them
  together.** Video is `select`'d frame-wise by ffmpeg; audio is assembled sample-wise in
  Python. If both were measured independently the roundings would disagree at every join,
  and over the ~950 cuts a measured 2-hour chunk produces the error random-walks into
  ~0.25 s.
  They agree because each segment's sample count is *derived from* its frame count
  (`audio.segments_for`), never measured. Verified against a synthetic source with a flash
  and a beep on every second: the A/V spread was 19.3 ms — pure measurement quantisation —
  and **identical at 21 cuts and at 113**. Also verified end to end by re-transcribing the
  edited audio of a real chunk: Deepgram heard exactly the 244 words the remap predicted,
  median offset −20 ms. Two things this depends on: `setpts=PTS-STARTPTS` before the rate
  normalisation, so frame 0 is time 0; and the video/audio `start_time` delta (0.034 vs
  0.044 on the reference masters — 10 ms, 480 samples), which must be applied to the PCM
  read or the whole track sits half a frame out.
- **The edit plan is built on the audio and applied to the video, and the two streams of
  a real capture do not end together.** *Found by the first full-length render, 2026-08-17.*
  c003's audio runs **1.03 s past its video** (3533.51 s against 3532.48 s), so the tail of
  the plan asked `select` for 14 frames that were never recorded; the encoder produced 12
  fewer frames than the audio had been cut for, which would have put the whole track out of
  step from that point back. `edit_stream_geometry` now returns the real frame count,
  `render_edit` clamps the planning duration to `video_frames / fps`, and `segments_for`
  takes a `max_frames` ceiling. Note the *other* reference chunk has the opposite skew
  (video 7200.867 s, audio 7200.832 s), which is why this never showed up on c000 and why a
  test fixture would not have caught it either. **The post-encode `frames != total_frames`
  check is what turned this into a refused job rather than a shipped file that drifts —
  do not relax it.**
- **The transcript is remapped against the ranges that were *rendered*, and clamped into
  them.** *Found by the acceptance check on the first full-length render, 2026-08-17.*
  Two separate bugs, one after the other, and the second was caused by fixing the first:
  - Remapping against `plan.keep` uses float boundaries the encoder never saw. Every one
    is rounded to a frame, and `remap_words` places a word by the cumulative length of
    everything before it, so those roundings random-walk — ~0.2 s of transcript drift by
    the end of a chunk with 478 cuts. Use `first / fps, (last + 1) / fps` from the frame
    ranges; they sum to exactly the output's duration, so there is nothing to accumulate.
  - But a frame-locked range *starts up to one frame later* than the boundary the planner
    chose, and the planner puts boundaries exactly on word starts (`_cover_words`).
    `word.start - range_start` then goes negative and the word lands *before* its own
    range, overlapping the last word of the previous one — 48 of c003's 8,022 words, by
    2–10 ms, one frame at 60 fps. So a word is clamped into its range's extent, and the
    **endpoints** are rounded with the duration derived from them: rounding a start and a
    duration separately lets their sum cross the next word's rounded start, and rounding
    is monotonic only if you round both ends of the span.
  The failure was silent at the point of damage. `write_exports` wrote the file happily
  and `load_words` refused it, so the edit was finished, correct, and had a `words.json`
  nothing could read — including `_edit_generation`, which reads it to decide whether to
  spend another forty-minute encode, and would therefore have rebuilt that chunk on every
  start forever. **`words_json_text` now runs its own output through `words_from_json`
  before returning it**, so the single funnel every `words.json` goes through cannot emit
  one this codebase cannot read. Refusing costs one publish and leaves the previous
  outputs alone; writing it costs the file.
- **The `select` expression is a balanced search tree, not a sum of `between()` terms.**
  A flat expression is O(n) per frame: ~950 ranges × 432,000 frames is 400 million
  evaluations before a macroblock is encoded. The tree is O(log n). It goes in a file via
  `-filter_script:v` because it runs to tens of kilobytes, far past a Windows command line.
- **An edit is queued only once its seam is settled** (`_seam_settled`). Boundary repair
  rewrites a chunk's tail when its *successor's* transcript completes, so cutting earlier
  spends a forty-minute encode to be redone for one word at the join. The gate costs at
  most one chunk of latency on a live recording. `recut()` — the manual path — ignores it
  deliberately, and deletes the existing file first so a rebuild actually rebuilds rather
  than adopting the file the operator asked to replace.
- **Plan before encoding, and refuse before encoding.** `render_edit` decodes audio,
  measures, plans, and only then touches video. A misconfigured threshold or a silent
  track produces an absurd plan; finding that out after half an hour of h264 is the
  difference between a warning and a wasted evening. `EditRefused` is recorded as
  `skipped`, not `error`, so recovery does not retry an encode that cannot succeed.
- **`array('h').extend(bytes)` appends one sample per byte.** It pads to twice the length
  asked for, and the audio then no longer matches the frame count it was derived from —
  which is the one thing `audio.py` exists to guarantee. Use a list of ints. Caught by
  `test_reading_past_the_end_pads_rather_than_truncating`; there is no other symptom until
  a chunk whose last segment runs past EOF desyncs.
- **`profanity` is the only `tags` value we emit. Filler tagging is removed — do not
  reinstate it.** *2026-08-17, on the user's report.* Adobe's enum allows `profanity` and
  `filler`; we wrote both, and the user's Premiere reported **"no filler words detected"**
  against a `premiere.json` carrying 397 `filler` tags on a valid, importable, correctly
  attached transcript. The tag is documented and schema-legal, so the reason it is ignored
  is inside Premiere and not observable from here. The user's decision was to cancel the
  feature rather than keep debugging it, and independently: *delete all fillers* cuts on
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
  `_walk` raises on any unknown path, so deleting a key that is present in the user's
  installed `config.json` makes the application refuse to start — the exact failure the
  transactional validator exists to prevent. Retired paths are dropped on load and gone
  from the file after the next save.
- **Retiring a published file needs `exports.RETIRED_EXPORTS`, not just removal from
  `PUBLISHED_EXPORTS`.** A publish only deletes files it *owns*, so a name simply dropped
  from the list is orphaned in every existing transcript folder forever. Retired names
  stay owned and are never rendered, which deletes them on the next publish. Recovery
  reaches that publish on its own: an old manifest declares the retired file, `declared`
  is then not a subset of `canonical_names`, `publication_is_consistent` returns False,
  and `_recover_artifacts` republishes. Verified on the real 16,867-word hasanabi c000:
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
| 5 | Summary style | **Objective rundown only.** No clip recommendations, no editorializing. User was explicit. |
| 6 | Summary engine | `claude -p` headless against the user's existing subscription. Pluggable so an API key can replace it. |
| 7 | Channels | **Arbitrary, user-added at runtime. Do not hardcode any channel.** User said "various streamers, don't assume which." |
| 8 | Storage | Masters → Desktop, kept until manually cleared. Proxies → auto-deleted after 1 day. All adjustable. |
| 9 | Language | Python for new code. The retired C# predecessor is archived privately at `github.com/MrBeldum/vod-transcript`. |
| 10 | Automatic editing | **Dead air and disfluency only.** The edited cut removes silence, filler sounds and false starts and mutes censored words. It makes **no judgement about content** — that is decision #5 again, and the reason the edit is safe to trust. It is a derivative: the master is never modified. |
| 11 | Filler removal | **Ours, not Premiere's.** Adobe's `filler` tag is not emitted (Premiere ignored it). Fillers are cut by `edit.py`, where the boundary, the crossfade and the audit trail are under our control. |

---

## Verified environment

Checked 2026-08-13. Re-verify if the machine changed.

- Windows 11 Home 10.0.26200. PowerShell is primary; a Bash tool exists but takes POSIX syntax.
- Ryzen 5 5600 (6c/12t), 32 GB RAM.
- **AMD Radeon RX 6600, 8 GB — no CUDA, no NVIDIA.** This is the reason local Whisper cannot hit 20x realtime and the reason ASR is cloud.
- **Single drive, C:, 191 GB free at time of check.** Masters land on the Desktop, which is on C:. At ~7 GB/hr for 1080p60 that is roughly 27 hours of headroom before proxies.
- `streamlink` **8.4.0** → `C:\Users\Daniel\AppData\Local\Programs\Streamlink\bin\streamlink.exe`
- `ffmpeg` / `ffprobe` → `C:\ffmpeg\bin\`
- `python` **3.14.6** → `C:\Python314\python.exe` — very new; keep dependencies minimal.
- `node` → `C:\Program Files\nodejs\node.exe`
- `claude.exe` → `C:\Users\Daniel\.local\bin\claude.exe`, authenticated against the user's subscription.
- `yt-dlp` **2026.07.04** → `C:\Users\Daniel\Tools\yt-dlp\bin\yt-dlp.exe`, on the user PATH. Its media library is `C:\Users\Daniel\Videos\yt-dlp`.

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
no difference: the user's Premiere found none of them. The feature is gone; see the
`tags` note above before considering it again.

---

## Historical `vod-transcript` source

The retired C#/.NET 8 predecessor is archived privately at
`https://github.com/MrBeldum/vod-transcript`; it is no longer installed locally. It used
Whisper large-v3-turbo over Vulkan plus Wav2Vec2 CTC alignment over DirectML. Its README
is the reference if any historical implementation detail is needed.

**Port these:**
- The `premiere.json` Static Transcript exporter — schema and the rule that starts a new
  speech segment at every pause longer than 0.4s so pauses stay visible to text-based editing.
- The censor-word output. Feed it from the user's existing curated master list at
  `C:\Users\Daniel\Desktop\censored_words_master.txt` rather than regenerating one.

**Do not port:**
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
- **An edit costs disk and an encode.** ~40 min of h264_amf and ~8 GB per 2-hour 1080p60
  chunk, on top of the master and the proxy. Peak disk while it runs is ~13 GB: the source
  PCM (1.4 GB), the edited PCM (~1.2 GB), the rendered video-only file and the muxed
  output. **`estimate_edit_peak_bytes` is anchored on the master's own file size, not a
  bits-per-pixel model.** The edit is a re-encode of that exact footage at that exact
  resolution, so the master predicts the output far better than a pixel count can — the
  first version used the bpp model and asked for **82 GB** to build that 13 GB chunk,
  which is the 319 GB proxy mistake all over again. *A reservation the drive cannot meet
  does not protect the disk, it turns the feature off.* This codebase has now made that
  error twice; check any new estimator against a real file before believing it.
- **Chunk boundaries must land on keyframes**, and words straddling a boundary need
  handling so the transcript doesn't lose or duplicate them.
- **Transcribe rolling, not after.** Outputs should land ~1 minute after a chunk closes,
  not 6+. Snapshots should come back in seconds.
- **The pipeline is file-based** — it writes files Premiere imports. It does not need the
  Premiere or After Effects MCP servers, both of which are currently disabled.
- **`claude -p` shares the user's subscription usage limits.** A heavy recording day could
  bump into them, which is why the summarizer must stay pluggable.
- Twitch stream codec is usually H.264 but can vary by channel and broadcast; don't assume
  a stream-copy always yields H.264.
