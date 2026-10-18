"""Email approval and duplicate-send protection. Gmail is a MOCK (httpx2.MockTransport)."""

from __future__ import annotations

import asyncio
import base64
import json
import threading

import httpx2
from conftest import make_settings

from home_agent.db import Database, in_seconds
from home_agent.security.crypto import SecretBox
from home_agent.services import drafts as drafts_service
from home_agent.services.gmail import SCOPE, GmailService
from home_agent.services.workspace import Workspace


class FakeGmail:
    """In-memory Gmail drafts API. mode for drafts/send: ok | timeout | sent_then_timeout | error500 | error400."""

    def __init__(self):
        self.drafts: dict[str, str] = {}
        self.sent: list[dict] = []
        self.send_calls = 0
        self.mode = "ok"
        self.counter = 0
        self.lock = threading.Lock()

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        url = str(request.url)
        if url.startswith("https://oauth2.googleapis.com/token"):
            return httpx2.Response(200, json={"access_token": "ya29.fake", "expires_in": 3600, "scope": SCOPE})
        if url.endswith("/profile"):
            return httpx2.Response(200, json={"emailAddress": "me@example.com"})
        if url.endswith("/drafts/send"):
            with self.lock:
                self.send_calls += 1
                body = json.loads(request.content)
                if self.mode == "timeout":
                    raise httpx2.ReadTimeout("timed out", request=request)
                if self.mode == "error500":
                    return httpx2.Response(500, json={"error": {"message": "backend error"}})
                if self.mode == "error400":
                    return httpx2.Response(400, json={"error": {"message": "Invalid To header"}})
                if body["id"] not in self.drafts:
                    return httpx2.Response(404, json={"error": {"message": "not found"}})
                self.drafts.pop(body["id"])
                self.sent.append({"raw": body["message"]["raw"]})
                if self.mode == "sent_then_timeout":
                    raise httpx2.ReadTimeout("response lost", request=request)
                return httpx2.Response(200, json={"id": f"msg{len(self.sent)}"})
        if url.endswith("/drafts") and request.method == "POST":
            with self.lock:
                self.counter += 1
                draft_id = f"r{self.counter}"
                self.drafts[draft_id] = json.loads(request.content)["message"]["raw"]
            return httpx2.Response(200, json={"id": draft_id})
        if "/drafts/" in url:
            draft_id = url.split("/drafts/")[1].split("?")[0]
            if request.method == "GET":
                return httpx2.Response(200, json={"id": draft_id}) if draft_id in self.drafts else httpx2.Response(404, json={})
            if request.method == "PUT":
                if draft_id not in self.drafts:
                    return httpx2.Response(404, json={})
                self.drafts[draft_id] = json.loads(request.content)["message"]["raw"]
                return httpx2.Response(200, json={"id": draft_id})
        return httpx2.Response(404, json={"error": {"message": f"unexpected {request.method} {url}"}})


def make_gmail(settings, db, fake: FakeGmail) -> GmailService:
    service = GmailService(settings, db, SecretBox(settings.secret_key.get_secret_value()), transport=httpx2.MockTransport(fake.handler))
    service._store_token({"access_token": "ya29.fake", "expires_in": 3600, "refresh_token": "1//refresh", "scope": SCOPE}, account="me@example.com")
    return service


def send_settings(tmp_path, **kw):
    return make_settings(tmp_path, gmail_client_id="id.apps.googleusercontent.com", gmail_client_secret="secret-xyz",
                         gmail_allow_send=True, **kw)  # fmt: skip


def setup(tmp_path, fake=None, **kw):
    settings = send_settings(tmp_path, **kw)
    db = Database(settings.db_path)
    db.migrate()
    fake = fake or FakeGmail()
    gmail = make_gmail(settings, db, fake)
    workspace = Workspace(settings.workspace_dir)
    draft = drafts_service.create_draft(db, to=["organizer@example.org"], subject="Eligibility question", body="Hello!")
    return settings, db, fake, gmail, workspace, draft


def current_hash(db, workspace, draft_id):
    return drafts_service.present(drafts_service.get_draft(db, draft_id), workspace)["content_hash"]


