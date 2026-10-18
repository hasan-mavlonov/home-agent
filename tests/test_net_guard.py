"""SSRF protection: URL rules, DNS answers, connect-time checks (DNS rebinding), redirects,
the browser's filtering proxy. Network access is simulated with local sockets and mocks."""

from __future__ import annotations

import asyncio
import base64
import ipaddress

import httpcore2
import httpx2
import pytest
from helpers import PUBLIC_TEST_IP, fake_resolver, make_guard, pages_transport

from home_agent.net.fetcher import WebFetcher
from home_agent.net.filter_proxy import FilteringProxy
from home_agent.net.guard import BlockedURL, GuardedNetworkBackend, NetGuard


@pytest.mark.parametrize("url", [
    "http://127.0.0.1/", "http://localhost/", "http://LOCALHOST./", "http://[::1]/", "http://0.0.0.0/",
    "http://169.254.169.254/latest/meta-data/", "http://metadata.google.internal/", "http://10.0.0.5/",
    "http://192.168.1.1/", "http://172.16.0.1/", "http://100.100.100.200/", "http://100.64.0.7/",
    "http://[::ffff:127.0.0.1]/", "http://[64:ff9b::7f00:1]/", "http://[2002:7f00:1::]/", "http://[fd00:ec2::254]/",
    "http://224.0.0.1/", "http://printer.local/", "http://intranet/", "http://router.lan/", "file:///etc/passwd",
    "ftp://example.com/", "gopher://example.com/", "http://user:pass@example.com/", "https://example.com:22/",
    "https://example.com:6379/",
])  # fmt: skip
def test_blocked_urls(url):
    guard = make_guard()
    with pytest.raises(BlockedURL):
        asyncio.run(guard.check_destination(url))


def test_public_url_allowed_and_dns_checked():
    guard = make_guard({"good.example.org": ["93.184.215.14"], "evil.example.org": ["93.184.215.14", "10.1.2.3"],
                        "rebind.example.org": ["127.0.0.1"]})  # fmt: skip
    assert asyncio.run(guard.check_destination("https://good.example.org/a?b=c")).host == "good.example.org"
    with pytest.raises(BlockedURL, match="private"):
        asyncio.run(guard.check_destination("https://evil.example.org/"))  # any private answer blocks
    with pytest.raises(BlockedURL):
        asyncio.run(guard.check_destination("https://rebind.example.org/"))


def test_extra_allowed_cidr_for_fake_ip_vpn_mode():
    guard = NetGuard([ipaddress.ip_network("198.18.0.0/15")], resolver=fake_resolver({"site.example.org": ["198.18.0.9"]}))
    assert asyncio.run(guard.check_destination("https://site.example.org/")).host == "site.example.org"


def test_connect_time_check_defeats_dns_rebinding():
    """The pre-check sees a public address; the socket connect sees 127.0.0.1 and is refused."""
    answers = iter([[PUBLIC_TEST_IP], ["127.0.0.1"]])

    async def flipping_resolver(host, port):
        return next(answers)

    guard = NetGuard(resolver=flipping_resolver)

    class NeverConnect(httpcore2.AsyncNetworkBackend):
        async def connect_tcp(self, *a, **k):  # pragma: no cover - must not be reached
            raise AssertionError("connected to a private address")

    async def run():
        await guard.check_destination("https://rebinding.example.org/")
        backend = GuardedNetworkBackend(guard, inner=NeverConnect())
        with pytest.raises(httpcore2.ConnectError, match="network guard"):
            await backend.connect_tcp("rebinding.example.org", 443)

    asyncio.run(run())


def test_fetcher_blocks_redirects_to_private_addresses():
    pages = {
        "https://redir.example.org/a": (302, "text/html", "REDIRECT http://127.0.0.1:8080/admin"),
        "https://redir.example.org/b": (301, "text/html", "REDIRECT http://169.254.169.254/latest/meta-data/"),
        "https://redir.example.org/c": (302, "text/html", "REDIRECT https://ok.example.org/page"),
        "https://ok.example.org/page": (200, "text/html", "<html><title>OK</title><body><p>Fine content here</p></body></html>"),
    }
    hits: list[str] = []
    fetcher = WebFetcher(make_guard(), transport=pages_transport(pages, hits))

    async def run():
        a = await fetcher.fetch("https://redir.example.org/a")
        b = await fetcher.fetch("https://redir.example.org/b")
        c = await fetcher.fetch("https://redir.example.org/c")
        await fetcher.aclose()
        return a, b, c

    a, b, c = asyncio.run(run())
    assert not a.ok and "Blocked redirect target" in a.error
    assert not b.ok and "Blocked redirect target" in b.error
    assert c.ok and c.title == "OK" and c.final_url == "https://ok.example.org/page"
    assert not any("127.0.0.1" in h or "169.254" in h for h in hits)


