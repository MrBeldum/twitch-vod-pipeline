"""Dashboard API tests, including the guards that matter on a loopback server."""

from __future__ import annotations

import http.client
import json
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from unittest.mock import patch

from vodpipe.config import Config, DEFAULTS, deep_merge
from vodpipe.pipeline import Pipeline
from vodpipe.server import serve

PORT = 8477


def request(path: str, payload=None, *, headers=None, raw: bytes | None = None):
    url = f"http://127.0.0.1:{PORT}{path}"
    sent = dict(headers or {})
    if payload is None and raw is None:
        req = urllib.request.Request(url, headers=sent)
    else:
        body = raw if raw is not None else json.dumps(payload).encode()
        sent.setdefault("Content-Type", "application/json")
        req = urllib.request.Request(url, data=body, method="POST", headers=sent)
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            return response.status, response.read(), dict(response.headers)
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, exc.read(), dict(exc.headers)
        finally:
            exc.close()


def json_request(path: str, payload=None, **kwargs):
    status, body, _ = request(path, payload, **kwargs)
    return status, json.loads(body)


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="vodpipe-http-"))
        data = deep_merge(DEFAULTS, {
            "paths": {"masters_root": str(cls.tmp / "masters"),
                      "work_root": str(cls.tmp / "work")},
            "watcher": {"enabled": False},
            "dashboard": {"open_browser": False, "port": 9999},
            "channels": [],
        })
        cls.config = Config(data, cls.tmp / "config.json")
        cls.pipeline = Pipeline(cls.config)
        cls.httpd = serve(cls.pipeline, cls.config, port=PORT, open_browser=False)
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.pipeline.shutdown()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    # -- overrides ---------------------------------------------------------

    def test_port_override_is_not_persisted(self):
        """A one-off --port must not become the stored default."""
        self.assertEqual(self.config.get("dashboard.port"), 9999)

    # -- static ------------------------------------------------------------

    def test_index_is_served(self):
        status, body, headers = request("/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers.get("Content-Type", ""))
        self.assertIn(b"VOD", body)

    def test_assets_are_served(self):
        for path, expected in (("/app.js", "javascript"), ("/style.css", "css")):
            status, _, headers = request(path)
            self.assertEqual(status, 200, path)
            self.assertIn(expected, headers.get("Content-Type", ""))

    def test_unknown_static_is_404(self):
        self.assertEqual(request("/nope.html")[0], 404)

    def test_static_traversal_is_blocked(self):
        self.assertEqual(request("/../config.json")[0], 404)

    # -- api ---------------------------------------------------------------

    def test_state_payload_shape(self):
        status, payload = json_request("/api/state")
        self.assertEqual(status, 200)
        for key in ("channels", "sessions", "jobs", "disk", "capabilities"):
            self.assertIn(key, payload)
        self.assertIn("free_bytes", payload["disk"])

    def test_unknown_endpoint_is_404(self):
        status, payload = json_request("/api/nope")
        self.assertEqual(status, 404)
        self.assertIn("error", payload)

    def test_channel_add_accepts_a_url(self):
        status, payload = json_request("/api/channels/add",
                                       {"name": "https://twitch.tv/SomeOne?tt=1"})
        self.assertEqual(status, 200)
        self.assertEqual(payload["channel"], "someone")
        self.assertIn("someone", self.pipeline.channels())

        json_request("/api/channels/remove", {"name": "someone"})
        self.assertNotIn("someone", self.pipeline.channels())

    def test_channel_add_requires_a_name(self):
        status, payload = json_request("/api/channels/add", {"name": "  "})
        self.assertEqual(status, 400)
        self.assertIn("error", payload)

    def test_channel_endpoints_reject_non_text_without_coercion(self):
        cases = (
            ("/api/channels/add", {"name": 1234}),
            ("/api/channels/remove", {"name": True}),
            ("/api/channels/settings", {"name": ["channel"],
                                         "auto_record": True}),
            ("/api/record/start", {"channel": {"name": "channel"}}),
            ("/api/record/stop", {"channel": 1234}),
        )
        for route, body in cases:
            status, payload = json_request(route, body)
            self.assertEqual(status, 400, route)
            self.assertIn("must be text", payload["error"], route)

    def test_no_channel_is_hardcoded(self):
        """The watch list must start empty -- channels are the user's to choose."""
        status, payload = json_request("/api/state")
        self.assertEqual(payload["channels"], [])

    def test_secrets_are_masked(self):
        self.config.set("secrets.deepgram_api_key", "super-secret-value")
        try:
            status, payload = json_request("/api/config")
            self.assertEqual(status, 200)
            self.assertNotIn("super-secret-value", json.dumps(payload))
            self.assertEqual(payload["secrets"]["deepgram_api_key"], "__unchanged__")
        finally:
            self.config.set("secrets.deepgram_api_key", "")

    def test_masked_secret_does_not_overwrite_the_stored_one(self):
        self.config.set("secrets.deepgram_api_key", "keep-me")
        try:
            json_request("/api/config", {"secrets": {"deepgram_api_key": "__unchanged__"}})
            self.assertEqual(self.config.secret("deepgram_api_key"), "keep-me")
        finally:
            self.config.set("secrets.deepgram_api_key", "")

    def test_config_save_cannot_clobber_the_channel_list(self):
        self.pipeline.add_channel("keeper")
        try:
            json_request("/api/config", {"channels": [], "proxies": {"height": 480}})
            self.assertIn("keeper", self.pipeline.channels())
            self.assertEqual(self.config.get("proxies.height"), 480)
        finally:
            self.pipeline.remove_channel("keeper")

    def test_file_endpoint_does_not_accept_caller_supplied_paths(self):
        path = urllib.parse.quote(r"C:\Windows\win.ini")
        status, payload = json_request(f"/api/file?path={path}")
        self.assertEqual(status, 400)
        self.assertIn("artifact_id", payload["error"])

    def test_unknown_session_is_404(self):
        status, _ = json_request("/api/session?session_id=nope")
        self.assertEqual(status, 404)

    def test_snapshot_requires_a_range(self):
        status, payload = json_request("/api/snapshot", {"session_id": "x"})
        self.assertEqual(status, 400)
        self.assertIn("last_minutes", payload["error"])

    def test_stopping_a_channel_that_is_not_recording_is_a_409_not_a_crash(self):
        status, payload = json_request("/api/record/stop", {"channel": "ghost"})
        self.assertEqual(status, 409)
        self.assertIn("not recording", payload["error"])

    # -- VOD download ------------------------------------------------------

    def test_vod_download_requires_a_url(self):
        status, payload = json_request("/api/vod/download", {})
        self.assertEqual(status, 400)
        self.assertIn("URL", payload["error"])

    def test_vod_download_rejects_a_non_vod_url(self):
        status, payload = json_request(
            "/api/vod/download", {"url": "https://twitch.tv/somechannel"})
        self.assertEqual(status, 409)
        self.assertIn("VOD", payload["error"])

    def test_vod_download_happy_path_is_accepted(self):
        from vodpipe.state import Session, SOURCE_VOD
        fake = Session(session_id="teststreamer_2026-01-01_000000_abcdef",
                       channel="teststreamer", started_at=1.0,
                       directory=str(self.tmp / "s"), status="recording",
                       source_kind=SOURCE_VOD,
                       source_url="https://www.twitch.tv/videos/1")
        with patch.object(self.pipeline, "download_vod",
                          return_value=fake) as mock:
            status, payload = json_request(
                "/api/vod/download",
                {"url": "https://www.twitch.tv/videos/1", "start": 60})
        self.assertEqual(status, 202)
        self.assertEqual(payload["session_id"], fake.session_id)
        self.assertEqual(mock.call_args.kwargs.get("start"), 60.0)

    def test_vod_stop_for_an_unknown_session_is_a_409(self):
        status, payload = json_request("/api/vod/stop", {"session_id": "nope"})
        self.assertEqual(status, 409)
        self.assertIn("no active VOD", payload["error"])

    # -- record means "when there is something to record" -------------------

    def test_recording_an_offline_channel_arms_it_rather_than_starting(self):
        """202, not 200: nothing has begun, and the answer must not imply it has."""
        try:
            status, payload = json_request("/api/record/start",
                                           {"channel": "definitelyoffline"})
            self.assertEqual(status, 202)
            self.assertEqual(payload["state"], "armed")
            self.assertEqual(payload["session_id"], "")
            self.assertTrue(self.pipeline.is_armed("definitelyoffline"))
        finally:
            self.pipeline.disarm("definitelyoffline")

    def test_an_armed_channel_is_visible_in_the_state_payload(self):
        try:
            json_request("/api/record/start", {"channel": "pendingchannel"})
            _, payload = json_request("/api/state")
            entry = next(item for item in payload["channels"]
                         if item["name"] == "pendingchannel")
            self.assertTrue(entry["armed"])
            self.assertFalse(entry["recording"])
        finally:
            self.pipeline.disarm("pendingchannel")

    def test_stop_cancels_a_pending_request(self):
        json_request("/api/record/start", {"channel": "cancelme"})
        self.assertTrue(self.pipeline.is_armed("cancelme"))

        status, _ = json_request("/api/record/stop", {"channel": "cancelme"})
        self.assertEqual(status, 200)
        self.assertFalse(self.pipeline.is_armed("cancelme"))

    def test_an_invalid_channel_name_is_still_refused(self):
        status, _ = json_request("/api/record/start", {"channel": "../evil"})
        self.assertEqual(status, 400)

    # -- queued work (AUD-025) ---------------------------------------------

    def add_session(self, session_id: str, duration: float = 60.0):
        """A session with placeholder media, enough for the geometry to resolve."""
        import time as _time

        from vodpipe.state import Chunk, Session

        directory = self.tmp / "masters" / "chan" / session_id
        (directory / "master").mkdir(parents=True, exist_ok=True)
        session = Session(session_id=session_id, channel="chan",
                          started_at=_time.time(), directory=str(directory),
                          status="complete")
        chunk = Chunk(index=0, session_id=session_id, channel="chan",
                      started_at=_time.time(), ts_name="chan_c000.ts",
                      master_name="chan_c000.mp4", duration=duration,
                      status="complete")
        (directory / "master" / chunk.master_name).write_bytes(b"x" * 2048)
        session.chunks.append(chunk)
        self.pipeline.store.add(session)
        return session

    def test_a_snapshot_is_accepted_rather_than_cut_on_the_request_thread(self):
        """The regression: the browser waited on an ffmpeg encode."""
        session = self.add_session("queued-cut")
        status, payload = json_request(
            "/api/snapshot", {"session_id": session.session_id,
                              "start": 0.0, "end": 5.0, "transcribe": False})
        self.assertEqual(status, 202)
        self.assertTrue(payload["queued"])
        self.assertIn("job", payload)

    def test_a_snapshot_of_an_unknown_session_is_a_409_not_a_crash(self):
        status, payload = json_request(
            "/api/snapshot", {"session_id": "no-such-session", "last_minutes": 1})
        self.assertEqual(status, 409)
        self.assertIn("unknown session", payload["error"])

    def test_a_snapshot_past_the_end_of_the_recording_is_refused_up_front(self):
        session = self.add_session("short-cut", duration=20.0)
        status, payload = json_request(
            "/api/snapshot", {"session_id": session.session_id,
                              "start": 500.0, "end": 600.0, "transcribe": False})
        self.assertEqual(status, 409)

    def test_non_positive_snapshot_ranges_are_refused_at_admission(self):
        # P9: a zero/negative window is a client mistake, a 400 not a 500.
        session = self.add_session("bad-range")
        for body, needle in (
            ({"last_minutes": 0}, "last_minutes"),
            ({"last_minutes": -3}, "last_minutes"),
            ({"start": -1.0, "end": 5.0}, "start"),
            ({"start": 5.0, "end": 5.0}, "after"),
            ({"start": 10.0, "end": 4.0}, "after"),
        ):
            status, payload = json_request(
                "/api/snapshot",
                {"session_id": session.session_id, "transcribe": False, **body})
            self.assertEqual(status, 400, body)
            self.assertIn(needle, payload["error"])

    def test_snapshot_name_is_bounded_before_work_is_queued(self):
        session = self.add_session("long-snapshot-name")
        status, payload = json_request(
            "/api/snapshot", {"session_id": session.session_id,
                              "start": 0.0, "end": 5.0,
                              "name": "x" * 121, "transcribe": False})
        self.assertEqual(status, 400)
        self.assertIn("too long", payload["error"])
        self.assertIn("120", payload["error"])

    def test_snapshot_name_limit_counts_windows_utf16_units(self):
        session = self.add_session("unicode-snapshot-name")
        status, payload = json_request(
            "/api/snapshot", {"session_id": session.session_id,
                              "start": 0.0, "end": 5.0,
                              "name": "\U0001f600" * 61,
                              "transcribe": False})
        self.assertEqual(status, 400)
        self.assertIn("too long", payload["error"])

    def test_retranscribing_a_chunk_with_no_key_is_a_409(self):
        session = self.add_session("no-key")
        status, payload = json_request(
            "/api/chunk/retranscribe", {"session_id": session.session_id,
                                        "chunk": "c000"})
        self.assertEqual(status, 409)
        self.assertIn("Deepgram", payload["error"])

    def test_retranscribing_an_unknown_chunk_is_a_404(self):
        session = self.add_session("no-chunk")
        status, _ = json_request(
            "/api/chunk/retranscribe", {"session_id": session.session_id,
                                        "chunk": "c999"})
        self.assertEqual(status, 404)

    def test_a_rundown_request_is_accepted(self):
        from vodpipe.exports import write_exports
        from vodpipe.state import DONE
        from vodpipe.transcript import Word

        session = self.add_session("rundown")
        chunk = session.chunks[0]
        output = self.pipeline.transcriber.output_dir(session, chunk)
        words = [Word(f"word-{index}", index * 0.5, 0.25, 0.9)
                 for index in range(25)]
        write_exports(
            output, words,
            meta={"chunk": chunk.label, "complete": True},
            words_meta={
                "chunk": chunk.label, "complete": True,
                "covered_seconds": chunk.duration,
                "expected_seconds": chunk.duration,
            },
        )
        chunk.transcript_status = DONE
        chunk.word_count = len(words)
        from vodpipe.jobs import Job
        with patch.object(
                self.pipeline, "_queue_summary",
                return_value=Job("summary:test", "test rundown", "summary")):
            status, payload = json_request(
                "/api/chunk/summarize", {"session_id": session.session_id,
                                         "chunk": "c000"})
        self.assertEqual(status, 202)
        self.assertTrue(payload["queued"])

    # -- trust boundary (AUD-024, AUD-025) ---------------------------------

    def test_anti_framing_headers_are_present(self):
        _, _, headers = request("/")
        self.assertEqual(headers.get("X-Frame-Options"), "DENY")
        self.assertIn("frame-ancestors 'none'",
                      headers.get("Content-Security-Policy", ""))

    def test_cross_origin_post_is_refused(self):
        """A page on another site must not be able to drive the dashboard."""
        status, payload = json_request(
            "/api/channels/add", {"name": "someone"},
            headers={"Origin": "https://evil.example"})
        self.assertEqual(status, 403)
        self.assertIn("cross-origin", payload["error"])

    def test_unexpected_host_is_still_refused_with_json(self):
        status, payload = json_request(
            "/api/state", headers={"Host": "attacker.example"})
        self.assertEqual(status, 403)
        self.assertIn("Host", payload["error"])

    def test_same_origin_post_is_allowed(self):
        status, _ = json_request("/api/channels/add", {"name": "okchannel"},
                                 headers={"Origin": f"http://127.0.0.1:{PORT}"})
        self.assertEqual(status, 200)
        json_request("/api/channels/remove", {"name": "okchannel"})

    def test_non_json_content_type_is_refused(self):
        """Blocks the simple-request content types a cross-site form can send."""
        status, payload = json_request(
            "/api/channels/add", {"name": "someone"},
            headers={"Content-Type": "text/plain"})
        self.assertEqual(status, 415)

    def test_malformed_body_is_a_400(self):
        status, payload = json_request("/api/channels/add", raw=b"{not json")
        self.assertEqual(status, 400)

    def test_non_object_body_is_refused(self):
        status, _ = json_request("/api/channels/add", raw=b'["a"]')
        self.assertEqual(status, 400)

    def test_traversal_in_a_channel_name_is_refused(self):
        for name in ("../evil", "C:\\Windows", "a/b", "con"):
            status, _ = json_request("/api/channels/add", {"name": name})
            self.assertEqual(status, 400, name)

    def test_chunk_query_cannot_escape_the_session(self):
        status, _ = json_request(
            "/api/outputs?session_id=nope&chunk=../../../../Windows")
        self.assertIn(status, (400, 404))

    def test_outputs_issue_opaque_ids_for_known_artifacts_only(self):
        session = self.add_session("artifact-list")
        output = session.path / "transcripts" / "c000"
        output.mkdir(parents=True)
        (output / "transcript.txt").write_text("known output", encoding="utf-8")
        (output / "private.txt").write_text("not an output", encoding="utf-8")
        (session.path / "notes.md").write_text("private note", encoding="utf-8")

        status, payload = json_request(
            f"/api/outputs?session_id={session.session_id}")
        self.assertEqual(status, 200)
        files = [item for group in payload["groups"] for item in group["files"]]
        self.assertEqual([item["name"] for item in files], ["transcript.txt"])
        artifact = files[0]
        self.assertIn("artifact_id", artifact)
        self.assertNotIn("path", artifact)
        self.assertTrue(all("directory" not in group for group in payload["groups"]))
        self.assertNotIn(str(session.path), json.dumps(payload))

        status, preview = json_request(
            "/api/file?artifact_id="
            + urllib.parse.quote(artifact["artifact_id"]))
        self.assertEqual(status, 200)
        self.assertEqual(preview["text"], "known output")

    def test_unknown_artifact_id_cannot_read_an_unrelated_in_root_file(self):
        unrelated = self.config.masters_root / "unrelated.txt"
        unrelated.parent.mkdir(parents=True, exist_ok=True)
        unrelated.write_text("must stay private", encoding="utf-8")
        status, payload = json_request(
            "/api/file?artifact_id=" + urllib.parse.quote(str(unrelated)))
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"], "unknown artifact")

    def test_registered_artifact_fails_if_replaced_by_an_escaping_symlink(self):
        session = self.add_session("artifact-symlink")
        output = session.path / "transcripts" / "c000"
        output.mkdir(parents=True)
        artifact_path = output / "transcript.txt"
        artifact_path.write_text("safe", encoding="utf-8")
        _, payload = json_request(
            f"/api/outputs?session_id={session.session_id}")
        artifact_id = payload["groups"][0]["files"][0]["artifact_id"]

        outside = self.tmp / "outside-artifact.txt"
        outside.write_text("secret", encoding="utf-8")
        artifact_path.unlink()
        try:
            artifact_path.symlink_to(outside)
        except (NotImplementedError, OSError) as exc:
            artifact_path.write_text("safe", encoding="utf-8")
            self.skipTest(f"symlinks are unavailable: {exc}")

        status, payload = json_request(
            "/api/file?artifact_id=" + urllib.parse.quote(artifact_id))
        self.assertEqual(status, 403)
        self.assertIn("no longer safe", payload["error"])

    def test_output_discovery_rejects_an_escaping_windows_junction(self):
        if os.name != "nt":
            self.skipTest("Windows junction test")
        session = self.add_session("artifact-junction")
        outside = self.tmp / "outside-junction"
        outside.mkdir()
        (outside / "transcript.txt").write_text("secret", encoding="utf-8")
        output = session.path / "transcripts" / "c000"
        output.parent.mkdir(parents=True)
        created = subprocess.run(
            ["cmd.exe", "/d", "/c", "mklink", "/J", str(output), str(outside)],
            capture_output=True, text=True)
        if created.returncode:
            self.skipTest(f"could not create a junction: {created.stderr.strip()}")
        try:
            status, payload = json_request(
                f"/api/outputs?session_id={session.session_id}")
            self.assertEqual(status, 200)
            names = [item["name"] for group in payload["groups"]
                     for item in group["files"]]
            self.assertNotIn("transcript.txt", names)
        finally:
            output.rmdir()

    def test_non_finite_snapshot_numbers_are_refused(self):
        status, payload = json_request(
            "/api/snapshot", raw=b'{"session_id":"x","start":NaN}')
        self.assertEqual(status, 400)

    def test_oversized_declared_body_is_rejected_without_being_drained(self):
        sock = socket.create_connection(("127.0.0.1", PORT), timeout=2)
        try:
            sock.sendall(
                b"POST /api/channels/add HTTP/1.1\r\n"
                b"Host: 127.0.0.1\r\n"
                b"Content-Type: application/json\r\n"
                b"Content-Length: 999999\r\n\r\n")
            response = b""
            while True:
                part = sock.recv(4096)
                if not part:
                    break
                response += part
        finally:
            sock.close()
        self.assertIn(b" 413 ", response)
        self.assertIn(b'application/json', response)
        self.assertIn(b'"error"', response)

    def test_handler_admission_is_bounded_and_overload_is_json(self):
        httpd = serve(self.pipeline, self.config, port=0, open_browser=False,
                      max_handlers=1, header_deadline=2.0)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        host, port = httpd.server_address
        blocker = socket.create_connection((host, port), timeout=2)
        try:
            blocker.sendall(
                b"GET /api/state HTTP/1.1\r\nHost: 127.0.0.1\r\n")
            deadline = time.monotonic() + 1
            while httpd.active_handlers != 1 and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual(httpd.active_handlers, 1)

            connection = http.client.HTTPConnection(host, port, timeout=2)
            try:
                connection.request("GET", "/api/state")
                response = connection.getresponse()
                payload = json.loads(response.read())
            finally:
                connection.close()
            self.assertEqual(response.status, 503)
            self.assertIn("busy", payload["error"])
        finally:
            blocker.close()
            httpd.shutdown()
            httpd.server_close()

    def test_partial_headers_are_closed_at_the_header_deadline(self):
        httpd = serve(self.pipeline, self.config, port=0, open_browser=False,
                      max_handlers=2, socket_timeout=1.0,
                      header_deadline=0.15, body_deadline=1.0)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        sock = socket.create_connection(httpd.server_address, timeout=2)
        started = time.monotonic()
        try:
            sock.sendall(b"GET /api/state HTTP/1.1\r\nHost: 127.0.0.1")
            sock.settimeout(2)
            while sock.recv(4096):
                pass
        finally:
            sock.close()
            httpd.shutdown()
            httpd.server_close()
        self.assertLess(time.monotonic() - started, 1.0)

    def test_partial_body_is_closed_at_the_body_deadline(self):
        httpd = serve(self.pipeline, self.config, port=0, open_browser=False,
                      max_handlers=2, socket_timeout=1.0,
                      header_deadline=1.0, body_deadline=0.15)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        sock = socket.create_connection(httpd.server_address, timeout=2)
        started = time.monotonic()
        try:
            sock.sendall(
                b"POST /api/channels/add HTTP/1.1\r\n"
                b"Host: 127.0.0.1\r\n"
                b"Content-Type: application/json\r\n"
                b"Content-Length: 100\r\n\r\n{")
            sock.settimeout(2)
            while sock.recv(4096):
                pass
        finally:
            sock.close()
            httpd.shutdown()
            httpd.server_close()
        self.assertLess(time.monotonic() - started, 1.0)

    def test_string_booleans_are_refused(self):
        """`bool("false")` is True, which silently inverted the flag."""
        status, _ = json_request("/api/snapshot",
                                 {"session_id": "x", "last_minutes": 1,
                                  "precise": "false"})
        self.assertEqual(status, 400)

    def test_invalid_config_is_rejected_without_changing_anything(self):
        before = self.config.get("proxies.height")
        status, payload = json_request("/api/config", {"proxies": {"height": 0}})
        self.assertEqual(status, 400)
        self.assertEqual(self.config.get("proxies.height"), before)

    def test_invalid_proxy_port_is_a_validation_error(self):
        before = self.config.get("network.proxy")
        status, payload = json_request(
            "/api/config", {"network": {"proxy": "http://host:99999"}})
        self.assertEqual(status, 400)
        self.assertIn("invalid port", payload["error"])
        self.assertEqual(self.config.get("network.proxy"), before)

    def test_unknown_config_key_is_rejected(self):
        status, payload = json_request("/api/config", {"totally": {"made": "up"}})
        self.assertEqual(status, 400)
        self.assertIn("not a known setting", payload["error"])

    def test_secret_can_be_cleared_explicitly(self):
        json_request("/api/config", {"secrets": {"deepgram_api_key": "temp"}})
        self.assertEqual(self.config.secret("deepgram_api_key"), "temp")
        json_request("/api/config", {"secrets": {"deepgram_api_key": "__clear__"}})
        self.assertEqual(self.config.secret("deepgram_api_key"), "")


if __name__ == "__main__":
    unittest.main()
