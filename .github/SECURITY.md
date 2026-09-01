# Security Policy

## Supported versions

The latest release on `main` is the only supported version.

## Reporting a vulnerability

Please report security issues **privately**, through GitHub's
[private vulnerability reporting](https://github.com/MrBeldum/twitch-vod-pipeline/security/advisories/new)
rather than a public issue.

Include what an attacker can do, how to reproduce it, and the version. You will get an
acknowledgement within a few days; this is a small project, so please be patient with the
fix timeline.

## Scope and threat model

The dashboard **has no authentication**, and this is deliberate rather than an oversight:
it refuses to bind to anything but loopback, checks `Origin` and `Host`, and requires a
JSON content type, so a malicious web page cannot drive it. **Anyone with an account on
the machine can.** Reports that amount to "another local user could use the dashboard"
are working as designed.

Things that are in scope and worth reporting:

- Anything that lets a remote page or host reach the dashboard API.
- Path traversal or absolute-path handling that lets crafted state on disk cause a write
  or a delete outside the session directory. Artifact names read from `session.json` are
  treated as untrusted for exactly this reason.
- Leaking `secrets.deepgram_api_key` or `secrets.twitch_oauth_token` into a log, an
  export, a dashboard response, or a subprocess argument list.
- Anything that turns a crafted Twitch channel name, VOD id, chat message or ASR response
  into command execution or a write outside the media tree.

## Your own data

`config.json` holds your Deepgram API key and Twitch OAuth token. It is gitignored and
must never be committed. If you think you have committed one, revoke the key at the
provider first and rewrite history second — revocation is what actually protects you.
