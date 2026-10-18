"""LIVE tests: real internet, real providers. Run with:  HOME_AGENT_LIVE=1 uv run pytest tests/live -s

Each test skips itself (and says why) when its credentials or tools are missing. They read
configuration from the environment / .env exactly like the app does (MODEL_PROVIDER,
ANTHROPIC_API_KEY, SEARCH_PROVIDER + key or SEARXNG_BASE_URL, OUTBOUND_PROXY,
BROWSER_EXECUTABLE_PATH ...). The end-to-end test spends real model/search credits.
"""

from __future__ import annotations

import asyncio
import os
import time

import pytest
from fastapi.testclient import TestClient

from home_agent.config import load_settings
from home_agent.db import Database
from home_agent.llm.base import ModelConfigError, ToolSpec
from home_agent.net.browser import BrowserRenderer, BrowserUnavailable
from home_agent.net.fetcher import WebFetcher
from home_agent.net.guard import NetGuard
from home_agent.search import SearchNotConfigured, build_search_provider
from home_agent.security.auth import AuthService
from home_agent.services.container import Services
from home_agent.web.app import create_app

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(os.environ.get("HOME_AGENT_LIVE") != "1", reason="set HOME_AGENT_LIVE=1 to run live tests"),
]


@pytest.fixture
def settings(tmp_path):
    return load_settings(data_dir=tmp_path / "data", allowed_hosts="testserver", secret_key="live-test-" + "k" * 40)


def test_live_fetch_public_page_and_block_private_redirect(settings):
    fetcher = WebFetcher(NetGuard(settings.extra_allowed_networks), proxy=settings.outbound_proxy, timeout=30)

    async def run():
        page = await fetcher.fetch("https://example.com/")
        redirect = await fetcher.fetch("https://httpbin.org/redirect-to?url=http%3A%2F%2F127.0.0.1%3A8080%2F")
        await fetcher.aclose()
        return page, redirect

    page, redirect = asyncio.run(run())
    assert page.ok and page.title == "Example Domain"
    if redirect.status_code is None and redirect.error and "Blocked" not in redirect.error:
        pytest.skip(f"httpbin.org unreachable: {redirect.error}")
    assert not redirect.ok and "Blocked redirect target" in redirect.error


def test_live_browser_render_and_redirect_block(settings):
    renderer = BrowserRenderer(NetGuard(settings.extra_allowed_networks), upstream_proxy=settings.outbound_proxy,
                               executable_path=settings.browser_executable_path, channel=settings.browser_channel)  # fmt: skip

    async def run():
        try:
            ok = await renderer.render("https://example.com/")
            bad = await renderer.render("https://httpbin.org/redirect-to?url=http%3A%2F%2F169.254.169.254%2Flatest%2Fmeta-data%2F")
            return ok, bad
        finally:
            await renderer.aclose()

    try:
        ok, bad = asyncio.run(run())
    except BrowserUnavailable as exc:
        pytest.skip(str(exc))
    assert ok.title == "Example Domain" and "Example Domain" in ok.html
    assert any("169.254.169.254" in b for b in bad.blocked), bad.blocked


def test_live_search(settings):
    provider, client = build_search_provider(settings)

    async def run():
        try:
            return await provider.search("AI robotics hackathon 2026", 5)
        finally:
            if client is not None:
                await client.aclose()

    try:
        results = asyncio.run(run())
    except SearchNotConfigured as exc:
        pytest.skip(f"credential-blocked: {exc}")
    assert results and all(r.url.startswith("http") for r in results)


def test_live_model_tool_call(settings):
    db = Database(settings.db_path)
    db.migrate()
    services = Services(settings, db)
    try:
        provider = services.get_provider(settings.model_provider)
    except ModelConfigError as exc:
        pytest.skip(f"credential-blocked: {exc}")
    spec = ToolSpec("web_search", "Search the web for current information.",
                    {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]})  # fmt: skip
    turn = asyncio.run(provider.complete(
        system="You are a research assistant. Use the web_search tool to answer questions about current events.",
        messages=provider.initial_messages([], "Use web_search to find AI hackathons happening in 2026."),
        tools=[spec], allow_tools=True))  # fmt: skip
    assert turn.tool_calls and turn.tool_calls[0].name == "web_search", (turn.text, turn.stop_reason)
    print(f"\nmodel={provider.model} tokens={turn.usage.total_tokens} cost=${turn.usage.cost_usd:.4f} call={turn.tool_calls[0].input}")


def test_live_end_to_end(settings):
    """request -> real search -> page reading -> cited answer -> saved opportunity -> email draft."""
    db = Database(settings.db_path)
    db.migrate()
    services = Services(settings, db)
    try:
        services.get_provider(settings.model_provider)
    except ModelConfigError as exc:
        pytest.skip(f"credential-blocked: {exc}")
    if getattr(services.search, "name", "none") == "none":
        pytest.skip(f"credential-blocked: {services.search.reason}")
    AuthService(db).set_password("live test password")
    app = create_app(settings, services=services, use_lock=False)
    prompt = os.environ.get("HOME_AGENT_LIVE_PROMPT", (
        "Find one upcoming AI or robotics hackathon or student competition (deadline in the future) that an international "
        "student based in Shanghai could enter. Search, read its official page, save it with save_opportunity, and draft "
        "a short email to the organizers asking whether international students can participate. Keep it brief."))  # fmt: skip
    with TestClient(app, base_url="http://testserver") as client:
        csrf = client.post("/api/auth/login", json={"password": "live test password"}).json()["csrf_token"]
        headers = {"X-CSRF-Token": csrf}
        conv = client.post("/api/conversations", json={}, headers=headers).json()
        task_id = client.post(f"/api/conversations/{conv['id']}/messages", json={"content": prompt}, headers=headers).json()["task"]["id"]
        deadline = time.time() + settings.task_max_seconds + 120
        task = None
        seen = 0
        while time.time() < deadline:
            task = client.get(f"/api/tasks/{task_id}").json()
            for event in task["events"][seen:]:
                print(f"  [{event['type']}] {event['summary']}")
            seen = len(task["events"])
            if task["status"] in ("succeeded", "failed", "cancelled"):
                break
            time.sleep(3)
        assert task and task["status"] == "succeeded", task and (task["error"] or task["status_detail"])
        print("\n--- answer ---\n" + task["result_text"])
        read = [s for s in task["sources"] if s["kind"] in ("fetched", "rendered") and s["ok"]]
        assert task["searches"] >= 1 and read, "the agent must search and read at least one page"
        opps = client.get("/api/opportunities").json()["items"]
        drafts = client.get("/api/drafts").json()["items"]
        print(f"\nsaved opportunities: {[(o['name'], o['verification_status']) for o in opps]}")
        print(f"drafts: {[(d['subject'], d['status'], d['warnings']) for d in drafts]}")
        print(f"cost: ${task['cost_usd']:.4f}, steps {task['steps']}, searches {task['searches']}, pages {task['fetches']}")
        assert all(d["status"] == "draft" for d in drafts)  # never sent