def test_send_requires_approval_and_exact_content(tmp_path):
    settings, db, fake, gmail, ws, draft = setup(tmp_path)
    h = current_hash(db, ws, draft["id"])

    async def run():
        # not approved -> refused before any Gmail call
        try:
            await drafts_service.send(db, ws, gmail, draft["id"], h, send_enabled=True)
            raise AssertionError("sent without approval")
        except drafts_service.DraftError as exc:
            assert exc.status == 409
        # approving a stale version is refused
        try:
            drafts_service.approve(db, ws, draft["id"], "0" * 64, send_enabled=True)
            raise AssertionError("approved stale content")
        except drafts_service.DraftError as exc:
            assert "changed" in str(exc)
        drafts_service.approve(db, ws, draft["id"], h, send_enabled=True)
        # editing after approval invalidates it
        drafts_service.update_draft(db, ws, draft["id"], {"body": "Hello! (edited)"})
        assert drafts_service.get_draft(db, draft["id"])["status"] == "draft"
        try:
            await drafts_service.send(db, ws, gmail, draft["id"], h, send_enabled=True)
            raise AssertionError("sent edited content")
        except drafts_service.DraftError:
            pass
        assert fake.send_calls == 0
        # approve the new version and send it once
        h2 = current_hash(db, ws, draft["id"])
        drafts_service.approve(db, ws, draft["id"], h2, send_enabled=True)
        sent = await drafts_service.send(db, ws, gmail, draft["id"], h2, send_enabled=True)
        assert sent["status"] == "sent" and fake.send_calls == 1
        raw = base64.urlsafe_b64decode(fake.sent[0]["raw"]).decode()
        assert "Hello! (edited)" in raw and "organizer@example.org" in raw
        # retrying the same request does not send again
        try:
            await drafts_service.send(db, ws, gmail, draft["id"], h2, send_enabled=True)
            raise AssertionError("sent twice")
        except drafts_service.DraftError as exc:
            assert exc.status == 409
        assert fake.send_calls == 1
        # a sent draft cannot be edited
        try:
            drafts_service.update_draft(db, ws, draft["id"], {"body": "x"})
            raise AssertionError("edited a sent draft")
        except drafts_service.DraftError:
            pass

    asyncio.run(run())


def test_sending_disabled_by_default(tmp_path):
    settings, db, fake, gmail, ws, draft = setup(tmp_path)
    h = current_hash(db, ws, draft["id"])
    try:
        drafts_service.approve(db, ws, draft["id"], h, send_enabled=False)
        raise AssertionError("approved while sending disabled")
    except drafts_service.DraftError as exc:
        assert exc.status == 403


def test_attachment_change_invalidates_approval(tmp_path):
    settings, db, fake, gmail, ws, draft = setup(tmp_path)
    ws.write_text("cv.md", "version 1")
    drafts_service.update_draft(db, ws, draft["id"], {"attachments": ["cv.md"]})
    h = current_hash(db, ws, draft["id"])
    drafts_service.approve(db, ws, draft["id"], h, send_enabled=True)
    ws.write_text("cv.md", "version 2 - changed after approval")

    async def run():
        try:
            await drafts_service.send(db, ws, gmail, draft["id"], h, send_enabled=True)
            raise AssertionError("sent a changed attachment")
        except drafts_service.DraftError as exc:
            assert "differs" in str(exc)

    asyncio.run(run())
    assert fake.send_calls == 0


def test_expired_approval(tmp_path):
    settings, db, fake, gmail, ws, draft = setup(tmp_path)
    h = current_hash(db, ws, draft["id"])
    drafts_service.approve(db, ws, draft["id"], h, send_enabled=True)
    db.execute("UPDATE drafts SET approval_expires_at = ? WHERE id = ?", (in_seconds(-5), draft["id"]))

    async def run():
        try:
            await drafts_service.send(db, ws, gmail, draft["id"], h, send_enabled=True)
            raise AssertionError("sent with expired approval")
        except drafts_service.DraftError as exc:
            assert "expired" in str(exc)

    asyncio.run(run())


def test_concurrent_send_requests_send_once(tmp_path):
    settings, db, fake, gmail, ws, draft = setup(tmp_path)
    h = current_hash(db, ws, draft["id"])
    drafts_service.approve(db, ws, draft["id"], h, send_enabled=True)

    async def run():
        results = await asyncio.gather(
            *[drafts_service.send(db, ws, gmail, draft["id"], h, send_enabled=True) for _ in range(5)],
            return_exceptions=True,
        )
        return results

    results = asyncio.run(run())
    assert sum(1 for r in results if isinstance(r, dict)) == 1
    assert fake.send_calls == 1 and len(fake.sent) == 1


def test_lost_response_is_resolved_without_resending(tmp_path):
    fake = FakeGmail()
    fake.mode = "sent_then_timeout"
    settings, db, fake, gmail, ws, draft = setup(tmp_path, fake)
    h = current_hash(db, ws, draft["id"])
    drafts_service.approve(db, ws, draft["id"], h, send_enabled=True)

    async def run():
        try:
            await drafts_service.send(db, ws, gmail, draft["id"], h, send_enabled=True)
        except drafts_service.DraftError:
            pass

    asyncio.run(run())
    row = drafts_service.get_draft(db, draft["id"])
    assert row["status"] == "sent" and "Most likely sent" in row["last_error"]
    assert fake.send_calls == 1


