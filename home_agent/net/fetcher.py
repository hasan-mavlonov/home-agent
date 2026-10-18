"""HTTP page fetching for the agent, with redirect-by-redirect guard checks."""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit

import httpx2

from .extract import Extracted, html_to_text, looks_like_challenge, needs_javascript, normalize_whitespace, pdf_to_text
from .guard import BlockedURL, NetGuard, guarded_transport

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/141.0 Safari/537.36 HomeAgent/0.1 (personal research assistant)"
)
MAX_REDIRECTS = 6
TEXT_TYPES = ("text/plain", "text/markdown", "text/csv", "application/json", "application/ld+json", "text/xml",
              "application/xml", "application/rss+xml", "application/atom+xml")  # fmt: skip
HTML_TYPES = ("text/html", "application/xhtml+xml")
MAX_STORED_CHARS = 400_000


@dataclass
class FetchResult:
    url: str
    final_url: str
    ok: bool
    status_code: int | None = None
    content_type: str | None = None
    title: str | None = None
    text: str = ""
    links: list[dict] = field(default_factory=list)
    error: str | None = None
    access_blocked: bool = False
    needs_javascript: bool = False
    redirects: list[str] = field(default_factory=list)
    via: str = "http"
    truncated: bool = False


class WebFetcher:
    def __init__(
        self,
        guard: NetGuard,
        *,
        proxy: str | None = None,
        timeout: float = 20.0,
        max_bytes: int = 8_000_000,
        transport: httpx2.AsyncBaseTransport | None = None,
    ):
        self.guard = guard
        self.proxy = proxy
        self.max_bytes = max_bytes
        self._client = httpx2.AsyncClient(
            transport=transport or guarded_transport(guard, proxy),
            timeout=httpx2.Timeout(timeout, connect=min(10.0, timeout)),
            follow_redirects=False,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,application/pdf;q=0.9,text/plain;q=0.8,*/*;q=0.5",
                "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.6",
                "Accept-Encoding": "gzip, deflate",
            },
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def fetch(self, url: str) -> FetchResult:
        current = url
        redirects: list[str] = []
        for _hop in range(MAX_REDIRECTS + 1):
            try:
                checked = await self.guard.check_destination(current, resolve=True)
            except BlockedURL as exc:
                where = "redirect target" if redirects else "URL"
                return FetchResult(url=url, final_url=current, ok=False, error=f"Blocked {where}: {exc}",
                                   redirects=redirects)  # fmt: skip
            try:
                async with self._client.stream("GET", checked.url) as response:
                    if response.status_code in (301, 302, 303, 307, 308) and response.headers.get("location"):
                        current = urljoin(str(response.url), response.headers["location"].strip())
                        redirects.append(current)
                        continue
                    body, truncated = await self._read_limited(response)
                    return self._build(url, str(response.url), response, body, truncated, redirects)
            except httpx2.TimeoutException:
                return FetchResult(url=url, final_url=current, ok=False, error="Timed out while fetching the page",
                                   redirects=redirects)  # fmt: skip
            except httpx2.ConnectError as exc:
                message = str(exc)
                if "network guard" in message:
                    return FetchResult(url=url, final_url=current, ok=False, error=f"Blocked: {message}",
                                       redirects=redirects)  # fmt: skip
                return FetchResult(url=url, final_url=current, ok=False, error=f"Could not connect: {message[:200]}",
                                   redirects=redirects)  # fmt: skip
            except httpx2.HTTPError as exc:
                return FetchResult(url=url, final_url=current, ok=False,
                                   error=f"HTTP error: {type(exc).__name__}: {str(exc)[:200]}", redirects=redirects)  # fmt: skip
        return FetchResult(url=url, final_url=current, ok=False, error="Too many redirects", redirects=redirects)

    async def _read_limited(self, response: httpx2.Response) -> tuple[bytes, bool]:
        chunks: list[bytes] = []
        size = 0
        async for chunk in response.aiter_bytes():
            chunks.append(chunk)
            size += len(chunk)
            if size > self.max_bytes:
                return b"".join(chunks)[: self.max_bytes], True
            await asyncio.sleep(0)
        return b"".join(chunks), False

    def _build(self, url, final_url, response, body: bytes, truncated: bool, redirects) -> FetchResult:
        status = response.status_code
        content_type = (response.headers.get("content-type") or "").split(";")[0].strip().lower()
        result = FetchResult(url=url, final_url=final_url, ok=False, status_code=status, content_type=content_type,
                             redirects=redirects, truncated=truncated)  # fmt: skip
        is_pdf = content_type == "application/pdf" or (not content_type and body[:5] == b"%PDF-")
        try:
            if is_pdf:
                title, text = pdf_to_text(body)
                extracted = Extracted(title=title, text=text)
            elif content_type in HTML_TYPES or (not content_type and b"<html" in body[:2000].lower()):
                extracted = html_to_text(_decode(body, response, html=True), final_url)
            elif content_type.startswith("text/") or content_type in TEXT_TYPES:
                extracted = Extracted(title=None, text=normalize_whitespace(_decode(body, response)))
            else:
                result.error = f"Unsupported content type '{content_type or 'unknown'}' (only web pages, text and PDF are read)"
                return result
        except Exception as exc:  # malformed documents
            result.error = f"Could not read the document: {type(exc).__name__}"
            return result

        result.title = extracted.title
        result.text = extracted.text[:MAX_STORED_CHARS]
        result.links = extracted.links
        if looks_like_challenge(status, extracted.text, extracted.title):
            result.access_blocked = True
            result.error = (
                f"The site returned a bot-protection or CAPTCHA page (HTTP {status}). "
                "Home Agent does not bypass these; use another source or ask the user to open it."
            )
            return result
        if status in (401, 402, 403, 407, 451):
            result.access_blocked = True
            result.error = f"Access denied by the site (HTTP {status})"
            return result
        if status == 429:
            result.access_blocked = True
            result.error = "The site is rate limiting requests (HTTP 429)"
            return result
        if status >= 400:
            result.error = f"The site returned HTTP {status}"
            return result
        if redirects and _looks_like_login(final_url) and not _looks_like_login(url):
            result.error = "The page redirected to a sign-in page; its content requires an account"
            result.access_blocked = True
            return result
        result.ok = True
        if not is_pdf and content_type in HTML_TYPES:
            result.needs_javascript = needs_javascript(extracted)
        return result


def _looks_like_login(url: str) -> bool:
    path = urlsplit(url).path
    return bool(re.search(r"/(login|log-in|signin|sign-in|sso|auth)(/|$|\.)", path, re.I))


def _decode(body: bytes, response: httpx2.Response, html: bool = False) -> str:
    charset = response.charset_encoding
    if not charset and html:
        match = re.search(rb"<meta[^>]+charset=[\"']?([A-Za-z0-9_\-]+)", body[:4096], re.I)
        if match:
            charset = match.group(1).decode("ascii", "ignore")
    for candidate in (charset, "utf-8", "gb18030", "latin-1"):
        if not candidate:
            continue
        try:
            return body.decode(candidate)
        except (LookupError, UnicodeDecodeError):
            continue
    return body.decode("utf-8", errors="replace")
