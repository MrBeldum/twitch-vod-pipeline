# Twitch VOD → Premiere Pipeline

Records Twitch streams live in chunks — or downloads a past VOD through the identical
pipeline — writes Premiere-ready masters and proxies, and publishes a word-timed transcript
plus an objective rundown for each chunk. It then cuts a **second, edited copy** with the
silences, filler sounds and false starts removed and censored words muted, and a matching
transcript so you can keep editing from text. You can also pull any range out of a broadcast
while it is still recording, and route everything through an HTTP/SOCKS proxy to reach Twitch
from a region it has left (such as South Korea).

Pure Python 3.14 standard library — no pip install, no virtualenv, no wheels to build.
External binaries only: `ffmpeg`, `ffprobe`, `streamlink`, and optionally `claude`.

---

## Quick start

```
vodpipe.cmd doctor          # check the environment
vodpipe.cmd                 # start the dashboard at http://127.0.0.1:8420
```

Then in the dashboard: paste your Deepgram key under **Settings**, add a channel, and
either press **Record** or leave **auto** ticked so it starts on its own when that
channel goes live.

To run it without a browser:

```
vodpipe.cmd record somechannel            # waits until the channel goes live
vodpipe.cmd record somechannel --now      # start this instant, live or not
vodpipe.cmd record somechannel --minutes 90
vodpipe.cmd vod https://www.twitch.tv/videos/123456789   # download a past VOD
vodpipe.cmd vod 123456789 --start 1:30:00 --duration 45:00   # just part of it
vodpipe.cmd sessions
vodpipe.cmd snapshot <session-id> --last 10
vodpipe.cmd transcribe "C:\path\to\any.mp4"
vodpipe.cmd edit <session-id> --dry-run   # what the edited cut would remove
vodpipe.cmd edit <session-id>             # build it
vodpipe.cmd calibrate "C:\path\to\a_master.mp4"   # pick the noise threshold
```

---

## What lands on disk

```
Desktop\twitch-vods\<channel>\<session-id>\
  master\
    <channel>_<session>_c000.mp4          ← the untouched recording
    Proxies\
      <channel>_<session>_c000_Proxy.mp4  ← auto-deleted after 1 day
    Edited\
      <channel>_<session>_c000_Edited.mp4 ← silences, fillers and repeats cut out
  snapshots\
    <channel>_<session>_snap_..._001432.mp4
    snapshots.json
  transcripts\
    c000\
      premiere.json        ← Static Transcript, enables text-based editing
      transcript.srt       ← captions
      transcript.txt       ← timestamped plain text
      transcript.json      ← segments + words, for anything else you build
      censor-words.txt     ← the terms from your master list that actually occur
      words.json           ← raw accumulated word stream (internal)
      rundown.md           ← objective rundown
      edit.md              ← every cut the edited file made, with timecodes
      edited\              ← the same export set, for the edited file
        premiere.json  transcript.srt  transcript.txt  words.json
  live\                    ← working .ts files, removed once remuxed
  logs\
  session.json             ← machine-readable state
  index.md                 ← human-readable map of the session
```

Masters stay until you delete them. Proxies self-clean after `proxies.retention_days`.

---

## Downloading a past VOD

A Twitch VOD goes through the **exact same pipeline as a live recording**. streamlink
pipes the archived video into the same ffmpeg segmenter, so you get keyframe-aligned
masters, auto-attached proxies, per-chunk word-timed `premiere.json` transcripts,
censor lists and objective rundowns — identical Premiere-ready output, from an archive
instead of a live edge.

From the dashboard: paste a VOD URL (or its numeric id) into **Download a VOD** and press
**Download**. It appears in **Recording now** with a **VOD** badge while it downloads and
then drops into **Sessions** like any other recording; you can even snapshot a range out of
it while it is still downloading.

From the command line:

```
vodpipe.cmd vod https://www.twitch.tv/videos/123456789
vodpipe.cmd vod 123456789                       # a bare id works too
vodpipe.cmd vod 123456789 --start 1:30:00 --duration 45:00
```