def test_failed_send_needs_new_approval(tmp_path):
    fake = FakeGmail()
    fake.mode = "timeout"  # Gmail never got it (draft still exists)
    settings, db, fake, gmail, ws, draft = setup(tmp_path, fake)
    h = current_hash(db, ws, draft["id"])
    drafts_service.approve(db, ws, draft["id"], h, send_enabled=True)

    async def run():
        try:
            await drafts_service.send(db, ws, gmail, draft["id"], h, send_enabled=True)
        except drafts_service.DraftError:
            pass
        row = drafts_service.get_draft(db, draft["id"])
        assert row["status"] == "failed" and row["approved_hash"] is None
        fake.mode = "ok"
        try:
            await drafts_service.send(db, ws, gmail, draft["id"], h, send_enabled=True)
            raise AssertionError("re-sent without new approval")
        except drafts_service.DraftError:
            pass

    asyncio.run(run())
    assert fake.send_calls == 1 and not fake.sent


def test_restart_during_send_never_resends(tmp_path):
    """Simulate a crash after the draft was claimed for sending ('sending' in the DB)."""
    settings, db, fake, gmail, ws, draft = setup(tmp_path)
    h = current_hash(db, ws, draft["id"])
    drafts_service.approve(db, ws, draft["id"], h, send_enabled=True)
    raw = drafts_service.build_mime(drafts_service.get_draft(db, draft["id"]), ws, "<x@home-agent.local>")
    gmail_id = asyncio.run(gmail.create_draft(raw))
    db.execute("UPDATE drafts SET status = 'sending', gmail_draft_id = ? WHERE id = ?", (gmail_id, draft["id"]))
    # Case 1: crash happened before Gmail sent it -> the Gmail draft still exists
    asyncio.run(drafts_service.recover_interrupted_sends(db, gmail))
    assert drafts_service.get_draft(db, draft["id"])["status"] == "failed"
    assert fake.send_calls == 0
    # Case 2: crash happened after Gmail sent it -> the Gmail draft is gone
    db.execute("UPDATE drafts SET status = 'sending' WHERE id = ?", (draft["id"],))
    fake.drafts.pop(gmail_id)
    asyncio.run(drafts_service.recover_interrupted_sends(db, gmail))
    assert drafts_service.get_draft(db, draft["id"])["status"] == "sent"
    assert fake.send_calls == 0


def test_api_approval_flow_and_gmail_draft_sync(harness_factory, tmp_path):
    fake = FakeGmail()
    h = harness_factory(settings_overrides={"gmail_client_id": "id", "gmail_client_secret": "secret", "gmail_allow_send": True})
    with h:
        h.login()
        gmail = make_gmail(h.settings, h.db, fake)
        h.services.gmail = gmail
        d = h.post("/api/drafts", json={"to": ["a@example.org"], "subject": "Hi", "body": "Body"}).json()
        assert h.post(f"/api/drafts/{d['id']}/send", json={"content_hash": d["content_hash"]}).status_code == 409
        synced = h.post(f"/api/drafts/{d['id']}/gmail").json()
        assert synced["gmail_draft_id"] and len(fake.drafts) == 1
        again = h.post(f"/api/drafts/{d['id']}/gmail").json()  # update, not a second draft
        assert again["gmail_draft_id"] == synced["gmail_draft_id"] and len(fake.drafts) == 1
        approved = h.post(f"/api/drafts/{d['id']}/approve", json={"content_hash": d["content_hash"]}).json()
        assert approved["approval_valid"]
        edited = h.put(f"/api/drafts/{d['id']}", json={"subject": "Hi again"}).json()
        assert edited["status"] == "draft" and not edited["approval_valid"]
        r = h.post(f"/api/drafts/{d['id']}/send", json={"content_hash": edited["content_hash"]})
        assert r.status_code == 409
        h.post(f"/api/drafts/{d['id']}/approve", json={"content_hash": edited["content_hash"]})
        sent = h.post(f"/api/drafts/{d['id']}/send", json={"content_hash": edited["content_hash"]})
        assert sent.status_code == 200 and sent.json()["status"] == "sent"
        assert fake.send_calls == 1
        audit = [r["action"] for r in h.db.all("SELECT action FROM audit_log ORDER BY id")]
        assert "draft.approved" in audit and "draft.approval_invalidated" in audit and "draft.sent" in audit


def test_agent_has_no_send_capability():
    from home_agent.agent.tools import TOOLS

    names = {t.name for t in TOOLS}
    assert not any("send" in n for n in names)
    assert "draft_email" in names
