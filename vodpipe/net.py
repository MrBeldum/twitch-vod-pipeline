"""Proxied TCP for the few places urllib cannot reach.

urllib handles HTTP(S) proxies. Twitch IRC is a raw TLS socket, and a SOCKS
proxy is how this machine reaches Twitch from a region it has left. Both need
a handshake this module owns so chat capture and GraphQL use the same path.

Supported `network.proxy` values match the schema: http(s), socks4/4a/5/5h.
Empty means a direct connection.
"""

from __future__ import annotations

import base64
import socket
import ssl
import struct
from urllib.parse import unquote, urlparse


class ProxyError(OSError):
    """The proxy handshake failed. The destination was never reached."""


def open_tcp(host: str, port: int, proxy: str = "", *,
             timeout: float = 20.0) -> socket.socket:
    """A connected TCP socket to `host:port`, optionally via `proxy`."""
    proxy = (proxy or "").strip()
    if not proxy:
        sock = socket.create_connection((host, port), timeout=timeout)
        sock.settimeout(timeout)
        return sock
    parsed = urlparse(proxy)
    scheme = (parsed.scheme or "").lower()
    proxy_host = parsed.hostname
    proxy_port = parsed.port
    if not proxy_host:
        raise ProxyError(f"proxy URL has no host: {proxy!r}")
    if proxy_port is None:
        proxy_port = 1080 if scheme.startswith("socks") else 8080
    username = unquote(parsed.username) if parsed.username else ""
    password = unquote(parsed.password) if parsed.password else ""

    sock = socket.create_connection((proxy_host, proxy_port), timeout=timeout)
    sock.settimeout(timeout)
    try:
        if scheme in ("http", "https"):
            _http_connect(sock, host, port, username, password)
        elif scheme in ("socks5", "socks5h"):
            _socks5_connect(sock, host, port, username, password,
                            remote_dns=scheme == "socks5h")
        elif scheme in ("socks4", "socks4a"):
            _socks4_connect(sock, host, port, username,
                            remote_dns=scheme == "socks4a")
        else:
            sock.close()
            raise ProxyError(f"unsupported proxy scheme {scheme!r}")
    except Exception:
        sock.close()
        raise
    return sock


def wrap_tls(sock: socket.socket, host: str, *,
             timeout: float | None = None) -> ssl.SSLSocket:
    context = ssl.create_default_context()
    if timeout is not None:
        sock.settimeout(timeout)
    return context.wrap_socket(sock, server_hostname=host)


def _http_connect(sock: socket.socket, host: str, port: int,
                  username: str, password: str) -> None:
    request = [f"CONNECT {host}:{port} HTTP/1.1", f"Host: {host}:{port}"]
    if username or password:
        token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
        request.append(f"Proxy-Authorization: Basic {token}")
    request.append("")
    request.append("")
    sock.sendall("\r\n".join(request).encode("ascii"))
    response = _read_until(sock, b"\r\n\r\n", limit=65536)
    status_line = response.split(b"\r\n", 1)[0].decode("ascii", "replace")
    parts = status_line.split(" ", 2)
    if len(parts) < 2 or not parts[1].startswith("2"):
        raise ProxyError(f"proxy CONNECT failed: {status_line.strip() or 'no status'}")


def _socks5_connect(sock: socket.socket, host: str, port: int,
                    username: str, password: str, *, remote_dns: bool) -> None:
    if username or password:
        sock.sendall(b"\x05\x02\x00\x02")
    else:
        sock.sendall(b"\x05\x01\x00")
    greeting = _read_exact(sock, 2)
    if greeting[0] != 5:
        raise ProxyError("proxy is not SOCKS5")
    method = greeting[1]
    if method == 2:
        user_b = username.encode("utf-8")[:255]
        pass_b = password.encode("utf-8")[:255]
        sock.sendall(b"\x01" + bytes([len(user_b)]) + user_b
                     + bytes([len(pass_b)]) + pass_b)
        auth = _read_exact(sock, 2)
        if auth[1] != 0:
            raise ProxyError("SOCKS5 username/password rejected")
    elif method != 0:
        raise ProxyError(f"SOCKS5 authentication method {method} is not supported")

    atyp, addr = _socks_addr(host, remote_dns=remote_dns)
    sock.sendall(b"\x05\x01\x00" + atyp + addr + struct.pack("!H", port))
    header = _read_exact(sock, 4)
    if header[1] != 0:
        raise ProxyError(f"SOCKS5 connect failed (code {header[1]})")
    _discard_socks_bind(sock, header[3])


def _socks4_connect(sock: socket.socket, host: str, port: int,
                    username: str, *, remote_dns: bool) -> None:
    user = username.encode("utf-8") + b"\x00"
    if remote_dns:
        # SOCKS4a: dest IP 0.0.0.x with x != 0, hostname follows the user id.
        payload = (b"\x04\x01" + struct.pack("!H", port) + b"\x00\x00\x00\x01"
                   + user + host.encode("idna") + b"\x00")
    else:
        try:
            ip = socket.inet_aton(host)
        except OSError:
            ip = socket.inet_aton(socket.gethostbyname(host))
        payload = b"\x04\x01" + struct.pack("!H", port) + ip + user
    sock.sendall(payload)
    reply = _read_exact(sock, 8)
    if reply[1] != 0x5A:
        raise ProxyError(f"SOCKS4 connect failed (code {reply[1]})")


def _socks_addr(host: str, *, remote_dns: bool) -> tuple[bytes, bytes]:
    if not remote_dns:
        try:
            packed = socket.inet_pton(socket.AF_INET, host)
            return b"\x01", packed
        except OSError:
            try:
                packed = socket.inet_pton(socket.AF_INET6, host)
                return b"\x04", packed
            except OSError:
                host = socket.getaddrinfo(host, None, socket.AF_INET)[0][4][0]
                return b"\x01", socket.inet_pton(socket.AF_INET, host)
    encoded = host.encode("idna")
    if len(encoded) > 255:
        raise ProxyError("hostname is too long for SOCKS5")
    return b"\x03", bytes([len(encoded)]) + encoded


def _discard_socks_bind(sock: socket.socket, atyp: int) -> None:
    if atyp == 1:
        _read_exact(sock, 4 + 2)
    elif atyp == 4:
        _read_exact(sock, 16 + 2)
    elif atyp == 3:
        length = _read_exact(sock, 1)[0]
        _read_exact(sock, length + 2)
    else:
        raise ProxyError(f"SOCKS5 returned unknown address type {atyp}")


def _read_exact(sock: socket.socket, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        piece = sock.recv(remaining)
        if not piece:
            raise ProxyError("proxy closed the connection during handshake")
        chunks.append(piece)
        remaining -= len(piece)
    return b"".join(chunks)


def _read_until(sock: socket.socket, marker: bytes, *, limit: int) -> bytes:
    buf = bytearray()
    while marker not in buf:
        piece = sock.recv(4096)
        if not piece:
            raise ProxyError("proxy closed the connection during handshake")
        buf.extend(piece)
        if len(buf) > limit:
            raise ProxyError("proxy handshake response was too large")
    return bytes(buf)
