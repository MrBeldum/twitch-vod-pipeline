<p align="center">
  <img src="docs/logo.png" alt="VOD Pipeline" width="120" height="120">
</p>

<h1 align="center">Twitch VOD → Premiere Pipeline</h1>

<p align="center">
  <b>Record a Twitch stream. Get back a Premiere-ready master, a proxy, and a
  word-timed transcript you can cut from.</b>
</p>

<p align="center">
  <a href="https://github.com/MrBeldum/twitch-vod-pipeline/actions/workflows/ci.yml"><img src="https://github.com/MrBeldum/twitch-vod-pipeline/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue.svg" alt="MIT licensed"></a>
  <a href="https://github.com/MrBeldum/twitch-vod-pipeline/releases/latest"><img src="https://img.shields.io/github/v/release/MrBeldum/twitch-vod-pipeline?label=release" alt="Latest release"></a>
  <img src="https://img.shields.io/badge/python-3.12%20%7C%203.13%20%7C%203.14-3776ab.svg" alt="Python 3.12+">
  <img src="https://img.shields.io/badge/dependencies-none-success.svg" alt="No dependencies">
  <img src="https://img.shields.io/badge/platform-Windows-0078d4.svg" alt="Windows">
</p>

---

Records Twitch streams live in chunks — or downloads a past VOD through the identical
pipeline — and hands you everything you need to start editing:

- a **Premiere-ready master**, stream-copied from the broadcast so nothing is re-encoded;
- its **proxy**, named so Premiere's *Attach Proxies* finds it;
- a **Static Transcript JSON** with per-word timings, which is what turns on text-based
  editing;
- an editor **report** (`report.md`) of what happened, with timestamps, best
  moments, Shorts candidates, and chat-backed evidence of what landed;
- the **Deepgram word stream** the rest is derived from, and the **verbatim API responses**
  beside it;
- **SRT** captions and a plain-text transcript;
- a **censor list** of the terms from your master list that actually occur.

You can also pull any range out of a broadcast while it is still recording, and route
everything through an HTTP/SOCKS proxy to reach Twitch from a region it has withdrawn from.

It records and it transcribes. **It does not cut**: no automatic edit, no decisions made
on your behalf about what to remove. What it gives you is a clean master and a transcript
good enough to cut from.

## Requirements

| | | |
|---|---|---|
| **Python 3.12+** | required | Standard library only — no `pip install`, no virtualenv, no wheels to build. |
| **ffmpeg / ffprobe** | required | Segmenting, remux, proxies, audio extraction, master verification. |
| **streamlink 8.4+** | required | Live capture and VOD download. |
| **A Deepgram API key** | for transcripts | Everything downstream of the master derives from it. |
| **`claude` or `grok` CLI** | for reports | Optional. Uses a subscription you already have; no API key. |
| **A Twitch OAuth token** | optional | Enables the ad-free path if you have Turbo. |

Windows is the supported and tested platform. `python -m vodpipe doctor` tells you what it
can find and what is missing.

## Contents

