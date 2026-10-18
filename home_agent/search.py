"""Web search adapters: Brave Search API, Tavily, or a SearXNG instance you run.

Only the search query string leaves the machine (to the configured provider).
Missing credentials raise `SearchNotConfigured`; nothing is ever faked.
"""

from __future__ import annotations

import asyncio
import html
import re
from dataclasses import dataclass, field

import httpx2

from .config import Settings, is_local_endpoint

FRESHNESS_BRAVE = {"day": "pd", "week": "pw", "month": "pm", "year": "py"}
FRESHNESS_TAVILY = {"day": "day", "week": "week", "month": "month", "year": "year"}
FRESHNESS_SEARXNG = {"day": "day", "week": "month", "month": "month", "year": "year"}


@dataclass
class SearchResult:
    title: str
    url: str
    snippet: str = ""
    published: str | None = None
    extra_snippets: list[str] = field(default_factory=list)


class SearchError(Exception):
    """Search failed; the message is safe to show to the user and the model."""


class SearchNotConfigured(SearchError):
    pass


def _clean(text: str | None) -> str:
    if not text:
        return ""
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", text))).strip()


class _HttpSearch:
    name = "search"
    max_attempts = 3

    def __init__(self, client: httpx2.AsyncClient):
        self.client = client

    async def _request(self, method: str, url: str, **kwargs) -> dict:
        delay = 1.0
        last = ""
        for attempt in range(1, self.max_attempts + 1):
            try:
                response = await self.client.request(method, url, **kwargs)
            except httpx2.TimeoutException:
                last = "timed out"
            except httpx2.HTTPError as exc:
                last = f"network error ({type(exc).__name__})"
            else:
                if response.status_code == 200:
                    try:
                        return response.json()
                    except ValueError:
                        raise SearchError(f"{self.name} returned a response that is not JSON") from None
                if response.status_code in (401, 403):
                    raise SearchError(
                        f"{self.name} rejected the API key (HTTP {response.status_code}). Check the key in .env."
                    )
                if response.status_code == 402:
                    raise SearchError(f"{self.name} reports the plan's quota or credits are used up (HTTP 402).")
                if response.status_code == 422 or response.status_code == 400:
                    raise SearchError(f"{self.name} rejected the query (HTTP {response.status_code}).")
                last = f"HTTP {response.status_code}"
                if response.status_code == 429:
                    retry_after = response.headers.get("retry-after", "")
                    if retry_after.isdigit():
                        delay = min(float(retry_after), 10.0)
                elif response.status_code < 500:
                    raise SearchError(f"{self.name} request failed ({last}).")
            if attempt < self.max_attempts:
                await asyncio.sleep(delay)
                delay *= 2
        raise SearchError(f"{self.name} is unavailable right now ({last}).")


class BraveSearch(_HttpSearch):
    name = "Brave Search"
    endpoint = "https://api.search.brave.com/res/v1/web/search"

    def __init__(self, client: httpx2.AsyncClient, api_key: str):
        super().__init__(client)
        self.api_key = api_key

    async def search(self, query: str, max_results: int = 8, freshness: str | None = None) -> list[SearchResult]:
        params = {"q": query, "count": max(1, min(max_results, 20)), "extra_snippets": "true"}
        if freshness in FRESHNESS_BRAVE:
            params["freshness"] = FRESHNESS_BRAVE[freshness]
        data = await self._request(
            "GET",
            self.endpoint,
            params=params,
            headers={"Accept": "application/json", "X-Subscription-Token": self.api_key},
        )
        results = []
        for item in (data.get("web") or {}).get("results") or []:
            url = item.get("url")
            if not url:
                continue
            results.append(
                SearchResult(
                    title=_clean(item.get("title")) or url,
                    url=url,
                    snippet=_clean(item.get("description")),
                    published=item.get("page_age") or item.get("age"),
                    extra_snippets=[_clean(s) for s in (item.get("extra_snippets") or [])][:3],
                )
            )
        return results[:max_results]


