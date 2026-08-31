# Contributing

Thanks for wanting to help. This document is short on ceremony and long on the two or
three things that actually go wrong in this codebase.

## Getting set up

There is nothing to install for the Python side — the pipeline is **standard library
only**, on purpose, and it should stay that way. You need:

| Tool | Why | Notes |
|---|---|---|
| Python 3.14+ | everything | no venv needed, no `pip install` |
| `ffmpeg` + `ffprobe` | segmenting, remux, proxies, verification | the test suite really runs them |
| `streamlink` 8.4+ | capture and VOD download | **the tests need it too** — `Pipeline` resolves it at construction |
| `claude` and/or `grok` | editor reports | optional |

```
git clone https://github.com/MrBeldum/twitch-vod-pipeline.git
cd twitch-vod-pipeline
python -m unittest discover -s tests -t .
```

The suite is ~960 tests and takes about two minutes, because a good number of them
generate real media with ffmpeg and read it back. **A test that only asserts against a
hand-written fixture has repeatedly passed while the real thing was broken** — see below —
so tests that touch media are expected to be slow.

`python -m vodpipe doctor` prints what it can find on your machine and why anything is
unavailable. Start there when something will not run.

## Before you open a pull request

1. **Run the full suite.** `python -m unittest discover -s tests -t .` — all green.
2. **Read `CLAUDE.md`.** It is the project's engineering memory: every non-obvious
   decision, with the failure that caused it. Most "obvious improvements" to this codebase
   have already been tried and are written up there with the reason they were reverted.
   Changing something it documents is fine — contradicting it silently is not.
3. **Read `DESIGN.md`** if you are touching the dashboard. The palette is contrast-tested
   in `tests/test_ui_contract.py`; a colour picked by eye will fail the suite.

## The failure modes this project actually has

These are not hypothetical. Each one cost real recordings.

**Validators stricter than reality.** The single most common defect here is a check that
is arithmetically correct and physically impossible to satisfy — an exact duration
comparison against measurements that carry quantisation noise, a word-end time bound
against timings the provider only estimates. Three of the four defects found in the first
live test were this. If you add a comparison between two measured quantities, ask what
the measurement error is, and give it a named tolerance.

**Removing things is more dangerous than adding them.** Config keys and manifest fields
are validated strictly, so deleting one that exists in somebody's installed
`config.json` makes the application refuse to start. Retiring needs
`schema.RETIRED_PATHS`, `state._RETIRED_CHUNK_FIELDS` or `exports.RETIRED_EDIT_EXPORTS`
depending on what you are removing. `tests/test_retirement.py` is the test for this and
should be extended whenever something is retired.

**Nothing that runs for minutes may hold a chunk mutation lock.** That lock is the
transcript-generation lock. A proxy encode once held it for ten minutes and cost a
rundown. Do expensive work unlocked; take the lock for a generation recheck and an atomic
commit.

**Data loss is the only unacceptable outcome.** A `.ts` recording is deleted only after
its MP4 master has been read end to end. A failed publish leaves the previous good
outputs alone. If your change can lose a recording, a transcript or a master, it needs a
test proving it cannot.

## Style

Match the surrounding code. Some specifics that are load-bearing rather than taste:

- **Standard library only.** A dependency is a much bigger cost here than the code it
  saves; the whole install story is "have ffmpeg".
- **Comments explain *why*, and name the failure.** The existing comments are long
  because they record what went wrong. Keep that.
- **One source of truth per question.** Channel-name validation, lock identity, summary
  capability — each is deliberately in exactly one place because two answers disagreed
  once and something broke.

## Reporting bugs

Open an issue with the version, your OS, the relevant lines from `logs/`, and what
`python -m vodpipe doctor` says. If it involves a recording, the `session.json` for that
session is usually the fastest way to explain it — **check it for a channel name or a path
you would rather not publish before pasting it.**

Never paste `config.json`: it holds your Deepgram key and Twitch OAuth token.

## Security

Please do not open a public issue for a security problem. See [SECURITY.md](SECURITY.md).
