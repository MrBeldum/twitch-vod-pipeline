"""Network proxy support: config, schema, and streamlink command wiring.

The proxy is the light-weight alternative to a VPN for reaching Twitch from a
region it withholds the source rendition in (South Korea, which it left in
February 2024). It must reach every streamlink invocation -- live capture, VOD
download, and the live-status probe -- or a partial application would silently
fall back to a direct, geo-blocked connection.
"""

from __future__ import annotations

import unittest

from vodpipe.config import DEFAULTS, deep_merge
from vodpipe.media import (
    oauth_args,
    proxy_args,
    streamlink_command,
    vod_download_command,
)
from vodpipe.schema import ConfigError, validate
from vodpipe.util import Tools

TOOLS = Tools(ffmpeg="ffmpeg", ffprobe="ffprobe", streamlink="streamlink",
              claude=None)


class ProxyArgsTests(unittest.TestCase):
    def test_empty_proxy_adds_nothing(self):
        self.assertEqual(proxy_args(""), [])
        self.assertEqual(proxy_args("   "), [])
        self.assertEqual(proxy_args(None), [])

    def test_proxy_becomes_http_proxy_flag(self):
        self.assertEqual(proxy_args("socks5://127.0.0.1:1080"),
                         ["--http-proxy", "socks5://127.0.0.1:1080"])

    def test_oauth_strips_scheme_prefix(self):
        self.assertEqual(oauth_args("oauth:abc123"),
                         ["--twitch-api-header", "Authorization=OAuth abc123"])
        self.assertEqual(oauth_args(""), [])


class StreamlinkCommandProxyTests(unittest.TestCase):
    def test_live_command_includes_proxy(self):
        cmd = streamlink_command(TOOLS, "https://twitch.tv/x", "best",
                                 proxy="http://host:3128")
        self.assertIn("--http-proxy", cmd)
        self.assertEqual(cmd[cmd.index("--http-proxy") + 1], "http://host:3128")

    def test_live_command_without_proxy_omits_flag(self):
        cmd = streamlink_command(TOOLS, "https://twitch.tv/x", "best")
        self.assertNotIn("--http-proxy", cmd)

    def test_vod_command_includes_proxy(self):
        cmd = vod_download_command(TOOLS, "https://www.twitch.tv/videos/1", "best",
                                   proxy="socks5h://127.0.0.1:9050")
        self.assertIn("--http-proxy", cmd)
        self.assertEqual(cmd[cmd.index("--http-proxy") + 1],
                         "socks5h://127.0.0.1:9050")


class ProxySchemaTests(unittest.TestCase):
    def _validated(self, proxy):
        data = deep_merge(DEFAULTS, {"network": {"proxy": proxy}})
        return validate(data)["network"]["proxy"]

    def test_default_is_empty(self):
        self.assertEqual(DEFAULTS["network"]["proxy"], "")
        self.assertEqual(validate(deep_merge(DEFAULTS, {}))["network"]["proxy"], "")

    def test_valid_proxy_urls_accepted(self):
        for url in ("http://host:3128", "https://host:3128",
                    "socks5://127.0.0.1:1080", "socks5h://127.0.0.1:1080",
                    "socks4://10.0.0.1:1080",
                    "http://user:pass@host:3128"):
            self.assertEqual(self._validated(url), url)

    def test_blank_proxy_stays_blank(self):
        self.assertEqual(self._validated(""), "")
        self.assertEqual(self._validated("   "), "")

    def test_bad_scheme_rejected(self):
        with self.assertRaises(ConfigError):
            self._validated("ftp://host:21")

    def test_missing_scheme_rejected(self):
        with self.assertRaises(ConfigError):
            self._validated("127.0.0.1:1080")

    def test_missing_host_rejected(self):
        with self.assertRaises(ConfigError):
            self._validated("socks5://")

    def test_invalid_ports_raise_config_error(self):
        for url in ("http://host:99999", "http://host:not-a-port"):
            with self.subTest(url=url), self.assertRaisesRegex(
                    ConfigError, "invalid port"):
                self._validated(url)

    def test_control_characters_rejected(self):
        with self.assertRaises(ConfigError):
            self._validated("socks5://127.0.0.1\x001080")


if __name__ == "__main__":
    unittest.main()