class TavilySearch(_HttpSearch):
    name = "Tavily"
    endpoint = "https://api.tavily.com/search"

    def __init__(self, client: httpx2.AsyncClient, api_key: str):
        super().__init__(client)
        self.api_key = api_key

    async def search(self, query: str, max_results: int = 8, freshness: str | None = None) -> list[SearchResult]:
        body = {
            "query": query,
            "max_results": max(1, min(max_results, 20)),
            "search_depth": "basic",
            "topic": "general",
            "include_answer": False,
            "include_raw_content": False,
            "include_published_date": True,
        }
        if freshness in FRESHNESS_TAVILY:
            body["time_range"] = FRESHNESS_TAVILY[freshness]
        data = await self._request(
            "POST", self.endpoint, json=body, headers={"Authorization": f"Bearer {self.api_key}"}
        )
        results = []
        for item in data.get("results") or []:
            url = item.get("url")
            if url:
                results.append(
                    SearchResult(
                        title=_clean(item.get("title")) or url,
                        url=url,
                        snippet=_clean(item.get("content"))[:600],
                        published=item.get("published_date"),
                    )
                )
        return results[:max_results]


class SearxngSearch(_HttpSearch):
    name = "SearXNG"

    def __init__(self, client: httpx2.AsyncClient, base_url: str):
        super().__init__(client)
        self.base_url = base_url.rstrip("/")

    async def search(self, query: str, max_results: int = 8, freshness: str | None = None) -> list[SearchResult]:
        params = {"q": query, "format": "json", "pageno": 1, "safesearch": 0}
        if freshness in FRESHNESS_SEARXNG:
            params["time_range"] = FRESHNESS_SEARXNG[freshness]
        try:
            data = await self._request("GET", f"{self.base_url}/search", params=params)
        except SearchError as exc:
            if "HTTP 403" in str(exc) or "rejected the API key" in str(exc):
                raise SearchError(
                    "SearXNG refused JSON output (HTTP 403). Enable it in settings.yml: search: formats: [html, json]"
                ) from None
            raise
        results = []
        seen = set()
        for item in data.get("results") or []:
            url = item.get("url")
            if not url or url in seen:
                continue
            seen.add(url)
            results.append(
                SearchResult(
                    title=_clean(item.get("title")) or url,
                    url=url,
                    snippet=_clean(item.get("content"))[:600],
                    published=item.get("publishedDate"),
                )
            )
        return results[:max_results]


class UnconfiguredSearch:
    def __init__(self, reason: str):
        self.name = "none"
        self.reason = reason

    async def search(self, query: str, max_results: int = 8, freshness: str | None = None) -> list[SearchResult]:
        raise SearchNotConfigured(self.reason)


def build_search_provider(settings: Settings):
    """Return (provider, http_client). The client must be closed on shutdown."""
    timeout = httpx2.Timeout(20.0, connect=10.0)
    choice = settings.search_provider
    if choice == "none":
        return UnconfiguredSearch("Web search is turned off (SEARCH_PROVIDER=none)."), None
    if choice == "brave":
        if not settings.brave_api_key:
            return UnconfiguredSearch(
                "Web search is not configured: set BRAVE_API_KEY in .env (or choose another SEARCH_PROVIDER)."
            ), None
        client = httpx2.AsyncClient(timeout=timeout, proxy=settings.outbound_proxy, trust_env=False)
        return BraveSearch(client, settings.brave_api_key.get_secret_value()), client
    if choice == "tavily":
        if not settings.tavily_api_key:
            return UnconfiguredSearch(
                "Web search is not configured: set TAVILY_API_KEY in .env (or choose another SEARCH_PROVIDER)."
            ), None
        client = httpx2.AsyncClient(timeout=timeout, proxy=settings.outbound_proxy, trust_env=False)
        return TavilySearch(client, settings.tavily_api_key.get_secret_value()), client
    if choice == "searxng":
        if not settings.searxng_base_url:
            return UnconfiguredSearch("SEARCH_PROVIDER=searxng but SEARXNG_BASE_URL is not set."), None
        proxy = None if is_local_endpoint(settings.searxng_base_url) else settings.outbound_proxy
        client = httpx2.AsyncClient(timeout=timeout, proxy=proxy, trust_env=False)
        return SearxngSearch(client, settings.searxng_base_url), client
    return UnconfiguredSearch(f"Unknown SEARCH_PROVIDER '{choice}'."), None
