"""Long-lived application services shared by the web app, worker and scheduler."""

from __future__ import annotations

import logging
from typing import Callable

from ..config import Settings
from ..db import Database
from ..llm import build_provider
from ..llm.base import ModelProvider
from ..net.browser import BrowserRenderer
from ..net.fetcher import WebFetcher
from ..net.guard import NetGuard
from ..search import build_search_provider
from ..security.crypto import SecretBox
from ..util import valid_timezone
from . import profile as profile_service
from .gmail import GmailService
from .workspace import Workspace

log = logging.getLogger(__name__)


class Services:
    def __init__(
        self,
        settings: Settings,
        db: Database,
        *,
        search=None,
        fetcher: WebFetcher | None = None,
        browser: BrowserRenderer | None = None,
        gmail: GmailService | None = None,
        provider_factory: Callable[[str], ModelProvider] | None = None,
        guard: NetGuard | None = None,
    ):
        self.settings = settings
        self.db = db
        self.guard = guard or NetGuard(settings.extra_allowed_networks)
        self.fetcher = fetcher or WebFetcher(
            self.guard, proxy=settings.outbound_proxy, timeout=settings.fetch_timeout, max_bytes=settings.fetch_max_bytes
        )
        self.browser = browser or BrowserRenderer(
            self.guard,
            upstream_proxy=settings.outbound_proxy,
            timeout=settings.browser_timeout,
            executable_path=settings.browser_executable_path,
            channel=settings.browser_channel,
        )
        self._search_client = None
        if search is None:
            search, self._search_client = build_search_provider(settings)
        self.search = search
        self.workspace = Workspace(settings.workspace_dir)
        self.box = SecretBox(settings.secret_key.get_secret_value()) if settings.secret_key else None
        self.gmail = gmail or GmailService(settings, db, self.box)
        self._provider_factory = provider_factory
        self._providers: dict[str, ModelProvider] = {}
        self.worker = None
        self.scheduler = None

    # -- runtime settings (editable in the UI, stored in the database) ---------------------
    def runtime(self, key: str):
        overrides = profile_service.get_runtime_settings(self.db)
        value = overrides.get(key)
        if key == "timezone":
            return value if value and valid_timezone(value) else self.settings.timezone
        if key == "model_provider":
            return value or self.settings.model_provider
        if key == "task_max_cost_usd":
            return float(value) if value is not None else self.settings.task_max_cost_usd
        if key == "daily_max_cost_usd":
            return float(value) if value is not None else self.settings.daily_max_cost_usd
        if key == "anthropic_effort":
            return value or self.settings.anthropic_effort
        raise KeyError(key)

    def effective_settings(self) -> Settings:
        return self.settings.model_copy(update={"anthropic_effort": self.runtime("anthropic_effort")})

    def get_provider(self, name: str) -> ModelProvider:
        if name in self._providers:
            return self._providers[name]
        if self._provider_factory is not None:
            provider = self._provider_factory(name)
        else:
            provider = build_provider(self.effective_settings(), name)
        self._providers[name] = provider
        return provider

    async def reset_providers(self) -> None:
        providers, self._providers = self._providers, {}
        for provider in providers.values():
            try:
                await provider.aclose()
            except Exception:
                pass

    async def aclose(self) -> None:
        await self.reset_providers()
        await self.browser.aclose()
        await self.fetcher.aclose()
        await self.gmail.aclose()
        if self._search_client is not None:
            await self._search_client.aclose()