`--start` and `--duration` accept seconds or an `HH:MM:SS` clock and fetch only that slice
of a long VOD (streamlink's `--hls-start-offset` / `--hls-duration`). Leave them off for the
whole thing.

The broadcaster's name is read from the VOD's own metadata, so the session lands under that
channel's folder beside its live recordings; if the name can't be resolved it falls back to
`vod_<id>`. Downloads run on their own lock (`vod-<id>`), so a VOD download and a live
recording of the same channel — or two different VODs — can run at once, and the same VOD
cannot be downloaded twice concurrently. A VOD that Twitch will not serve (deleted,
subscriber-only, or geo-blocked with no proxy set) is refused up front rather than left as
an empty session; set `network.proxy` for the geo-blocked case (see *Recording from South
Korea*).

---

## Getting it into Premiere

**Masters and proxies.** Import the `master` folder. Then select the clips →
right-click → **Proxy → Attach Proxies…**. Proxies are written as
`<mastername>_Proxy.mp4` inside a `Proxies` subfolder, which is Adobe's own naming
convention, so the attach dialog matches them automatically.

> There is no `.epr` ingest preset in this repo. Adobe's `.epr` is an opaque binary
> format that cannot be reliably synthesised, so the pipeline satisfies the same goal
> the honest way — by writing proxies that Premiere's built-in Attach Proxies already
> recognises. If you would rather have Premiere generate them on ingest, point its
> ingest preset at the `master` folder and turn `proxies.enabled` off.

**Text-based editing.** Each chunk's transcript lives in
`transcripts/c00N/` beside the master of the same number.

Double-click the master **in the Project panel** so it opens in the **Source Monitor**,
then `Window > Text` → **Transcript** tab → `...` menu →
**Import > Import Static Transcript** and choose that chunk's `premiere.json`.

> **The Source Monitor step is the one that matters.** Premiere binds an imported
> transcript to whatever is loaded there. Import it with a *sequence* active, or against
> the clip instance already sitting in a timeline, and you get a transcript that is not
> attached to any clip — it reads fine, it filters fine, and text-based editing has
> nothing to cut, because as far as Premiere is concerned the words and the footage are
> unrelated objects. The file is identical in both cases and Premiere does not say which
> happened. If a transcript feels disconnected from its clip, this is why: undo the
> import, load the master in the Source Monitor, and import again.

An SRT gives you captions but *not* text-based editing — that is why `premiere.json`
exists and why every word carries its own start and duration.

> **Put the clip in a sequence before you try to delete anything.** Importing the
> transcript against a clip in the Source Monitor gives you a readable, searchable,
> filterable transcript — but **Delete**, **Extract** and **Lift** stay greyed out,
> because those operations remove frames *from a sequence* and a source clip has no
> sequence to remove them from. Drag the master to a timeline, select the clip, and
> work from the Transcript panel with that sequence active. This catches everyone
> once; it is a property of Premiere, not of the file.

**Filler words are transcribed, not tagged.** Every "uh" and "um" appears in the
transcript as an ordinary word, so the text matches the audio and a cut made from it
lands where you expect. Select one and delete it like any other word.

> **Premiere's *delete all fillers* is not supported, deliberately.** This pipeline
> used to write Adobe's `filler` tag onto the words it was confident about. Premiere
> ignored them — the Filter menu reported "no filler words detected" against files
> carrying hundreds of tags — and the feature was removed on 2026-08-17 rather than
> left in as something that looks configured and does nothing. Bulk deletion was the
> wrong shape for this footage anyway: it cuts on word boundaries, which is audible on
> a hesitation that runs into the next word, and one wrong call in a thousand silently
> removes real speech. Filler *removal* is a manual edit; the transcript's job is to
> make each one easy to find.

Turn `transcription.filler_words` off if you would rather have clean reading copy than a
verbatim transcript. That one is a Deepgram request parameter, so changing it does
require re-transcribing — Deepgram was previously told not to write those sounds down.

**Censoring.** Premiere censors from a word list you supply; nothing inside the
transcript file drives it. Open `censor-words.txt`, copy the terms, and paste them into
Text panel → Transcript → Filter → **Censored words**. The file only lists terms from
your master list that were actually spoken in that chunk, so it stays short.

If you would rather not do any of that by hand, the edited cut below has it done
already.

---

## The edited cut

Beside every master, in `master/Edited/`, the pipeline writes a second file with the
dead air taken out:

- **silences removed** — anything quieter than `edit.noise_floor_db` for longer than
  `edit.min_silence_seconds`, keeping `edit.margin_seconds` of air either side;
- **filler sounds removed** — "uh", "um", "er";
- **repeats and false starts removed** — "the the", "I can I can", "that could be that
  could be";
- **censored words muted** — the audio is silenced in place; the picture, the timing and
  the transcript are untouched, because muting a word you did not want to hear is a
  smaller intervention than cutting a hole in the timeline;
- **a matching transcript** in `transcripts/c000/edited/`, so text-based editing works
  on the cut file too;
- **`edit.md`** listing every decision with a timecode.

Measured on the reference recording — a seven-hour HasanAbi broadcast, four chunks:

| chunk | source | edited | removed | cuts |
|---|---|---|---|---|
| c000 | 2:00:00 | 1:39:29 | 17.1% | 955 |
| c001 | 2:00:00 | 1:42:34 | 14.5% | 942 |
| c002 | 2:00:00 | 1:41:01 | 15.8% | 953 |
| c003 | 0:58:53 | 0:46:16 | 21.4% | 478 |

**6h59m of recording became 5h49m.** Most of that is silence; fillers and repeats
together are a couple of minutes per chunk. Budget roughly forty minutes of background
encoding per two-hour chunk.

> **The master is never modified.** The edit is a derivative, like a proxy. If a cut is
> wrong, change the setting and re-run it; nothing you disagree with is permanent.

### Why it does not cut a word in half

Silence is measured from the audio, because the transcript cannot see it: Deepgram pads
each word so it abuts its neighbour, and across 60,693 reference words the median gap
between one word ending and the next starting is **0.000s**. Transcript gaps carry
almost no information about where the speaker actually stopped.

The consequence is that the audio and the transcript disagree about where a word ends,
and on a five-minute sample the acoustic cuts alone clipped **37 of 701 words**, the
worst by 230 ms. So the two are used for different jobs: **acoustics propose a cut, and
the transcript vetoes it.** Every keep range is grown until it fully contains any word it
touches. That removed all 37 clips and cost 1.1 percentage points of running time.

Cuts are also blended rather than butted: `edit.crossfade_ms` of equal-power crossfade at
every join, taken from the material that would have followed the outgoing side, so a cut
cannot click. Mutes ramp in and out inside their margin for the same reason.

### The noise threshold

`vodpipe calibrate <file>` prints what each threshold would actually remove:

```
  threshold   silence found   share
      -35 dB       00:01:15    25.3%
      -41 dB       00:01:09    23.0%  <- edit.noise_floor_db
      -47 dB       00:01:05    21.8%
```

Speech is either loud or near-silent with very little in between — p25 of the reference
recording is −72 dB and p50 is −30 dB — so the exact number matters much less than it
looks. Anything from about −35 to −50 dB gives the same answer to within two percentage
points. Pick one from the flat part of the table. `edit.md` repeats this per chunk.

### Rebuilding one

Nothing about an edit needs Deepgram again — the words are already stored — so it is
cheap to re-run with different settings:

```
python -m vodpipe edit <session-id> --chunk c000            # rebuild one
python -m vodpipe edit <session-id> --dry-run               # plan only, writes edit.md
```

or press **Re-cut** on the chunk in the dashboard. A chunk is cut automatically once its
transcript is complete *and* its chunk boundary has been repaired, which happens when the
next chunk finishes — waiting for that costs at most one chunk of latency and saves a
whole re-encode per chunk.

### What it will not do

It does not judge content. It removes dead air and disfluency, and that is all; it has no
opinion about what is interesting, which is the same settled decision that keeps the
rundown objective. If a plan would remove more than `edit.max_removed_fraction` of a
chunk the edit is refused rather than rendered, because that is a wrong threshold or a
silent track rather than an editorial result.

---

## How it works, and why

**Recording.** `streamlink` writes the live stream to stdout; ffmpeg's segment muxer
stream-copies it into chunks. No re-encode, so CPU stays free and quality is whatever
Twitch sent.

**Record means "record when there is something to record".** Pressing **Record** on a
channel that is live starts immediately. On a channel that is not, it *arms* the channel
instead — nothing is launched, no session directory is claimed, no lock is taken — and
the watcher starts the real recording on the pass that first sees it live. The row shows
an amber dot and **Cancel** until then.

This matters because streamlink is configured to retry forever, which is right once a
stream exists (a broadcast that drops for a minute is waited out) and wrong at the very
start. Previously, Record on an offline channel produced a session sitting at `recording`
with an empty chunk for as long as the app ran, and the dashboard reported it as
recording, because as far as the pipeline was concerned it was.

Arming outranks the per-channel **auto** setting — it is an explicit instruction given
after that setting was chosen — and armed channels are probed even with the watcher
switched off, and even if they were never added to the watch list. A request that fails
to start for a transient reason (a momentary disk dip) stays armed and is retried.

There is a backstop for the case where the live check was simply wrong: if not one byte
of video arrives within `recording.startup_timeout_seconds` (default 120), the attempt is
abandoned and says so. Only *zero* bytes counts — once anything has been written the
stream existed, and from then on retrying is exactly the behaviour we want.

On the command line, `vodpipe.cmd record <channel>` waits the same way; pass `--now` to
start immediately regardless.

**Chunks land on keyframes.** The segment muxer cannot cut a copied stream mid-GOP, so
it rolls forward to the next keyframe. Every master therefore starts on a keyframe —
there is a test asserting exactly this.

**Chunks are recorded as MPEG-TS, then remuxed to MP4.** This is the decision the whole
design hangs on. An MP4 has no index until it is closed, so a live MP4 is unreadable —
you could not transcribe it, and you could not cut a snapshot out of it. MPEG-TS is
readable while it is still being appended to. When a chunk closes it is remuxed
(container rewrite only, no re-encode) into an MP4 that Premiere is happy with, and the
`.ts` is reclaimed.

**Transcription runs during the recording, not after it.** Audio is sliced out of the
growing `.ts` every few minutes and sent to Deepgram, which returns word-level timings
natively. That is what removes the forced-alignment stage the old C# tool needed, and
it is why exports land about a minute after a chunk closes instead of six.

**Slice seams.** Consecutive slices deliberately overlap by a few seconds, so a word
straddling the boundary is heard whole by at least one of them. The two streams are then
joined at the *earliest* point in the overlap where neither has a word in flight. Earliest
rather than widest, deliberately: everything after the seam comes from the newer slice,
and the newer slice is the better one throughout the overlap because the older one had
its audio truncated at that very point — its last word is often clipped (`wor` for
`world`). Cutting as early as is safe hands the whole contested region to the
transcription that actually heard all of it.

**Chunk seams need a different answer.** Two chunks are two separate files transcribed
independently, so overlapping slices cannot help: a word spoken across the join is heard
by neither side in full, and comes out as `wor` at the end of one and `ld` at the start
of the next — or vanishes from both. When a chunk closes, the pipeline transcribes a
short passage (default 6s either side) built from the tail of the previous file and the
head of the new one, and hands each word to whichever chunk it was *mostly* spoken in.
That gives one deterministic owner per word, so a straddling word appears once, whole,
and never twice. Anything within a fraction of a second of the join is replaced by that
reading; anything further in that the seam pass did not cover is left exactly as it was,
because ASR is not deterministic and a second pass failing to repeat a word is not
evidence the word was never said. A seam pass that returns nothing changes nothing. Cost
is one extra short request per chunk boundary, and it can be turned off in Settings.

One consequence worth knowing: the previous chunk's `rundown.md` is *not* regenerated
afterwards. It was written from a transcript that differs by at most a word at the very
end, and rewriting rundowns hours after the fact would cost far more than that is worth.

**Snapshots are non-destructive.** The recorder's ffmpeg keeps writing untouched; the
snapshot only ever opens that file for reading. A range that crosses a chunk boundary is
cut from both files and joined, so "the last 20 minutes" works even when the current
chunk is two minutes old. Copy mode returns in seconds but can only start on a keyframe
(≈2s of lead-in on Twitch); tick **frame-exact** to re-encode for a precise start.

The range has to be covered end to end before anything is cut — a session missing a chunk
in the middle is refused with the gap named, rather than quietly producing a shorter file
that jumps. The duration reported afterwards is probed from the finished file, not echoed
back from the request, so a short export is visible instead of hidden. Cuts are queued on
their own worker pool and the dashboard answers `202 Accepted` immediately; while one is
reading a `.ts`, the remux that would normally reclaim that file waits for it to finish.

**Work is scheduled in three pools, not one queue.** Rolling transcription, chunk
finalisation and remuxes are capture-critical and get their own workers. Proxy transcodes
and rundowns are heavy but disposable and get another. Snapshots are user-initiated and
get a third. With a single FIFO, a quarter-hour `claude -p` call could sit in front of the
transcript slice for a channel that was still recording.

**Proxies** are hardware-encoded via `h264_amf` on the RX 6600 when it is available —
verified by actually encoding two seconds of test pattern, because a build listing an
encoder does not prove the GPU accepts it — and fall back to `libx264` otherwise.

**Summaries** run through `claude -p` against your subscription by default. Switch
`summary.provider` to `anthropic-api` and set an API key if a heavy recording day starts
bumping into subscription limits.

---

## Ads

**Twitch Turbo + an OAuth token is the mechanism.** Paste the token into Settings and it
is passed as `--twitch-api-header=Authorization=OAuth <token>`. Turbo covers every
channel, which matters because the channel list is arbitrary.

**Nothing is ever cut out of your recording to remove ads.** That is deliberate, and it
is worth explaining because an earlier version of this pipeline got it wrong.

streamlink's Twitch plugin already filters ad segments out of the stream before it
reaches us — `TwitchHLSStreamWriter.should_filter_segment()` returns `segment.ad`, so ad
segments are dropped, not forwarded. Ad content is therefore *not present in the file we
record*, and no interval of that file corresponds to an ad. An earlier build watched
streamlink's log for ad messages and cut the matching time ranges out of transcripts and
masters. Two things made that actively harmful:

- There was nothing there to cut, so every "ad range" removed real content.
- `"Will skip ad segments"` is logged unconditionally when the HLS reader starts, for
  every stream. Matching it opened a phantom ad range at the top of every recording.

So ad log lines are now recorded as **operational metadata only** — you can see them in
`session.json` and `index.md` with an approximate broadcast position, and they influence
nothing. There is no ad-free master output and no transcription exclusion, and
`tests/test_ads.py` exists specifically to stop that mapping being reintroduced.

If a Turbo-less recording does contain ad content, cut it in Premiere. A correct
automated version of this would need to identify ads from the recorded packets
themselves, not from log lines about segments that were already discarded.

---

## Why a recording can be 720p

This one cost four two-hour masters before it was noticed, so it is documented in full.

A `hasanabi` session recorded on 2026-08-13 produced four chunks at **1280×720**. The
pipeline was configured with `quality: "best"` and did exactly the right thing — the
ladder Twitch served simply had nothing better on it. From the session's own
`logs/streamlink.log`:

```
[cli][info] Available streams: audio_only, 160p (worst), 360p, 480p, 720p60 (best)
[cli][info] Opening stream: 720p60 (hls)
```

**The cause is the connection's country.** Fetching the raw Usher master playlist directly
on 2026-08-14 showed that every variant Twitch offered, on **every** channel tested, was
marked `IVS-VARIANT-SOURCE="transcode"` — the broadcaster's own full-quality `source`
rendition was never offered at all. The same playlist carried `USER-COUNTRY="KR"`.
Connecting from Japan through a VPN restores the `source` rendition, and with it
1080p60 source quality. That was confirmed directly.

With source withheld, all that remains is the channel's transcode ladder, and which stack
a channel is on then decides the ceiling:

| Channel | `TRANSCODESTACK` | Best offered from a KR IP |
|---|---|---|
| `xqc`, `caseoh_`, `hasanabi` | `2025-Transcode-ELT-V1` | **720p60** |
| `summit1g`, `otplol_` | `Custom:1_Source_Max_1440p60…:5_Transcode_Max_1080p60…` | 1080p60 |

That second row is why the cap is easy to misdiagnose: some channels *do* reach 1080p60
from Korea, so a spot check on the wrong channel suggests everything is fine.

Things that were tested and made **no** difference:

- `--twitch-supported-codecs h264,h265,av1` — identical ladder, so this is not Twitch's
  HEVC/AV1 enhanced-broadcasting tiers being filtered out.
- `--twitch-force-client-integrity` — identical ladder.
- Subscriber-gated quality — streamlink logs `"requires a subscription"` when that is the
  cause, and that warning appears nowhere in 43,000 lines of the session log.
- Being a RERUN. Twitch does cap reruns, but these were genuine live broadcasts.

**So: to record at source quality from Korea, route Twitch through another region** — either
a full VPN or the built-in `network.proxy` setting (an HTTP/SOCKS proxy applied to every
streamlink call; see *Recording from South Korea*). A Twitch OAuth token is still worth
setting for ad avoidance, but it is not what gates resolution.

**What the pipeline does about it now.** It can't raise a ceiling Twitch imposes, but it
will never let one pass silently again:

- Both streamlink lines are parsed and stored on the session as `quality_selected` and
  `quality_available`, so any recording can be explained afterwards from `session.json`.
- Each chunk records the **measured** `width`/`height` read back off the finished master,
  which is ground truth rather than a rendition name.
- `recording.min_height` (default `1080`) is the resolution you expect. A capture below it
  raises a warning on the session — shown on the dashboard row without expanding it, and
  at the top of `index.md`.
- That warning distinguishes the two causes, because they need different responses:
  *"Twitch offered nothing better"* (no setting can help) versus *"720p60 taken but
  1080p60 was available"* (a real defect — check `recording.quality`).
- `recording.on_low_quality` is `warn` by default. Set it to `refuse` to abort instead;
  `warn` is the default because a 720p capture of a one-time broadcast beats no capture.
- `recording.quality` accepts a streamlink fallback chain, e.g. `1080p60,best`.

---

## Recording from South Korea

Twitch shut its Korean operations down in February 2024. From a Korean IP Twitch withholds
the `source` rendition on every channel, so the best you can record is whatever transcode
the channel happens to have — 720p60 on many large channels. The full evidence is in "Why a
recording can be 720p" above. There are two ways to get back to source quality, and both
work for live capture *and* VOD downloads:

**A network proxy (`network.proxy`) — the light-weight option.** Set an HTTP or SOCKS proxy
in Settings (or `config.json`) and every streamlink request — live capture, VOD download,
and the live-status probe — is routed through it, so only Twitch traffic leaves the country
and the rest of the machine stays on its normal connection. It accepts `http(s)://` and
`socks4/4a/5/5h://` URLs, e.g.:

```
socks5://127.0.0.1:1080          # e.g. an SSH dynamic tunnel: ssh -D 1080 user@jp-host
http://user:pass@proxy.example:3128
```

Prefer `socks5h://` for a SOCKS proxy so DNS is resolved on the proxy side too. `vodpipe.cmd
doctor` prints whether a proxy is configured. A proxy endpoint that cannot sustain the
throughput below shows up as buffering, not an error.

**A full VPN — the heavy option.** A VPN to Japan was confirmed on 2026-08-14 to restore
source quality (1080p60). It routes the whole machine and needs no pipeline setting.

This corrects an earlier claim in this file, which said capture worked at full quality
from Korea without a VPN. It does not. That conclusion came from spot-checking channels
that happened to have a 1080p60 *transcode*, which masked the missing source rendition.

Everything else is geography-neutral: Deepgram and the Anthropic API are reachable
globally, and the dashboard is loopback-only. Two things to keep in mind when recording
over a VPN:

- **Throughput matters.** Source 1080p60 is roughly 7 GB/hr sustained. A VPN endpoint
  that cannot hold that will show up as buffering and dropped segments, not as an error.
- **A VPN drop is survivable.** streamlink is configured to retry indefinitely
  (`--retry-streams 5 --retry-max 0`), so a brief reconnection is waited out rather than
  ending the session. The startup watchdog only fires when *zero* bytes have ever
  arrived, so it will not kill a recording that has already started.

Twitch Turbo remains the pipeline's only ad avoidance, and a Korean billing address may
affect buying it. That costs coverage at ad breaks, not resolution. See the Ads section.

Re-check with `streamlink https://twitch.tv/<a live channel>` if Twitch's regional
behaviour changes; look at whether the ladder reaches source quality, not just at whether
it reaches 1080p.

## Disk safety

One drive, no second disk, so there are two guards:

- **Free-space floor** (default 50 GB): a new chunk will not open below it. The session
  ends cleanly at the current chunk boundary instead.
- **Hard reserve** (default 10 GB): crossing this aborts mid-chunk, because a disk that
  fills while ffmpeg is writing costs the whole chunk rather than the tail of it.

At roughly 7 GB/hr for 1080p60, the default floor leaves about 7 hours of headroom.

---

## Configuration

Everything lives in `config.json` next to the app — gitignored, created on first save.
`config.example.json` documents the common keys; the full set of defaults, with comments,
is in `vodpipe/config.py`.

Secrets resolve config first, then environment (`DEEPGRAM_API_KEY`, `TWITCH_OAUTH_TOKEN`,
`ANTHROPIC_API_KEY`), so a shell export works too.

---

## Tests

```
C:\Python314\python.exe -m unittest discover -s tests -t .
```

Most are instant; the integration tests run the real pipeline against a synthetic stream —
a local ffmpeg process stands in for streamlink and
emits MPEG-TS on stdout, which is byte-for-byte what the recorder consumes in production.
The VOD end-to-end test drives `download_vod` through the same synthetic stream. No API key
and no network access are needed anywhere in the suite.

| File | Covers |
|---|---|
| `test_integration.py` | Chunking, keyframe alignment, remux, proxies, snapshots across a chunk boundary, rolling ASR against a stubbed engine |
| `test_media_timeline.py` | Nonzero-PTS seek alignment, segment-CSV parsing, stream mapping, master validation, concat quoting |
| `test_transcription_completion.py` | Coverage honesty, no-progress bounds, duration caps, slice-seam word preservation |
| `test_seams.py` | Chunk-boundary ownership, idempotence, refusal to act on a bad seam pass, the whole pass against real media |
| `test_publishing.py` | Stale-export retirement, transcript rollback on a failed rebuild, rundown retirement |
| `test_snapshots.py` | Source preference, gap refusal, read leases, queueing and concurrency caps |
| `test_scheduling.py` | Pool separation, merged job view, parallel channel probes, feature-aware `doctor` |
| `test_arming.py` | Record-when-live: arming, firing, cancellation, the startup watchdog |
| `test_quality.py` | Capture-resolution parsing, the Twitch-cap vs. our-setting distinction, and warn/refuse enforcement |
| `test_ui_contract.py` | `hidden` actually hides, no dangling selectors, settings fields map to real config keys |
| `test_recovery.py` | Crash recovery, restart idempotence, disk guards |
| `test_lifecycle.py` | Job draining and cancellation, channel locking, session identity |
| `test_validation.py` | Channel/path/config validation, secret lifecycle, corrupt-config handling |
| `test_server.py` | Dashboard API, Origin/CSRF, body limits, path traversal, secret masking |
| `test_ads.py` | Guards against reintroducing log-derived ad exclusion |
| `test_hardening.py` | Secret redaction in logs, summary policy, per-artifact errors |
| `test_transcript.py`, `test_exports.py` | Transcript model, censor matching, Premiere exports |
| `test_vod.py` | VOD URL parsing, download-command shape, source provenance, and an end-to-end VOD run through the full pipeline |
| `test_network_proxy.py`, `test_arming.py` | Proxy validation and streamlink wiring for live capture, VODs and live probes |
| `test_edit.py` | What the edited cut removes and never removes; the arithmetic that keeps its audio and video from drifting apart; PCM assembly, crossfades and mutes |
| `test_edit_pipeline.py` | Edit scheduling, the seam gate, adoption, staleness, manual re-cut, refusals |
| `test_premiere_schema.py` | Every export validated against Adobe's own checked-in schema file, reading its enums rather than paraphrasing them |
| `test_live_failures_20260816.py` | Regressions for the seven defects the first two live recordings found — capture stream mapping, coverage tolerances, proxy disk reservation, lock scope, rundown retry |
| `test_closed_findings_20260815.py` | Regressions for the audit/product findings closed 2026-08-15 |

---

## Layout

| File | Role |
|---|---|
| `vodpipe/recorder.py` | streamlink → ffmpeg segmenter (live or VOD), write-head tracking, ad markers, disk guards |
| `vodpipe/media.py` | every ffmpeg/streamlink invocation (incl. proxy and VOD download) and the timestamp conventions they share |
| `vodpipe/pipeline.py` | orchestration: recorder events → background jobs; channel watcher; `download_vod` |
| `vodpipe/transcribe.py` | rolling slice scheduling, chunk-boundary repair, publishing |
| `vodpipe/transcript.py` | word model, pause segmentation, slice and chunk seams, censor matching |
| `vodpipe/exports.py` | Static Transcript JSON, SRT, text, censor list |
| `vodpipe/edit.py` | what the edited cut removes and why — pure, no I/O |
| `vodpipe/audio.py` | sample-exact PCM assembly, crossfades and mutes |
| `vodpipe/render.py` | running an edit plan: encode, assemble, mux, verify |
| `vodpipe/asr.py` | Deepgram client |
| `vodpipe/summarize.py` | the rundown prompt |
| `vodpipe/models.py` | `claude -p` and Anthropic API transports, with retry and truncation checks |
| `vodpipe/config.py`, `schema.py` | layered defaults, secrets, and total transactional validation |
| `vodpipe/snapshot.py` | early cut geometry |
| `vodpipe/server.py` | dashboard API |
| `vodpipe/state.py` | session/chunk model, persistence, crash recovery |
| `vodpipe/jobs.py` | background worker pool |
| `vodpipe/cli.py` | command line |

---

## Reliability behaviour

What happens when things go wrong, since a recorder is judged on its bad days:

- **Stopping** kills streamlink only, so ffmpeg reads EOF and finalises the chunk it is
  on. The final tail and its true duration survive. Ctrl+C on the dashboard exits
  through normal control flow and drains queued work before the process ends. Stopping a
  channel that is merely armed cancels the pending request instead.
- **A recording only ever starts against a live channel**, and abandons the attempt if no
  video arrives at all. A session at `recording` means video is being written.
- **Stopping is not a failure.** Terminating streamlink closes the pipe under ffmpeg
  mid-packet, so ffmpeg exits `AVERROR_INVALIDDATA`. That is the consequence of stopping,
  not a bad recording, and it is no longer folded into the session status — every
  hand-stopped session used to be marked `failed` while holding complete, validated
  masters. An ffmpeg exit *nobody asked for* is still surfaced, and per-chunk failures are
  recorded against the chunk either way.
- **A transcript is only marked complete once its coverage reaches the chunk's duration.**
  Falling behind — an API outage, a late key, queue congestion — leaves it explicitly
  incomplete rather than published as finished. Catch-up is bounded and stops the moment
  a pass fails to advance.
- **A `.ts` is deleted only after its MP4 validates** (readable video stream, plausible
  duration). A failed or incompatible remux keeps the recording.
- **Masters are stream-copy H.264 with explicit stream mapping.** A non-H.264 source is
  refused rather than silently re-encoded or dropped, and extra audio tracks are carried
  through instead of being lost to ffmpeg's default selection.
- **Publishing a transcript replaces the whole set.** If a re-transcription legitimately
  comes back empty, the previous `premiere.json`, `.srt` and censor list are removed
  rather than left for Premiere to import. If it *fails*, nothing is touched: the words
  file is restored from its stash and the exports are rebuilt from it, so a failed
  attempt costs you nothing.
- **A transcript that could not be read back is never written.** `words.json` is checked
  against its own reader before it is serialised, so a publish that would produce a file
  nothing can load fails instead — leaving the previous outputs in place. A file that
  will not load is worse than a failed publish: recovery, `retranscribe` and the edited
  cut's freshness check all read it, and the last of those would otherwise re-encode the
  same chunk on every start.
- **A rundown does not outlive the transcript it describes.** If the transcript no longer
  has enough speech to summarise, the stale `rundown.md` goes with it. Turning summaries
  off does not delete work already done — that is a different thing entirely.
- **A rundown gets more than one attempt.** `summary.max_retries` (default 3, all
  attempts sharing `summary.timeout_seconds`) applies to both engines. `claude -p` runs
  against your Claude subscription and therefore shares its usage limits, so a busy
  recording day can produce a transient refusal; one of those should not be the end of
  the rundown. If every attempt fails, the error shown is whatever the engine actually
  said, on stdout or stderr.
- **A network stall does not kill the recording.** Only video and audio are captured.
  Twitch also sends a `timed_id3` metadata stream whose timestamps are not corrected
  across an HLS sequence gap; copying it meant one dropped connection could abort ffmpeg
  mid-recording. It was discarded at remux anyway, so nothing is lost by never taking it.
  Corrupt input packets are dropped rather than treated as fatal.
- **Restarting reconciles what is on disk.** Interrupted recordings get remuxed, stale
  `.partial` files are removed, invalid masters are rebuilt, and a second restart does
  nothing further.
- **Disk guards**: no new chunk below the floor, session aborts below the hard reserve,
  and proxy/remux work is refused rather than filling the drive under a live recording.
- **One recorder per channel**, enforced in-process and by a lock file across processes.

## Known limits

- No automated ad removal. See the Ads section — that is a deliberate correction, not an
  omission.
- Copy-mode snapshots start on a keyframe, so expect up to ~2s of lead-in unless you tick
  frame-exact (which re-encodes and is slower). The reported duration is the file's, so
  it will read slightly longer than the range you asked for.
- Chunk-boundary repair costs one extra ASR request per boundary and runs after the new
  chunk's transcript is complete. The previous chunk's rundown is not regenerated for it.
- Adobe's transcript language list is a closed enum of 29 codes. A regional variant it
  does not list resolves to the same language (`fr-ca` → `fr-fr`, `en-au` → `en-gb`); a
  language it does not support at all is written `??-??`, Adobe's own value for
  "unknown", rather than being relabelled `en-us`. Premiere will not offer text-based
  editing for a `??-??` transcript — that is preferable to claiming English speech that
  is not English.
- Fillers are transcribed but never tagged, so Premiere's *delete all fillers* finds
  nothing. That is deliberate; see the Premiere section — the edited cut removes them
  instead.
- The edited cut is a re-encode, so it costs roughly forty minutes and ~8 GB per 2-hour
  1080p60 chunk and is one generation removed from the stream copy. Turn `edit.enabled`
  off if you only want masters.
- The edited cut makes **hard cuts on the picture**. That is what removing dead air looks
  like; only the audio is blended. There are no dissolves and no B-roll — it is a tightened
  version of the recording, not a produced video.
- It removes dead air and disfluency and nothing else. It has no opinion about which parts
  of a broadcast are worth keeping, by design.
- No proxy is generated for the edited file. Attach the master's proxy to the master; the
  edited file has different timings and needs its own if you want one.
- The rundown is only as good as the transcript. Crosstalk, music and heavy accents all
  degrade it, and the prompt instructs the model to say so rather than guess.
- `claude -p` shares your subscription usage limits.
- No `.epr` proxy ingest preset is shipped — see the Premiere section for why and for
  what is shipped instead.
- The dashboard has no authentication and therefore refuses to bind anywhere but
  loopback. It checks `Origin`/`Host` and requires JSON content type, which stops a
  malicious web page driving it, but anyone with an account on this machine can use it.