- [Quick start](#quick-start) · [What lands on disk](#what-lands-on-disk) · [Downloading a past VOD](#downloading-a-past-vod)
- [Getting it into Premiere](#getting-it-into-premiere) — **read this if a transcript "does not work"**
- [What you get, and what you do with it](#what-you-get-and-what-you-do-with-it) · [How it works, and why](#how-it-works-and-why)
- [Ads](#ads) · [Why a recording can be 720p](#why-a-recording-can-be-720p) · [Recording from a withheld-source region](#recording-from-a-withheld-source-region)
- [Disk safety](#disk-safety) · [Configuration](#configuration) · [Tests](#tests) · [Layout](#layout)
- [Reliability behaviour](#reliability-behaviour) · [Known limits](#known-limits) · [Contributing](#contributing) · [License](#license)

---

## Quick start

```
vodpipe.cmd doctor          # check the environment
vodpipe.cmd install         # compile VODPipeline.exe and register it with Windows
vodpipe.cmd                 # open the desktop app (Chromium/Chrome window)
```

After `install`, Windows shows **VOD Pipeline** in the Start Menu and in
Settings → Apps. Double-click `VODPipeline.exe`, or `Start VOD Pipeline.vbs`,
for the same thing without a console. The window is Chromium or Google Chrome
in app mode — never Edge.

`vodpipe.cmd dashboard` still runs the local web server only, if you want a
browser tab instead of a window.

Then in the dashboard: paste your Deepgram key under **Settings**, add a channel, and
either press **Record** or leave **auto** ticked so it starts on its own when that
channel goes live. **Refresh** (R or F5) re-checks live status immediately rather
than waiting for the watcher interval; the page itself does not reload.

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
vodpipe.cmd republish <session-id>        # rebuild exports from the stored words
```

---

## What lands on disk

```
Desktop\twitch-vods\<channel>\<session-id>\
  index.md                 ← start here: what to open, in the order you open it
  master\                  ← import this folder into Premiere
    <channel>_<session>_c000.mp4          ← the untouched recording
    Proxies\
      <channel>_<session>_c000_Proxy.mp4  ← auto-deleted after 1 day
  transcripts\
    c000\                  ← only the four things you actually open
      report.md            ← editor report: read this first
                             (timeline, best moments, Shorts, titles)
      premiere.json        ← Static Transcript, enables text-based editing
      transcript.srt       ← captions
      censor-words.txt     ← terms from your master list that actually occur
      source\              ← what the pipeline reads and you rarely do
        words.json         ← the word stream every export is rebuilt from
        transcript.json    ← segments + words, for anything you build yourself
        transcript.txt     ← timestamped plain text
        chat.json          ← Twitch chat for this chunk (live IRC or VOD comments)
        chat.txt           ← the same, readable
        moments.json       ← content-aware chat peaks (laugh / hype / clip-call)
        exports.json       ← which generation the files beside it belong to
        deepgram\          ← the provider's verbatim responses, one per request
          0001.json  0002.json  ...
  snapshots\
    <channel>_<session>_snap_..._001432.mp4
    snapshots.json
  live\                    ← working .ts files, removed once remuxed
  logs\
  session.json             ← machine-readable state
```

**The split is by how often you open a file, not by what kind of file it is.** A
folder is browsed, not searched, so everything sitting in a chunk folder competes
for attention with `premiere.json` — the one file that has to be easy to find. The
four at the top are the ones an edit actually starts from; `source/` holds the
plumbing, which is there so a transcript can be rebuilt or checked, not so you can
read it.

Both halves are written by a single transaction, so they can never disagree: a
crash mid-publish cannot leave a `premiere.json` describing words that
`source/words.json` no longer contains.

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
an empty session; set `network.proxy` for the geo-blocked case (see *Recording from a
withheld-source region*).

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
> make each one easy to find, which a verbatim transcript does.

Turn `transcription.filler_words` off if you would rather have clean reading copy than a
verbatim transcript. That one is a Deepgram request parameter, so changing it does
require re-transcribing — Deepgram was previously told not to write those sounds down.

**Censoring.** Premiere censors from a word list you supply; nothing inside the
transcript file drives it. Open `censor-words.txt`, copy the terms, and paste them into
Text panel → Transcript → Filter → **Censored words**. The file only lists terms from
your master list that were actually spoken in that chunk, so it stays short.

---

## What you get, and what you do with it

Everything below lands per chunk, in `transcripts/<chunk>/`. Nothing here is a judgement
about your footage — that is the point.

**In the chunk folder — the four you open:**

| File | What it is | What it is for |
|---|---|---|
| `report.md` | editor's cut list: timeline, best moments, Shorts, titles | read it first and you know what to cut |
| `premiere.json` | Adobe Static Transcript, per-word timings | import it and text-based editing works |
| `transcript.srt` | captions | subtitles, or an import into anything that reads SRT |
| `censor-words.txt` | terms from your master list that actually occur | paste into Premiere's censored-words filter |

**In `source/` — what the pipeline reads:**

| File | What it is | What it is for |
|---|---|---|
| `words.json` | the Deepgram word stream, normalised | every other file is derived from this one; `republish` rebuilds them all from it without touching Deepgram |
| `deepgram/NNNN.json` | the provider's answers, verbatim | what was actually said back, before we made anything of it |
| `transcript.txt` | timestamped plain text | searching, quoting, skimming |
| `chat.json` / `chat.txt` | Twitch chat for this chunk | live via IRC, VODs via Twitch's comments API (same as TwitchDownloader) |
| `moments.json` | content-aware chat peaks | laugh emotes, copypasta, clip-calls — not just messages/second |
| `transcript.json` | segments and words | for anything you build yourself |
| `exports.json` | which generation these files describe | lets the pipeline tell a stale export set from a current one |

You never have to go into `source/` to edit. It is there so a transcript can be rebuilt,
checked against what the provider actually returned, or read a different way later.

### Why both `words.json` and `deepgram/`

`words.json` is the stream everything else is built on, and it is *normalised*: sorted,
de-overlapped, and with same-start collisions resolved by dropping one of the pair.
Downstream that is exactly what you want — but it means the file is not quite what
Deepgram said.

`deepgram/` keeps what Deepgram said, one file per request, before anything was made of
it. It answers the question the normalised file cannot: *did we lose that word, or was it
never there?* It also lets a future reading of the same responses be built without paying
for the audio again. About 15 KB per request, so roughly 1.5 MB for a two-hour chunk;
turn it off with `transcription.keep_raw_responses` if you would rather not have it.

### There is no automatic edit

An earlier version of this tool cut a second copy of every chunk with the silences,
fillers and false starts removed. It worked — measured on a real chunk it took 58:52 down
to 46:15, it never clipped a word, and its joins did not click. It was **removed deliberately**, and
the reasoning is worth keeping:

- an automatic cut has to be *checked*, and checking a 46-minute file means watching it;
- the cost is real and permanent — forty minutes of encoding and several gigabytes per
  chunk, for a derivative one generation removed from a stream copy;
- and the thing it saves you is the easy part. Finding the dead air is not what makes
  editing slow.

What survives is the part that was actually useful: a **verbatim** transcript, with the
hesitations in it, timed to the word, so a cut you make lands where you expect it to.
If you want an automatic cut, that lives in a separate, unpublished tool — this same
pipeline with the cut still in it, decided by a language model. It is not part of this
project and is not on the roadmap for it.

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

**Reports** run through Claude Code (`claude -p`) or Grok Build (`grok -p`) — see below.

**Chat** is captured for live broadcasts over IRC and for VODs through the same GraphQL
comments API TwitchDownloader uses. Per-chunk `source/chat.json` plus a content-aware
moment analysis (laugh emotes, copypasta, clip-calls — not just chat speed) are handed
to the report engine. A chat failure never fails a recording.

### Report engine

A report is one background call per chunk. Two engines, both local subscription CLIs:

| `summary.provider` | What it spends | Needs |
|---|---|---|
| `claude-cli` *(default)* | your Claude subscription, via `claude -p` | the `claude` executable |
| `grok-cli` | your Grok subscription, via `grok -p` (Grok 4.6 unless you name another model) | the `grok` executable |
| `none` | nothing — reports off | |

`summary.model` is passed through as `--model`. Leave it blank: Claude Code picks from
the subscription; Grok uses its CLI default, which is currently **Grok 4.6**
(reported in usage as `grok-4.6-build`). The old alias `grok-build` is not a valid
model id on Grok CLI 1.0.5 and is rewritten to blank on load.

For Claude Code the transcript arrives on **stdin**. Grok Build does not read stdin —
the prompt is written to a temp file and passed as `--prompt-file`, with `--cwd` pointed
at an empty directory so Grok does not walk this repository. A two-hour transcript is
far past the Windows command-line length limit either way. The report is whatever the
CLI writes to **stdout**. A failed attempt is retried up to `summary.max_retries` times
(default 3), all attempts sharing the one `summary.timeout_seconds` deadline — a
non-zero exit is not classifiable from outside, so the retry is unconditional and bounded.

**Paid HTTP APIs were tried and withdrawn.** On 2026-08-18 a seven-hour recording hit
`You’ve hit your session limit` and lost that chunk’s report, so the engine was made
pluggable across the Anthropic, Kimi, DeepSeek and OpenAI APIs. The paid APIs then
failed on the ordinary case, twice in one night: Kimi refused one report for exceeding
an organization concurrency of **one** — against a pipeline whose whole shape is
background jobs — and refused the next as `high risk` content, having been handed a
Twitch transcript. They were removed on 2026-08-19. A fallback that fails on the
ordinary case is not a fallback. `grok-cli` is a second *subscription CLI*, the same
shape as `claude-cli`, not a paid HTTP API.

If your `config.json` still names one of the removed APIs, it is rewritten to `claude-cli`
on load rather than refused, and the API keys beside it are dropped from the file on the next
save.

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

A session recorded on 2026-08-13 from a large English-language channel produced four
chunks at **1280×720**. The
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
| Several of the largest channels | `2025-Transcode-ELT-V1` | **720p60** |
| Others | `Custom:1_Source_Max_1440p60…:5_Transcode_Max_1080p60…` | 1080p60 |

That second row is why the cap is easy to misdiagnose: some channels *do* reach 1080p60
from an affected region, so a spot check on the wrong channel suggests everything is
fine.

Things that were tested and made **no** difference:

- `--twitch-supported-codecs h264,h265,av1` — identical ladder, so this is not Twitch's
  HEVC/AV1 enhanced-broadcasting tiers being filtered out.
- `--twitch-force-client-integrity` — identical ladder.
- Subscriber-gated quality — streamlink logs `"requires a subscription"` when that is the
  cause, and that warning appears nowhere in 43,000 lines of the session log.
- Being a RERUN. Twitch does cap reruns, but these were genuine live broadcasts.

**So: to record at source quality from an affected region, route Twitch through another
one** — either a full VPN or the built-in `network.proxy` setting (an HTTP/SOCKS proxy
applied to every streamlink call; see *Recording from a withheld-source region*). A Twitch OAuth token is still worth
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

## Recording from a withheld-source region

Twitch withholds the `source` rendition from IPs in some countries it no longer operates
in. **South Korea, which it left in February 2024, is the documented case**, and the one
the evidence below was gathered from. From such an IP the best you can record is whatever
transcode the channel happens to have — 720p60 on many large channels. The full evidence
is in "Why a recording can be 720p" above. There are two ways to get back to source
quality, and both work for live capture *and* VOD downloads:

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

**A full VPN — the heavy option.** A VPN endpoint in an unaffected region (Japan was the
one tested, on 2026-08-14) restores source quality at 1080p60. It routes the whole machine
and needs no pipeline setting.

An earlier version of this file claimed capture worked at full quality from an affected
region without either. It does not. That conclusion came from spot-checking channels that
happened to have a 1080p60 *transcode*, which masked the missing source rendition.

Everything else is geography-neutral: Deepgram is reachable globally, `claude -p` runs
locally against your subscription, and the dashboard is loopback-only. Two things to
keep in mind when recording over a VPN:

- **Throughput matters.** Source 1080p60 is roughly 7 GB/hr sustained. A VPN endpoint
  that cannot hold that will show up as buffering and dropped segments, not as an error.
- **A VPN drop is survivable.** streamlink is configured to retry indefinitely
  (`--retry-streams 5 --retry-max 0`), so a brief reconnection is waited out rather than
  ending the session. The startup watchdog only fires when *zero* bytes have ever
  arrived, so it will not kill a recording that has already started.

Twitch Turbo remains the pipeline's only ad avoidance, and a billing address in a region
Twitch has withdrawn from may affect buying it. That costs coverage at ad breaks, not
resolution. See the Ads section.

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

Secrets resolve config first, then environment (`DEEPGRAM_API_KEY`,
`TWITCH_OAUTH_TOKEN`), so a shell export works too.

The validator is total and transactional — a rejected save changes nothing — and it is
strict about unknown keys, so a typo is refused rather than silently ignored. The one
exception is a *retired* key: `edit.*` settings written by the build that had the
automatic cut are dropped on load rather than refused, so switching to this build does
not leave you with an application that will not start on its own settings file. They are
gone from the file after the next save.

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
| `test_retirement.py` | That a `config.json` and a `session.json` written by the build that had the edited cut still load here, and that the retired names disappear on the next save |
| `test_raw_responses.py` | The verbatim Deepgram archive, and that a failure to write it can never lose a transcription that succeeded |
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
| `vodpipe/asr.py` | Deepgram client |
| `vodpipe/summarize.py` | the rundown prompt |
| `vodpipe/models.py` | the `claude -p` transport, with its retry and its deadline |
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
- **A `.ts` is deleted only after its MP4 is read from end to end.** The header check
  — readable video stream, plausible duration, matching stream inventory — is not enough
  on its own: on 2026-08-18 two masters passed it while carrying a single wrong byte in
  their index, one silently truncating playback to 44% of the file and the other
  misaddressing every sample after a certain point. Both looked perfect to `ffprobe`'s
  summary, and both had their `.ts` deleted. So before a master is published, its packets
  are counted: it must read without ffmpeg reporting a single error, and its video must
  deliver as many frames as the file itself claims to hold. One full pass, 3–14s for a
  two-hour chunk here against a 30–40s remux, and it is the only check that can see this.
  `recording.verify_master` turns it off; if you do that, turn on
  `recording.keep_ts_after_remux`.
- **A failed remux is tried again** (`recording.remux_attempts`, default 3). A remux is
  deterministic work over bytes already on disk, so a failure is either permanent — and
  costs a bounded couple of minutes to confirm — or a one-off. A one-off used to cost the
  master permanently: one chunk of that same recording died on an ffmpeg assertion, and
  re-running the identical command over the identical bytes afterwards produced a perfect
  master.
- **A short proxy names the master, not the encoder.** If an encode stops early, the
  master is read through before anything is blamed. Both proxies of a damaged master used
  to be reported as "h264_amf failed on real media", fall back to libx264, and spend
  another five minutes proving software could not read the file either — an encoder
  cannot encode frames its input will not hand over.
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
  will not load is worse than a failed publish: recovery and `retranscribe` both read it, and
  neither can do anything with a file that will not parse.
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
  nothing. Premiere ignored the tags when they were written; see the Premiere section.
- **Nothing is cut for you.** No silence removal, no filler removal, no censoring of the
  media — the censor list is a list, and Premiere does the censoring. This build
  produces a master and the material to edit it with, and stops there.
- The rundown is only as good as the transcript. Crosstalk, music and heavy accents all
  degrade it, and the prompt instructs the model to say so rather than guess.
- `claude -p` shares your subscription usage limits.
- No `.epr` proxy ingest preset is shipped — see the Premiere section for why and for
  what is shipped instead.
- The dashboard has no authentication and therefore refuses to bind anywhere but
  loopback. It checks `Origin`/`Host` and requires JSON content type, which stops a
  malicious web page driving it, but anyone with an account on this machine can use it.

---

## Contributing

Contributions are welcome. Start with **[CONTRIBUTING.md](CONTRIBUTING.md)** — it covers
the setup (there is almost none), how to run the suite, and the three failure modes this
codebase actually has.

Two documents are worth reading before proposing a change:

- **[`CLAUDE.md`](CLAUDE.md)** is the project's engineering memory. Every non-obvious
  decision is recorded there with the failure that caused it, including several features
  that were built and then deliberately removed. Most "obvious improvements" have already
  been tried and are written up with the reason they were reverted.
- **[`DESIGN.md`](DESIGN.md)** is the dashboard's design system. The palette is
  contrast-tested in CI, so a colour picked by eye will fail the suite.

Please report security issues privately — see [SECURITY.md](SECURITY.md). By taking part
you agree to the [Code of Conduct](CODE_OF_CONDUCT.md).

## Releases

Every release attaches the compiled `VODPipeline.exe`, an sdist, a wheel and a
`SHA256SUMS.txt`, all built by
[the release workflow](.github/workflows/release.yml) from the tagged commit rather than
uploaded by hand — see **[the latest release](https://github.com/MrBeldum/twitch-vod-pipeline/releases/latest)**
and [CHANGELOG.md](CHANGELOG.md).

The executable is an unsigned .NET Framework 4 host, so Windows SmartScreen warns on first
run. You can compile it yourself with `packaginguild.cmd` — that is exactly what the
workflow does.

## License

[MIT](LICENSE). Use it, change it, ship it.

This project drives `ffmpeg`, `streamlink` and Deepgram; each carries its own licence and
terms, and recording a broadcast is your responsibility under Twitch's terms and the law
where you are.
