"""JavaScript rendering with headless Chromium (Playwright), behind the filtering proxy.

The browser is started lazily, renders one page at a time, never downloads files,
blocks service workers, skips images/media/fonts, and is shut down after a short idle
period so it does not hold memory on the home computer.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

from .fetcher import USER_AGENT
from .filter_proxy import FilteringProxy
from .guard import BlockedURL, NetGuard

log = logging.getLogger(__name__)

IDLE_SHUTDOWN_SECONDS = 180
MAX_HTML_CHARS = 3_000_000
SKIP_RESOURCE_TYPES = {"image", "media", "font"}
CHROMIUM_ARGS = [
    "--disable-quic",
    "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
    "--webrtc-ip-handling-policy=disable_non_proxied_udp",
    "--disable-background-networking",
    "--disable-component-update",
    "--disable-default-apps",
    "--disable-sync",
    "--no-first-run",
    "--dns-prefetch-disable",
]


class BrowserUnavailable(Exception):
    pass


@dataclass
class RenderResult:
    url: str
    final_url: str
    status_code: int | None
    title: str | None
    html: str
    blocked: list[str] = field(default_factory=list)


class BrowserRenderer:
    def __init__(
        self,
        guard: NetGuard,
        *,
        upstream_proxy: str | None = None,
        timeout: float = 30.0,
        executable_path: str | None = None,
        channel: str | None = None,
    ):
        self.guard = guard
        self.upstream_proxy = upstream_proxy
        self.timeout = timeout
        self.executable_path = executable_path
        self.channel = channel
        self._lock = asyncio.Lock()
        self._render_slot = asyncio.Semaphore(1)
        self._playwright = None
        self._browser = None
        self._proxy: FilteringProxy | None = None
        self._last_used = 0.0
        self._idle_task: asyncio.Task | None = None
        self.last_error: str | None = None

    async def _ensure_started(self):
        async with self._lock:
            if self._browser is not None and self._browser.is_connected():
                return
            await self._shutdown_locked()
            try:
                from playwright.async_api import async_playwright
            except ImportError as exc:  # pragma: no cover - dependency is pinned
                raise BrowserUnavailable("Playwright is not installed") from exc
            self._proxy = FilteringProxy(self.guard, upstream_proxy=self.upstream_proxy)
            port = await self._proxy.start()
            self._playwright = await async_playwright().start()
            launch_kwargs = {
                "headless": True,
                "args": CHROMIUM_ARGS,
                "proxy": {"server": f"http://127.0.0.1:{port}", "username": self._proxy.username,
                          "password": self._proxy.password},  # fmt: skip
            }
            if self.executable_path:
                launch_kwargs["executable_path"] = self.executable_path
            elif self.channel:
                launch_kwargs["channel"] = self.channel
            try:
                self._browser = await self._playwright.chromium.launch(**launch_kwargs)
            except Exception as exc:
                message = str(exc).splitlines()[0][:300]
                await self._shutdown_locked()
                if "Executable doesn't exist" in str(exc) or "playwright install" in str(exc):
                    message = "Chromium for Playwright is not installed. Run: uv run playwright install chromium"
                self.last_error = message
                raise BrowserUnavailable(message) from None
            self.last_error = None
            if self._idle_task is None or self._idle_task.done():
                self._idle_task = asyncio.create_task(self._idle_watch())

    async def render(self, url: str) -> RenderResult:
        checked = await self.guard.check_destination(url, resolve=self.upstream_proxy is None)
        async with self._render_slot:
            await self._ensure_started()
            assert self._browser is not None and self._proxy is not None
            self._last_used = time.monotonic()
            before_blocked = len(self._proxy.blocked)
            context = await self._browser.new_context(
                service_workers="block",
                accept_downloads=False,
                user_agent=USER_AGENT,
                viewport={"width": 1280, "height": 900},
                locale="en-US",
                java_script_enabled=True,
            )
            try:
                await context.route("**/*", self._route)
                page = await context.new_page()
                page.set_default_timeout(self.timeout * 1000)
                response = await page.goto(checked.url, wait_until="domcontentloaded", timeout=self.timeout * 1000)
                try:
                    await page.wait_for_load_state("networkidle", timeout=6000)
                except Exception:
                    pass  # busy pages never go idle; what has rendered is enough
                html = (await page.content())[:MAX_HTML_CHARS]
                title = await page.title()
                final_url = page.url
                status = response.status if response is not None else None
            finally:
                await context.close()
                self._last_used = time.monotonic()
            blocked = list(self._proxy.blocked)[before_blocked:] if len(self._proxy.blocked) >= before_blocked else []
            return RenderResult(url=url, final_url=final_url, status_code=status, title=title or None, html=html,
                                blocked=blocked)  # fmt: skip

    async def _route(self, route):
        request = route.request
        if request.resource_type in SKIP_RESOURCE_TYPES:
            await route.abort()
            return
        target = request.url
        if target.startswith(("data:", "blob:", "about:")):
            await route.continue_()
            return
        try:
            self.guard.check_url(target)
        except BlockedURL:
            await route.abort("blockedbyclient")
            return
        await route.continue_()

    async def _idle_watch(self):
        try:
            while True:
                await asyncio.sleep(30)
                if self._browser is None:
                    return
                if time.monotonic() - self._last_used > IDLE_SHUTDOWN_SECONDS and not self._render_slot.locked():
                    async with self._lock:
                        await self._shutdown_locked()
                    return
        except asyncio.CancelledError:
            return

    async def _shutdown_locked(self):
        browser, self._browser = self._browser, None
        playwright, self._playwright = self._playwright, None
        proxy, self._proxy = self._proxy, None
        if browser is not None:
            try:
                await browser.close()
            except Exception:
                pass
        if playwright is not None:
            try:
                await playwright.stop()
            except Exception:
                pass
        if proxy is not None:
            await proxy.stop()

    async def aclose(self):
        if self._idle_task is not None:
            self._idle_task.cancel()
        async with self._lock:
            await self._shutdown_locked()

    def status(self) -> dict:
        return {"running": self._browser is not None, "last_error": self.last_error}
