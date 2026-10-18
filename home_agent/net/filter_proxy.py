"""A tiny in-process HTTP proxy that only connects to public destinations.

Headless Chromium is pointed at this proxy for every request (Playwright also routes
loopback traffic through a configured proxy), so redirects, subresources, fetch/XHR
and WebSockets are all checked by `NetGuard` at connect time. The proxy listens on
127.0.0.1 with random credentials known only to the browser we launch.
"""

from __future__ import annotations

import asyncio
import base64
import collections
import logging
import secrets
from urllib.parse import urlsplit

from .guard import ALLOWED_PORTS, BlockedURL, NetGuard

log = logging.getLogger(__name__)

IDLE_TIMEOUT = 60.0
MAX_BYTES_PER_CONNECTION = 60_000_000
HOP_HEADERS = {b"proxy-authorization", b"proxy-connection", b"connection", b"keep-alive", b"upgrade-insecure-requests"}


class FilteringProxy:
    def __init__(self, guard: NetGuard, upstream_proxy: str | None = None, connector=None):
        self.guard = guard
        self.username = secrets.token_hex(8)
        self.password = secrets.token_hex(16)
        self.port: int | None = None
        self.blocked: collections.deque[str] = collections.deque(maxlen=200)
        self._server: asyncio.base_events.Server | None = None
        self._connector = connector or _open_tcp
        self._upstream = _parse_upstream(upstream_proxy) if upstream_proxy else None
        self._expected_auth = base64.b64encode(f"{self.username}:{self.password}".encode()).decode()

    async def start(self) -> int:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0, limit=65536)
        self.port = self._server.sockets[0].getsockname()[1]
        return self.port

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            try:
                await asyncio.wait_for(self._server.wait_closed(), timeout=5)
            except (asyncio.TimeoutError, Exception):
                pass
            self._server = None

    # -- request handling ------------------------------------------------------------
    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        upstream_writer = None
        try:
            try:
                head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=20)
            except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, asyncio.TimeoutError, ConnectionError):
                return
            lines = head.split(b"\r\n")
            try:
                method, target, version = lines[0].decode("latin-1").split(" ", 2)
            except ValueError:
                await self._respond(writer, 400, "Bad Request")
                return
            headers = _parse_headers(lines[1:])
            if not self._authorized(headers):
                writer.write(
                    b"HTTP/1.1 407 Proxy Authentication Required\r\n"
                    b'Proxy-Authenticate: Basic realm="home-agent"\r\n'
                    b"Content-Length: 0\r\nConnection: close\r\n\r\n"
                )
                await writer.drain()
                return
            if method.upper() == "CONNECT":
                upstream_writer = await self._handle_connect(target, reader, writer)
            else:
                upstream_writer = await self._handle_plain(method, target, version, lines[1:], reader, writer)
        except Exception as exc:  # never let one connection take the proxy down
            log.debug("filtering proxy connection error: %s", exc)
        finally:
            for w in (writer, upstream_writer):
                if w is not None:
                    try:
                        w.close()
                    except Exception:
                        pass

    def _authorized(self, headers: dict[bytes, bytes]) -> bool:
        value = headers.get(b"proxy-authorization", b"").decode("latin-1")
        scheme, _, token = value.partition(" ")
        return scheme.lower() == "basic" and secrets.compare_digest(token.strip(), self._expected_auth)

    async def _open(self, host: str, port: int):
        if port not in ALLOWED_PORTS:
            raise BlockedURL(f"port {port} is not allowed")
        if self._upstream is not None:
            try:
                await self.guard.resolve_and_check(host, port)
            except BlockedURL as exc:
                if "could not resolve" not in str(exc):
                    raise
                self.guard.check_host_name(host)  # the upstream proxy resolves public names itself
            up_host, up_port, up_auth = self._upstream
            r, w = await asyncio.wait_for(asyncio.open_connection(up_host, up_port), timeout=15)
            request = f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n"
            if up_auth:
                request += f"Proxy-Authorization: Basic {up_auth}\r\n"
            w.write((request + "\r\n").encode("latin-1"))
            await w.drain()
            reply = await asyncio.wait_for(r.readuntil(b"\r\n\r\n"), timeout=20)
            status = reply.split(b" ", 2)[1] if b" " in reply else b"0"
            if status != b"200":
                w.close()
                raise ConnectionError(f"upstream proxy refused CONNECT ({status.decode('latin-1')})")
            return r, w
        addresses = await self.guard.resolve_and_check(host, port)
        last: Exception | None = None
        for address in addresses:
            try:
                return await asyncio.wait_for(self._connector(address, port), timeout=15)
            except (OSError, asyncio.TimeoutError) as exc:
                last = exc
        raise ConnectionError(f"could not connect to {host}:{port}: {last}")

    async def _handle_connect(self, target: str, reader, writer):
        host, port = _split_host_port(target, default_port=443)
        try:
            up_reader, up_writer = await self._open(host, port)
        except BlockedURL as exc:
            self.blocked.append(f"{host}:{port} ({exc})")
            await self._respond(writer, 403, f"Blocked by Home Agent network guard: {exc}")
            return None
        except (ConnectionError, OSError, asyncio.TimeoutError) as exc:
            await self._respond(writer, 502, f"Bad Gateway: {exc}")
            return None
        writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        await writer.drain()
        await _pipe_both(reader, writer, up_reader, up_writer)
        return up_writer

    async def _handle_plain(self, method: str, target: str, version: str, header_lines, reader, writer):
        parts = urlsplit(target)
        if parts.scheme.lower() != "http" or not parts.hostname:
            await self._respond(writer, 400, "Only absolute http:// URLs are accepted")
            return None
        try:
            checked = self.guard.check_url(target)
            up_reader, up_writer = await self._open(checked.host, checked.port)
        except BlockedURL as exc:
            self.blocked.append(f"{target[:200]} ({exc})")
            await self._respond(writer, 403, f"Blocked by Home Agent network guard: {exc}")
            return None
        except (ConnectionError, OSError, asyncio.TimeoutError) as exc:
            await self._respond(writer, 502, f"Bad Gateway: {exc}")
            return None
        path = target if self._upstream else (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        out = [f"{method} {path} {version}".encode("latin-1")]
        for line in header_lines:
            if not line:
                continue
            name = line.split(b":", 1)[0].strip().lower()
            if name in HOP_HEADERS:
                continue
            out.append(line)
        out.append(b"Connection: close")
        up_writer.write(b"\r\n".join(out) + b"\r\n\r\n")
        await up_writer.drain()
        await _pipe_both(reader, writer, up_reader, up_writer)
        return up_writer

    async def _respond(self, writer, status: int, message: str) -> None:
        body = message.encode("utf-8", "replace")[:2000]
        reason = {400: "Bad Request", 403: "Forbidden", 502: "Bad Gateway"}.get(status, "Error")
        writer.write(
            f"HTTP/1.1 {status} {reason}\r\nContent-Type: text/plain; charset=utf-8\r\n"
            f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode("latin-1")
            + body
        )
        try:
            await writer.drain()
        except ConnectionError:
            pass


async def _open_tcp(address: str, port: int):
    return await asyncio.open_connection(address, port)


def _parse_headers(lines: list[bytes]) -> dict[bytes, bytes]:
    headers: dict[bytes, bytes] = {}
    for line in lines:
        if b":" in line:
            name, value = line.split(b":", 1)
            headers[name.strip().lower()] = value.strip()
    return headers


def _split_host_port(target: str, default_port: int) -> tuple[str, int]:
    target = target.strip()
    if target.startswith("["):
        host, _, rest = target[1:].partition("]")
        port = int(rest[1:]) if rest.startswith(":") else default_port
        return host, port
    host, sep, port = target.rpartition(":")
    if not sep:
        return target, default_port
    return host, int(port)


def _parse_upstream(url: str) -> tuple[str, int, str | None]:
    parts = urlsplit(url if "://" in url else f"http://{url}")
    if parts.scheme not in ("http", ""):
        raise ValueError("OUTBOUND_PROXY must be an http:// proxy URL for browser rendering")
    auth = None
    if parts.username:
        auth = base64.b64encode(f"{parts.username}:{parts.password or ''}".encode()).decode()
    return parts.hostname or "127.0.0.1", parts.port or 8080, auth


async def _pipe(src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> None:
    total = 0
    try:
        while True:
            data = await asyncio.wait_for(src.read(65536), timeout=IDLE_TIMEOUT)
            if not data:
                break
            total += len(data)
            if total > MAX_BYTES_PER_CONNECTION:
                break
            dst.write(data)
            await dst.drain()
    except (asyncio.TimeoutError, ConnectionError, OSError):
        pass
    finally:
        try:
            if dst.can_write_eof():
                dst.write_eof()
        except (OSError, RuntimeError):
            pass


async def _pipe_both(client_reader, client_writer, up_reader, up_writer) -> None:
    tasks = [
        asyncio.create_task(_pipe(client_reader, up_writer)),
        asyncio.create_task(_pipe(up_reader, client_writer)),
    ]
    try:
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        # give the other direction a moment to flush, then stop it
        if pending:
            await asyncio.wait(pending, timeout=IDLE_TIMEOUT)
    finally:
        for t in tasks:
            if not t.done():
                t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
