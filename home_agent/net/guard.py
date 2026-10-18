"""Outbound request guard for agent-initiated web access.

Every URL the agent asks to fetch or render goes through `NetGuard`:

* only http/https, no embedded credentials, a small set of web ports;
* host names that are obviously internal (localhost, *.local, metadata names,
  single-label names) are refused;
* the host is resolved and *every* resolved address must be public: loopback,
  RFC 1918, link-local (cloud metadata 169.254.169.254), CGNAT/Tailscale
  100.64.0.0/10, ULA, multicast, NAT64/6to4-wrapped private addresses, etc. are refused;
* for direct connections the check runs again at connect time, against the exact
  address the socket connects to, so DNS rebinding between check and connect fails.

Redirects are followed manually by the fetcher and every hop is checked again.
Explicitly configured endpoints (model servers, SearXNG, the outbound proxy) do not
use this guard; the agent cannot reach them through its tools.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass

import httpcore2
import httpx2

ALLOWED_SCHEMES = {"http", "https"}
ALLOWED_PORTS = {80, 443, 8000, 8008, 8080, 8443, 8888}
BLOCKED_HOSTS = {
    "localhost",
    "metadata",
    "metadata.google.internal",
    "instance-data",
    "instance-data.ec2.internal",
    "kubernetes.default",
    "host.docker.internal",
}
BLOCKED_SUFFIXES = (
    ".localhost",
    ".local",
    ".localdomain",
    ".internal",
    ".intranet",
    ".lan",
    ".home",
    ".home.arpa",
    ".corp",
    ".private",
)
_NAT64 = ipaddress.ip_network("64:ff9b::/96")
_NAT64_LOCAL = ipaddress.ip_network("64:ff9b:1::/48")
_EXTRA_BLOCKED = [
    ipaddress.ip_network(n)
    for n in (
        "0.0.0.0/8",
        "100.64.0.0/10",
        "192.0.0.0/24",
        "192.0.2.0/24",
        "198.18.0.0/15",
        "198.51.100.0/24",
        "203.0.113.0/24",
        "240.0.0.0/4",
        "255.255.255.255/32",
    )
]

Resolver = Callable[[str, int], Awaitable[list[str]]]


class BlockedURL(Exception):
    """The URL or destination is not allowed for agent web access."""


@dataclass(frozen=True)
class CheckedURL:
    url: str
    scheme: str
    host: str
    port: int


async def system_resolver(host: str, port: int) -> list[str]:
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    seen: list[str] = []
    for info in infos:
        addr = info[4][0]
        if addr not in seen:
            seen.append(addr)
    return seen


class NetGuard:
    def __init__(
        self,
        extra_allowed: Iterable[ipaddress.IPv4Network | ipaddress.IPv6Network] = (),
        resolver: Resolver | None = None,
    ):
        self.extra_allowed = list(extra_allowed)
        self.resolver = resolver or system_resolver

    # -- addresses -------------------------------------------------------------
    def ip_allowed(self, value: str | ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
        try:
            ip = ipaddress.ip_address(value.split("%", 1)[0] if isinstance(value, str) else value)
        except ValueError:
            return False
        if any(ip in net for net in self.extra_allowed):
            return True
        if ip.version == 6:
            if ip.ipv4_mapped is not None:
                return self.ip_allowed(ip.ipv4_mapped)
            if ip in _NAT64 or ip in _NAT64_LOCAL:
                return self.ip_allowed(ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF))
            if ip.sixtofour is not None:
                return self.ip_allowed(ip.sixtofour)
            if ip.teredo is not None:
                server, client = ip.teredo
                return self.ip_allowed(server) and self.ip_allowed(client)
        if ip.is_multicast or ip.is_unspecified or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            return False
        if not ip.is_global:
            return False
        return not any(ip.version == net.version and ip in net for net in _EXTRA_BLOCKED)

    # -- URLs -----------------------------------------------------------------------
    def check_url(self, url: str) -> CheckedURL:
        if not isinstance(url, str) or not url.strip():
            raise BlockedURL("empty URL")
        url = url.strip()
        if len(url) > 4096:
            raise BlockedURL("URL is too long")
        try:
            parsed = httpx2.URL(url)
        except Exception as exc:  # httpx2.InvalidURL and friends
            raise BlockedURL(f"invalid URL: {exc}") from None
        scheme = parsed.scheme.lower()
        if scheme not in ALLOWED_SCHEMES:
            raise BlockedURL(f"scheme '{scheme}' is not allowed (only http and https)")
        if parsed.userinfo:
            raise BlockedURL("URLs with embedded credentials are not allowed")
        try:
            host = parsed.raw_host.decode("ascii").lower().rstrip(".")
        except UnicodeDecodeError:
            raise BlockedURL("invalid host name") from None
        if not host:
            raise BlockedURL("URL has no host")
        port = parsed.port or (443 if scheme == "https" else 80)
        if port not in ALLOWED_PORTS:
            raise BlockedURL(f"port {port} is not allowed")
        self.check_host_name(host)
        return CheckedURL(url=str(parsed), scheme=scheme, host=host, port=port)

    def check_host_name(self, host: str) -> None:
        host = host.lower().rstrip(".").strip("[]")
        if _is_ip_literal(host):
            if not self.ip_allowed(host):
                raise BlockedURL(f"address {host} is private, local, or reserved")
            return
        if host in BLOCKED_HOSTS or host.endswith(BLOCKED_SUFFIXES):
            raise BlockedURL(f"host '{host}' is a local or internal name")
        if "." not in host:
            raise BlockedURL(f"host '{host}' is not a public domain name")

    async def resolve_and_check(self, host: str, port: int) -> list[str]:
        """Resolve `host` and return its addresses; raise if any address is not public."""
        host = host.lower().rstrip(".").strip("[]")
        self.check_host_name(host)
        if _is_ip_literal(host):
            return [host]
        try:
            addresses = await self.resolver(host, port)
        except (OSError, UnicodeError) as exc:
            raise BlockedURL(f"could not resolve {host}: {exc}") from None
        if not addresses:
            raise BlockedURL(f"could not resolve {host}")
        bad = [a for a in addresses if not self.ip_allowed(a)]
        if bad:
            raise BlockedURL(f"{host} resolves to a private or reserved address ({bad[0]})")
        return addresses

    async def check_destination(self, url: str, *, resolve: bool = True) -> CheckedURL:
        checked = self.check_url(url)
        if resolve:
            await self.resolve_and_check(checked.host, checked.port)
        return checked


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host.split("%", 1)[0])
        return True
    except ValueError:
        return False


Connector = Callable[..., Awaitable[httpcore2.AsyncNetworkStream]]


class GuardedNetworkBackend(httpcore2.AsyncNetworkBackend):
    """httpcore network backend that connects only to addresses NetGuard allows.

    The address check happens here, on the exact IP the socket connects to, which
    closes the DNS-rebinding gap between "check" and "connect". TLS still verifies the
    certificate against the original host name (httpcore passes it as SNI).
    """

    def __init__(self, guard: NetGuard, inner: httpcore2.AsyncNetworkBackend | None = None):
        self._guard = guard
        self._inner = inner or httpcore2.AnyIOBackend()

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        try:
            addresses = await self._guard.resolve_and_check(host, port)
        except BlockedURL as exc:
            raise httpcore2.ConnectError(f"blocked by network guard: {exc}") from None
        last_error: Exception | None = None
        for address in addresses:
            try:
                return await self._inner.connect_tcp(
                    address, port, timeout=timeout, local_address=local_address, socket_options=socket_options
                )
            except (httpcore2.ConnectError, httpcore2.ConnectTimeout, OSError) as exc:
                last_error = exc
        assert last_error is not None
        raise last_error

    async def connect_unix_socket(self, path, timeout=None, socket_options=None):
        raise httpcore2.ConnectError("unix sockets are not allowed")

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


def guarded_transport(guard: NetGuard, proxy: str | None = None) -> httpx2.AsyncBaseTransport:
    """Transport for agent web access.

    Without a proxy every TCP connection is validated at connect time. With an
    outbound proxy configured (for example a local VPN client) the proxy resolves and
    connects, so only the pre-flight checks in `NetGuard.check_destination` apply.
    """
    if proxy:
        return httpx2.AsyncHTTPTransport(proxy=proxy, trust_env=True)
    transport = httpx2.AsyncHTTPTransport(trust_env=True)
    ssl_context = transport._pool._ssl_context  # reuse httpx2's trust-store setup
    transport._pool = httpcore2.AsyncConnectionPool(
        ssl_context=ssl_context,
        max_connections=20,
        max_keepalive_connections=5,
        keepalive_expiry=5.0,
        network_backend=GuardedNetworkBackend(guard),
    )
    return transport
