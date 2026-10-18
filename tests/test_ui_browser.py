"""Drives the real web UI in headless Chromium against a live server.

MOCKED: the model (scripted), search results and web pages. Real: HTTP server, auth,
cookies/CSRF, the JavaScript UI, the worker. Skipped when Chromium is not installed.
"""

from __future__ import annotations

import json
import os
import socket
import threading
import time
from pathlib import Path

import pytest
import uvicorn
from conftest import PASSWORD, make_settings
from helpers import FakeSearch, pages_transport, make_guard
from test_workflow import OFFICIAL, PAGE, script

from home_agent.db import Database
from home_agent.net.fetcher import WebFetcher
from home_agent.search import SearchResult
from home_agent.security.auth import AuthService
from home_agent.services.container import Services
from home_agent.web.app import create_app

pytestmark = pytest.mark.browser
CHROMIUM = os.environ.get("BROWSER_EXECUTABLE_PATH") or ("/opt/pw-browsers/chromium" if Path("/opt/pw-browsers/chromium").exists() else None)
SHOTS = Path(os.environ.get("HOME_AGENT_SCREENSHOTS", "")) if os.environ.get("HOME_AGENT_SCREENSHOTS") else None


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def live_server(tmp_path):
    settings = make_settings(tmp_path, port=_free_port())
    db = Database(settings.db_path)
    db.migrate()
    guard = make_guard()
    services = Services(
        settings, db,
        search=FakeSearch([SearchResult(title="Shanghai AI & Robotics Hackathon 2026", url=OFFICIAL, snippet="AI hackathon")]),
        fetcher=WebFetcher(guard, transport=pages_transport({OFFICIAL: (200, "text/html", PAGE)})),
        guard=guard,
        provider_factory=lambda name: script(),
    )  # fmt: skip
    AuthService(db).set_password(PASSWORD)
    app = create_app(settings, services=services, use_lock=False)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=settings.port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    yield f"http://127.0.0.1:{settings.port}"
    server.should_exit = True
    thread.join(timeout=10)


@pytest.mark.skipif(CHROMIUM is None and not os.environ.get("PLAYWRIGHT_BROWSERS_PATH"), reason="Chromium not installed")
def test_ui_end_to_end(live_server, tmp_path):
    import sqlite3

    from playwright.sync_api import sync_playwright

    errors: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM) if CHROMIUM else p.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 860})
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: errors.append(str(e)))

        page.goto(live_server + "/")
        page.fill("#password", "wrong password")
        page.click("#login-form button")
        page.wait_for_selector("#login-error:has-text('Wrong password')")
        errors.clear()  # the 401 for the wrong password is expected console noise
        page.fill("#password", PASSWORD)
        page.click("#login-form button")
        page.wait_for_selector("text=What should your agent work on?")

        page.click("text=Find AI and robotics hackathons I can enter in the next three months.")
        page.wait_for_selector("textarea[aria-label=Message]")
        page.click("button:has-text('Send')")
        page.wait_for_selector(".msg.assistant table", timeout=20000)
        assert "Shanghai AI" in page.inner_text(".msg.assistant")
        if SHOTS:
            page.screenshot(path=str(SHOTS / "chat-desktop.png"), full_page=True)

        # a notice from the save check, as written for a page that looks like a past edition
        notice = "https://hackathon.example.org/2026 may describe a past edition (the title says \"Recap\")."
        with sqlite3.connect(make_settings(tmp_path).db_path) as conn:
            conn.execute("UPDATE opportunities SET warnings = ?",
                         (json.dumps([{"kind": "past_edition", "url": OFFICIAL, "reasons": [], "message": notice}]),))
        page.goto(live_server + "/#/opportunities")
        page.wait_for_selector("table.data td.name")
        assert "Shanghai AI" in page.inner_text("table.data")
        assert page.locator("tr.is-new").count() == 1
        assert page.inner_text("table.data td.name .opp-warn summary") == "⚠ May be a past edition"
        page.click("table.data td.name .opp-warn summary")
        assert notice in page.inner_text("table.data td.name .opp-warn")
        # the fabricated cost quote: the mark says why the fact is not confirmed
        cost_mark = page.locator("table.data tbody tr td:nth-child(7) .fact-mark")
        assert cost_mark.inner_text() == "!"
        assert cost_mark.get_attribute("title") == "Claimed but not confirmed on a fetched page (quote not found on the fetched page)"
        if SHOTS:
            page.screenshot(path=str(SHOTS / "opportunities-desktop.png"), full_page=True)
        page.click("button:has-text('☆')")
        page.wait_for_selector("button:has-text('★')")

        page.goto(live_server + "/#/drafts")
        page.click(".list a.item")
        page.wait_for_selector("text=Sending")
        assert page.input_value("textarea") .startswith("Hello")
        if SHOTS:
            page.screenshot(path=str(SHOTS / "draft-desktop.png"), full_page=True)

        for route, marker in [("tasks", "Tasks"), ("schedules", "Schedules"), ("profile", "Profile & memory"),
                              ("files", "Files"), ("settings", "Settings")]:  # fmt: skip
            page.goto(f"{live_server}/#/{route}")
            page.wait_for_selector(f"h2:has-text('{marker}')")
            if SHOTS and route in ("settings", "profile"):
                page.wait_for_timeout(300)
                page.screenshot(path=str(SHOTS / f"{route}-desktop.png"), full_page=True)
        page.goto(live_server + "/#/tasks/1")
        page.wait_for_selector("text=Sources consulted")
        assert "cost could not be confirmed on the cited pages" in page.inner_text("main")
        if SHOTS:
            page.screenshot(path=str(SHOTS / "task-desktop.png"), full_page=True)

        mobile = browser.new_page(viewport={"width": 390, "height": 844}, is_mobile=True)
        mobile.on("pageerror", lambda e: errors.append(str(e)))
        mobile.goto(live_server + "/")
        mobile.fill("#password", PASSWORD)
        mobile.click("#login-form button")
        mobile.wait_for_selector(".bottomnav")
        mobile.goto(live_server + "/#/opportunities")
        mobile.wait_for_selector(".opp-card h3")
        assert mobile.inner_text(".opp-card .opp-warn") == "⚠ " + notice
        assert mobile.evaluate("document.documentElement.scrollWidth <= window.innerWidth")  # the long URL wraps
        if SHOTS:
            mobile.screenshot(path=str(SHOTS / "opportunities-mobile.png"), full_page=True)
            mobile.goto(live_server + "/#/chat/1")
            mobile.wait_for_selector(".msg.assistant")
            mobile.screenshot(path=str(SHOTS / "chat-mobile.png"), full_page=True)
        browser.close()
    assert not [e for e in errors if "favicon" not in e], errors