def test_fetcher_reports_captcha_and_does_not_bypass():
    challenge = "<html><title>Just a moment...</title><body>Checking your browser before accessing. cf-browser-verification</body></html>"
    pages = {"https://protected.example.org/": (403, "text/html", challenge),
             "https://login.example.org/x": (302, "text/html", "REDIRECT https://login.example.org/accounts/login?next=x"),
             "https://login.example.org/accounts/login?next=x": (200, "text/html", "<html><body>Sign in</body></html>")}  # fmt: skip
    fetcher = WebFetcher(make_guard(), transport=pages_transport(pages))
    result = asyncio.run(fetcher.fetch("https://protected.example.org/"))
    assert not result.ok and result.access_blocked and "does not bypass" in result.error
    login = asyncio.run(fetcher.fetch("https://login.example.org/x"))
    assert not login.ok and "sign-in" in login.error


def test_fetcher_reads_pdf_and_limits_size():
    from pypdf import PdfWriter

    import io

    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    buffer = io.BytesIO()
    writer.write(buffer)
    pages = {"https://docs.example.org/rules.pdf": (200, "application/pdf", "")}
    transport = httpx2.MockTransport(lambda request: httpx2.Response(200, content=buffer.getvalue(), headers={"content-type": "application/pdf"}))
    fetcher = WebFetcher(make_guard(), transport=transport)
    result = asyncio.run(fetcher.fetch("https://docs.example.org/rules.pdf"))
    assert result.ok and "[page 1]" in result.text
    big = httpx2.MockTransport(lambda request: httpx2.Response(200, content=b"<html><body>" + b"x" * 3_000_000 + b"</body></html>",
                                                               headers={"content-type": "text/html"}))  # fmt: skip
    small = WebFetcher(make_guard(), transport=big, max_bytes=100_000)
    result = asyncio.run(small.fetch("https://big.example.org/"))
    assert result.truncated
    del pages


async def _proxy_request(proxy: FilteringProxy, raw: bytes) -> bytes:
    reader, writer = await asyncio.open_connection("127.0.0.1", proxy.port)
    writer.write(raw)
    await writer.drain()
    data = await asyncio.wait_for(reader.read(65536), timeout=5)
    writer.close()
    return data


def _auth(proxy: FilteringProxy) -> bytes:
    token = base64.b64encode(f"{proxy.username}:{proxy.password}".encode())
    return b"Proxy-Authorization: Basic " + token + b"\r\n"


def test_filtering_proxy_rules():
    async def run():
        # a local "public site" the proxy will reach through a test connector
        async def site(reader, writer):
            await reader.readuntil(b"\r\n\r\n")
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\nConnection: close\r\n\r\nhello")
            await writer.drain()
            writer.close()

        server = await asyncio.start_server(site, "127.0.0.1", 0)
        site_port = server.sockets[0].getsockname()[1]

        async def connector(address, port):
            assert address == PUBLIC_TEST_IP  # only ever connects to the vetted address
            return await asyncio.open_connection("127.0.0.1", site_port)

        proxy = FilteringProxy(make_guard(), connector=connector)
        await proxy.start()
        try:
            no_auth = await _proxy_request(proxy, b"CONNECT site.example.org:443 HTTP/1.1\r\n\r\n")
            assert no_auth.startswith(b"HTTP/1.1 407")
            blocked = await _proxy_request(proxy, b"CONNECT 127.0.0.1:443 HTTP/1.1\r\n" + _auth(proxy) + b"\r\n")
            assert blocked.startswith(b"HTTP/1.1 403")
            metadata = await _proxy_request(proxy, b"GET http://169.254.169.254/latest/meta-data/ HTTP/1.1\r\nHost: 169.254.169.254\r\n" + _auth(proxy) + b"\r\n")
            assert metadata.startswith(b"HTTP/1.1 403")
            bad_port = await _proxy_request(proxy, b"CONNECT site.example.org:25 HTTP/1.1\r\n" + _auth(proxy) + b"\r\n")
            assert bad_port.startswith(b"HTTP/1.1 403")
            ok = await _proxy_request(proxy, b"GET http://site.example.org/ HTTP/1.1\r\nHost: site.example.org\r\n" + _auth(proxy) + b"\r\n")
            assert ok.startswith(b"HTTP/1.1 200") and ok.endswith(b"hello")
            assert any("169.254.169.254" in b for b in proxy.blocked)
        finally:
            await proxy.stop()
            server.close()

    asyncio.run(run())
