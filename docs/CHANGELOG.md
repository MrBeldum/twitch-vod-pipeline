# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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

[1.0.0]: https://github.com/MrBeldum/twitch-vod-pipeline/releases/tag/v1.0.0