@pytest.mark.skipif(CHROMIUM is None and not os.environ.get("PLAYWRIGHT_BROWSERS_PATH"), reason="Chromium not installed")
def test_ui_draft_review_approve_send(tmp_path):
    """MOCKED Gmail. The UI flow: write draft -> review exact content -> approve -> edit cancels -> re-approve -> send."""
    from playwright.sync_api import sync_playwright
    from test_drafts import FakeGmail, make_gmail

    settings = make_settings(tmp_path, port=_free_port(), gmail_client_id="id", gmail_client_secret="secret", gmail_allow_send=True)
    db = Database(settings.db_path)
    db.migrate()
    fake = FakeGmail()
    services = Services(settings, db, provider_factory=lambda name: script(), gmail=make_gmail(settings, db, fake))
    AuthService(db).set_password(PASSWORD)
    app = create_app(settings, services=services, use_lock=False)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=settings.port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.05)
    base = f"http://127.0.0.1:{settings.port}"
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(executable_path=CHROMIUM) if CHROMIUM else p.chromium.launch()
            page = browser.new_page()
            page.on("dialog", lambda d: d.accept())
            page.goto(base + "/")
            page.fill("#password", PASSWORD)
            page.click("#login-form button")
            page.wait_for_selector(".sidenav")
            page.goto(base + "/#/drafts")
            page.click("button:has-text('New draft')")
            page.wait_for_selector("text=Review & approve for sending")
            inputs = page.locator(".card.stack input:not([type=checkbox])")
            inputs.nth(0).fill("organizer@example.org")
            inputs.nth(3).fill("Can international students join?")
            page.fill(".card.stack textarea", "Hello, is the hackathon open to international students in Shanghai?")
            page.click("button:has-text('Review & approve for sending')")  # saves first, then shows the exact content
            page.wait_for_selector(".review")
            assert "organizer@example.org" in page.inner_text(".review")
            approve = page.locator("button:has-text('Approve this exact email')")
            assert approve.is_disabled()
            page.check("#approve-check")
            approve.click()
            page.wait_for_selector("button:has-text('Send now')")
            # editing after approval cancels it (enforced by the server)
            page.fill(".card.stack textarea", "Edited text")
            page.click("button:has-text('Save')")
            page.wait_for_selector("text=Review & approve for sending")
            assert page.locator("button:has-text('Send now')").count() == 0
            page.click("button:has-text('Review & approve for sending')")
            page.check("#approve-check")
            page.click("button:has-text('Approve this exact email')")
            page.click("button:has-text('Send now')")
            page.wait_for_selector(".status.sent")
            browser.close()
        assert fake.send_calls == 1
        assert "Edited text" in __import__("base64").urlsafe_b64decode(fake.sent[0]["raw"]).decode()
    finally:
        server.should_exit = True
        thread.join(timeout=10)
