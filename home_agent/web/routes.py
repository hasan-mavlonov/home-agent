"""JSON API used by the web interface. Everything except sign-in requires a session."""

from __future__ import annotations

import sqlite3
from typing import Any

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, Response, UploadFile
from fastapi.responses import HTMLResponse, PlainTextResponse
from pydantic import BaseModel, Field

from ..db import loads, now_iso
from ..llm import configured_providers
from ..llm.base import ModelConfigError, ModelError
from ..search import SearchError
from ..security.auth import SESSION_COOKIE
from ..services import drafts as drafts_service
from ..services import opportunities as opp_service
from ..services import profile as profile_service
from ..services import schedules as schedule_service
from ..services.gmail import GmailError
from ..services.sources import list_task_sources
from ..services.tasks import (
    create_task,
    get_task,
    list_events,
    list_notifications,
    list_tasks,
    mark_read,
    public_task,
    request_cancel,
    spend_summary,
    unread_count,
)
from ..services.workspace import WorkspaceError
from ..util import truncate, valid_timezone
from .deps import auth_service, client_ip, require_session, services
from .render import render_markdown

public = APIRouter(prefix="/api")
api = APIRouter(prefix="/api", dependencies=[Depends(require_session)])


def _secure_cookie(request: Request) -> bool:
    mode = request.app.state.services.settings.cookie_secure
    if mode == "always":
        return True
    if mode == "never":
        return False
    return request.url.scheme == "https" or request.headers.get("x-forwarded-proto", "") == "https"


# ---------------------------------------------------------------------------------------------
# auth


class LoginBody(BaseModel):
    password: str = Field(max_length=500)


@public.get("/health")
def health():
    return {"ok": True}


@public.get("/auth/session")
def session_info(request: Request):
    auth = auth_service(request)
    session = auth.get_session(request.cookies.get(SESSION_COOKIE))
    return {
        "authenticated": session is not None,
        "password_set": auth.password_is_set(),
        "csrf_token": session.csrf_token if session else None,
    }


@public.post("/auth/login")
async def login(body: LoginBody, request: Request, response: Response):
    import asyncio

    auth = auth_service(request)
    if request.headers.get("sec-fetch-site", "") == "cross-site":
        raise HTTPException(403, "Cross-site request blocked")
    if not auth.password_is_set():
        raise HTTPException(409, "No password is set yet. On the Mac, run: uv run home-agent set-password")
    ip = client_ip(request)
    wait = auth.limiter.blocked_for(ip)
    if wait:
        raise HTTPException(429, f"Too many failed attempts. Try again in {int(wait // 60) + 1} minutes.")
    if not auth.check_password(body.password):
        auth.limiter.failure(ip)
        await asyncio.sleep(0.7)
        raise HTTPException(401, "Wrong password")
    auth.limiter.success(ip)
    token, session = auth.create_session(request.headers.get("user-agent"), ip)
    days = request.app.state.services.settings.session_days
    response.set_cookie(SESSION_COOKIE, token, max_age=days * 86400, httponly=True, samesite="strict",
                        secure=_secure_cookie(request), path="/")  # fmt: skip
    return {"ok": True, "csrf_token": session.csrf_token}


@api.post("/auth/logout")
def logout(request: Request, response: Response):
    auth_service(request).delete_session(request.cookies.get(SESSION_COOKIE))
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"ok": True}


# ---------------------------------------------------------------------------------------------
# overview / notifications


@api.get("/overview")
def overview(request: Request):
    svc = services(request)
    db = svc.db
    active = list_tasks(db, status="active", limit=20)
    return {
        "unread_notifications": unread_count(db),
        "active_tasks": [{"id": t["id"], "title": t["title"], "status": t["status"]} for t in active],
        "new_opportunities": int(db.scalar("SELECT COUNT(*) FROM opportunities WHERE seen_at IS NULL AND dismissed = 0") or 0),
        "pending_schedules": int(db.scalar("SELECT COUNT(*) FROM schedules WHERE status = 'pending_confirmation'") or 0),
        "server_time": now_iso(),
    }


@api.get("/notifications")
def notifications(request: Request, unread: bool = False):
    return {"items": list_notifications(services(request).db, unread_only=unread)}


class ReadBody(BaseModel):
    ids: list[int] | None = None


@api.post("/notifications/read")
def notifications_read(body: ReadBody, request: Request):
    return {"updated": mark_read(services(request).db, body.ids)}


# ---------------------------------------------------------------------------------------------
# conversations and chat


class ConversationBody(BaseModel):
    title: str | None = Field(None, max_length=200)


class MessageBody(BaseModel):
    content: str = Field(min_length=1, max_length=20000)


def _message_out(row: dict) -> dict:
    out = dict(row)
    if row["role"] in ("assistant", "user"):
        out["html"] = render_markdown(row["content"]) if row["role"] == "assistant" else None
    return out


@api.get("/conversations")
def conversations(request: Request):
    db = services(request).db
    rows = db.all(
        "SELECT c.*, (SELECT content FROM messages m WHERE m.conversation_id = c.id ORDER BY m.id DESC LIMIT 1) AS last_message "
        "FROM conversations c ORDER BY c.updated_at DESC, c.id DESC LIMIT 200"
    )
    for row in rows:
        row["last_message"] = truncate(row["last_message"] or "", 120)
    return {"items": rows}


@api.post("/conversations")
def create_conversation(body: ConversationBody, request: Request):
    db = services(request).db
    now = now_iso()
    conversation_id = db.insert("conversations", {"title": body.title or "New chat", "created_at": now, "updated_at": now})
    return db.one("SELECT * FROM conversations WHERE id = ?", (conversation_id,))


def _conversation_or_404(db, conversation_id: int) -> dict:
    row = db.one("SELECT * FROM conversations WHERE id = ?", (conversation_id,))
    if row is None:
        raise HTTPException(404, "Conversation not found")
    return row


@api.get("/conversations/{conversation_id}")
def conversation_detail(conversation_id: int, request: Request):
    db = services(request).db
    conversation = _conversation_or_404(db, conversation_id)
    messages = [_message_out(m) for m in db.all("SELECT * FROM messages WHERE conversation_id = ? ORDER BY id", (conversation_id,))]
    tasks = list_tasks(db, conversation_id=conversation_id, limit=200)
    return {"conversation": conversation, "messages": messages, "tasks": tasks}


@api.patch("/conversations/{conversation_id}")
def rename_conversation(conversation_id: int, body: ConversationBody, request: Request):
    db = services(request).db
    _conversation_or_404(db, conversation_id)
    db.execute("UPDATE conversations SET title = ?, updated_at = ? WHERE id = ?", ((body.title or "Chat")[:200], now_iso(), conversation_id))
    return db.one("SELECT * FROM conversations WHERE id = ?", (conversation_id,))


@api.delete("/conversations/{conversation_id}")
def delete_conversation(conversation_id: int, request: Request):
    svc = services(request)
    _conversation_or_404(svc.db, conversation_id)
    for task in list_tasks(svc.db, conversation_id=conversation_id, status="active"):
        request_cancel(svc.db, task["id"])
        if svc.worker:
            svc.worker.cancel(task["id"])
    svc.db.execute("DELETE FROM conversations WHERE id = ?", (conversation_id,))
    return {"ok": True}


def _check_provider(svc) -> str:
    name = svc.runtime("model_provider")
    try:
        svc.get_provider(name)
    except ModelConfigError as exc:
        raise HTTPException(400, f"The model is not ready: {exc}") from None
    return name


@api.post("/conversations/{conversation_id}/messages")
def post_message(conversation_id: int, body: MessageBody, request: Request):
    svc = services(request)
    db = svc.db
    conversation = _conversation_or_404(db, conversation_id)
    if list_tasks(db, conversation_id=conversation_id, status="active"):
        raise HTTPException(409, "This chat is still working on the previous request. Wait for it or cancel it.")
    provider = _check_provider(svc)
    content = body.content.strip()
    now = now_iso()
    with db.tx():
        message_id = db.insert("messages", {"conversation_id": conversation_id, "role": "user", "content": content, "created_at": now})
        task_id = create_task(db, kind="chat", title=truncate(content.splitlines()[0], 100), prompt=content,
                              conversation_id=conversation_id, user_message_id=message_id)  # fmt: skip
        db.execute("UPDATE tasks SET provider = ? WHERE id = ?", (provider, task_id))
        db.execute("UPDATE messages SET task_id = ? WHERE id = ?", (task_id, message_id))
        title = conversation["title"]
        if title in ("New chat", "", None):
            title = truncate(content.splitlines()[0], 60)
        db.execute("UPDATE conversations SET title = ?, updated_at = ? WHERE id = ?", (title, now, conversation_id))
    if svc.worker:
        svc.worker.wake()
    return {"message": _message_out(db.one("SELECT * FROM messages WHERE id = ?", (message_id,))),
            "task": public_task(get_task(db, task_id))}  # fmt: skip


# ---------------------------------------------------------------------------------------------
# tasks


@api.get("/tasks")
def tasks(request: Request, status: str | None = None, limit: int = Query(100, le=500)):
    return {"items": list_tasks(services(request).db, status=status, limit=limit)}


@api.get("/tasks/{task_id}")
def task_detail(task_id: int, request: Request):
    svc = services(request)
    db = svc.db
    task = get_task(db, task_id)
    if task is None:
        raise HTTPException(404, "Task not found")
    out = public_task(task)
    out["result_html"] = render_markdown(task["result_text"])
    out["events"] = list_events(db, task_id)
    out["sources"] = list_task_sources(db, task_id)
    out["opportunities"] = db.all(
        "SELECT id, name, verification_status, first_seen_task_id = ? AS is_new FROM opportunities WHERE last_task_id = ? OR first_seen_task_id = ?",
        (task_id, task_id, task_id),
    )
    out["drafts"] = db.all("SELECT id, subject, status FROM drafts WHERE source_task_id = ?", (task_id,))
    out["running_here"] = bool(svc.worker and task_id in svc.worker.running)
    return out


@api.get("/tasks/{task_id}/events")
def task_events(task_id: int, request: Request, after: int = 0):
    db = services(request).db
    task = get_task(db, task_id)
    if task is None:
        raise HTTPException(404, "Task not found")
    return {"task": public_task(task), "events": list_events(db, task_id, after=after)}


@api.post("/tasks/{task_id}/cancel")
def cancel_task(task_id: int, request: Request):
    svc = services(request)
    task = request_cancel(svc.db, task_id)
    if task is None:
        raise HTTPException(404, "Task not found")
    if svc.worker:
        svc.worker.cancel(task_id)
    return public_task(get_task(svc.db, task_id))


@api.post("/tasks/{task_id}/retry")
def retry_task(task_id: int, request: Request):
    svc = services(request)
    task = get_task(svc.db, task_id)
    if task is None:
        raise HTTPException(404, "Task not found")
    if task["status"] in ("queued", "running"):
        raise HTTPException(409, "The task is still active")
    provider = _check_provider(svc)
    new_id = create_task(svc.db, kind="manual" if task["kind"] == "scheduled" else task["kind"], title=task["title"],
                         prompt=task["prompt"], conversation_id=task["conversation_id"],
                         user_message_id=task["user_message_id"])  # fmt: skip
    svc.db.execute("UPDATE tasks SET provider = ? WHERE id = ?", (provider, new_id))
    if svc.worker:
        svc.worker.wake()
    return public_task(get_task(svc.db, new_id))


# ---------------------------------------------------------------------------------------------
# opportunities


class OpportunityPatch(BaseModel):
    bookmarked: bool | None = None
    dismissed: bool | None = None
    notes: str | None = Field(None, max_length=5000)
    seen: bool | None = None


@api.get("/opportunities")
def opportunities(request: Request, include_dismissed: bool = False, q: str | None = None):
    svc = services(request)
    return {"items": opp_service.list_opportunities(svc.db, svc.runtime("timezone"), include_dismissed=include_dismissed, query=q)}


@api.patch("/opportunities/{opp_id}")
def patch_opportunity(opp_id: int, body: OpportunityPatch, request: Request):
    svc = services(request)
    if opp_service.get_opportunity(svc.db, opp_id, svc.runtime("timezone")) is None:
        raise HTTPException(404, "Opportunity not found")
    opp_service.update_flags(svc.db, opp_id, bookmarked=body.bookmarked, dismissed=body.dismissed, notes=body.notes, seen=body.seen)
    return opp_service.get_opportunity(svc.db, opp_id, svc.runtime("timezone"))


@api.delete("/opportunities/{opp_id}")
def delete_opportunity(opp_id: int, request: Request):
    if not opp_service.delete_opportunity(services(request).db, opp_id):
        raise HTTPException(404, "Opportunity not found")
    return {"ok": True}


@api.post("/opportunities/mark-seen")
def opportunities_seen(request: Request):
    return {"updated": opp_service.mark_all_seen(services(request).db)}


@api.get("/opportunities/export.csv")
def opportunities_csv(request: Request):
    svc = services(request)
    return PlainTextResponse(opp_service.export_csv(svc.db, svc.runtime("timezone")), media_type="text/csv",
                             headers={"Content-Disposition": "attachment; filename=opportunities.csv"})  # fmt: skip


# ---------------------------------------------------------------------------------------------
# drafts


class DraftBody(BaseModel):
    to: list[str] | None = None
    cc: list[str] | None = None
    bcc: list[str] | None = None
    subject: str | None = Field(None, max_length=500)
    body: str | None = Field(None, max_length=50000)
    attachments: list[str] | None = None


class ApprovalBody(BaseModel):
    content_hash: str = Field(min_length=10, max_length=100)


class ResolveBody(BaseModel):
    outcome: str


def _draft_out(svc, draft: dict) -> dict:
    out = drafts_service.present(draft, svc.workspace)
    out["send_enabled"] = svc.settings.gmail_allow_send
    out["gmail_connected"] = svc.gmail.connected
    return out


def _draft_error(exc: drafts_service.DraftError):
    raise HTTPException(exc.status, str(exc)) from None


@api.get("/drafts")
def drafts(request: Request):
    svc = services(request)
    return {"items": [{**d, "send_enabled": svc.settings.gmail_allow_send} for d in drafts_service.list_drafts(svc.db, svc.workspace)],
            "gmail": svc.gmail.status()}  # fmt: skip


@api.post("/drafts")
def create_draft(body: DraftBody, request: Request):
    svc = services(request)
    try:
        draft = drafts_service.create_draft(svc.db, to=body.to or [], cc=body.cc or [], bcc=body.bcc or [],
                                            subject=body.subject or "", body=body.body or "")  # fmt: skip
        if body.attachments:
            draft = drafts_service.update_draft(svc.db, svc.workspace, draft["id"], {"attachments": body.attachments})
    except drafts_service.DraftError as exc:
        _draft_error(exc)
    return _draft_out(svc, draft)


@api.get("/drafts/{draft_id}")
def draft_detail(draft_id: int, request: Request):
    svc = services(request)
    draft = drafts_service.get_draft(svc.db, draft_id)
    if draft is None:
        raise HTTPException(404, "Draft not found")
    return _draft_out(svc, draft)


@api.put("/drafts/{draft_id}")
def update_draft(draft_id: int, body: DraftBody, request: Request):
    svc = services(request)
    fields = {k: v for k, v in body.model_dump().items() if v is not None}
    try:
        draft = drafts_service.update_draft(svc.db, svc.workspace, draft_id, fields)
    except drafts_service.DraftError as exc:
        _draft_error(exc)
    return _draft_out(svc, draft)


@api.delete("/drafts/{draft_id}")
def delete_draft(draft_id: int, request: Request):
    try:
        drafts_service.delete_draft(services(request).db, draft_id)
    except drafts_service.DraftError as exc:
        _draft_error(exc)
    return {"ok": True}


@api.post("/drafts/{draft_id}/gmail")
async def draft_to_gmail(draft_id: int, request: Request):
    svc = services(request)
    try:
        draft = await drafts_service.save_to_gmail(svc.db, svc.workspace, svc.gmail, draft_id)
    except drafts_service.DraftError as exc:
        _draft_error(exc)
    return _draft_out(svc, draft)


@api.post("/drafts/{draft_id}/approve")
def approve_draft(draft_id: int, body: ApprovalBody, request: Request):
    svc = services(request)
    try:
        draft = drafts_service.approve(svc.db, svc.workspace, draft_id, body.content_hash, send_enabled=svc.settings.gmail_allow_send)
    except drafts_service.DraftError as exc:
        _draft_error(exc)
    return _draft_out(svc, draft)


@api.post("/drafts/{draft_id}/revoke")
def revoke_draft(draft_id: int, request: Request):
    svc = services(request)
    try:
        draft = drafts_service.revoke_approval(svc.db, draft_id)
    except drafts_service.DraftError as exc:
        _draft_error(exc)
    return _draft_out(svc, draft)


@api.post("/drafts/{draft_id}/send")
async def send_draft(draft_id: int, body: ApprovalBody, request: Request):
    svc = services(request)
    try:
        draft = await drafts_service.send(svc.db, svc.workspace, svc.gmail, draft_id, body.content_hash,
                                          send_enabled=svc.settings.gmail_allow_send)  # fmt: skip
    except drafts_service.DraftError as exc:
        _draft_error(exc)
    return _draft_out(svc, draft)


@api.post("/drafts/{draft_id}/resolve")
def resolve_draft(draft_id: int, body: ResolveBody, request: Request):
    svc = services(request)
    try:
        draft = drafts_service.mark_resolved(svc.db, draft_id, body.outcome)
    except drafts_service.DraftError as exc:
        _draft_error(exc)
    return _draft_out(svc, draft)


# ---------------------------------------------------------------------------------------------
# schedules


class ScheduleBody(BaseModel):
    name: str | None = Field(None, max_length=200)
    prompt: str | None = Field(None, max_length=4000)
    recurrence: dict[str, Any] | None = None
    timezone: str | None = None


def _schedule_error(exc: Exception):
    raise HTTPException(400, str(exc)) from None


@api.get("/schedules")
def schedules(request: Request):
    svc = services(request)
    items = schedule_service.list_schedules(svc.db)
    for item in items:
        last = svc.db.one("SELECT id, status, new_opportunities, finished_at FROM tasks WHERE schedule_id = ? ORDER BY id DESC LIMIT 1", (item["id"],))
        item["last_task"] = last
    return {"items": items, "timezone": svc.runtime("timezone")}


@api.post("/schedules")
def create_schedule(body: ScheduleBody, request: Request):
    svc = services(request)
    try:
        return schedule_service.create_schedule(svc.db, name=body.name or "", prompt=body.prompt or "",
                                                recurrence=body.recurrence or {}, timezone=body.timezone or svc.runtime("timezone"),
                                                created_by="user")  # fmt: skip
    except schedule_service.ScheduleError as exc:
        _schedule_error(exc)


@api.put("/schedules/{schedule_id}")
def update_schedule(schedule_id: int, body: ScheduleBody, request: Request):
    fields = {k: v for k, v in body.model_dump().items() if v is not None}
    try:
        return schedule_service.update_schedule(services(request).db, schedule_id, fields)
    except schedule_service.ScheduleError as exc:
        _schedule_error(exc)


@api.post("/schedules/{schedule_id}/activate")
def activate_schedule(schedule_id: int, request: Request):
    try:
        return schedule_service.set_status(services(request).db, schedule_id, "active")
    except schedule_service.ScheduleError as exc:
        _schedule_error(exc)


@api.post("/schedules/{schedule_id}/pause")
def pause_schedule(schedule_id: int, request: Request):
    try:
        return schedule_service.set_status(services(request).db, schedule_id, "paused")
    except schedule_service.ScheduleError as exc:
        _schedule_error(exc)


@api.post("/schedules/{schedule_id}/run-now")
def run_schedule_now(schedule_id: int, request: Request):
    svc = services(request)
    schedule = schedule_service.get_schedule(svc.db, schedule_id)
    if schedule is None:
        raise HTTPException(404, "Schedule not found")
    if svc.db.one("SELECT id FROM tasks WHERE schedule_id = ? AND status IN ('queued', 'running')", (schedule_id,)):
        raise HTTPException(409, "A run of this schedule is already in progress")
    provider = _check_provider(svc)
    try:
        task_id = create_task(svc.db, kind="scheduled", title=schedule["name"], prompt=schedule_service.scheduled_prompt(schedule),
                              schedule_id=schedule_id, scheduled_for=f"manual-{now_iso()}")  # fmt: skip
    except sqlite3.IntegrityError:
        raise HTTPException(409, "A run of this schedule was just started") from None
    svc.db.execute("UPDATE tasks SET provider = ? WHERE id = ?", (provider, task_id))
    if svc.worker:
        svc.worker.wake()
    return public_task(get_task(svc.db, task_id))


@api.delete("/schedules/{schedule_id}")
def delete_schedule(schedule_id: int, request: Request):
    if not schedule_service.delete_schedule(services(request).db, schedule_id):
        raise HTTPException(404, "Schedule not found")
    return {"ok": True}


# ---------------------------------------------------------------------------------------------
# profile and memories


class MemoryBody(BaseModel):
    content: str = Field(min_length=1, max_length=600)


@api.get("/profile")
def get_profile(request: Request):
    return profile_service.get_profile(services(request).db)


@api.put("/profile")
def put_profile(body: dict[str, Any], request: Request):
    return profile_service.save_profile(services(request).db, body)


@api.get("/memories")
def memories(request: Request):
    return {"items": profile_service.list_memories(services(request).db)}


@api.post("/memories")
def add_memory(body: MemoryBody, request: Request):
    try:
        return profile_service.add_memory(services(request).db, body.content, source="user")
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None


@api.put("/memories/{memory_id}")
def edit_memory(memory_id: int, body: MemoryBody, request: Request):
    try:
        row = profile_service.update_memory(services(request).db, memory_id, body.content)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    if row is None:
        raise HTTPException(404, "Not found")
    return row


@api.delete("/memories/{memory_id}")
def remove_memory(memory_id: int, request: Request):
    if not profile_service.delete_memory(services(request).db, memory_id):
        raise HTTPException(404, "Not found")
    return {"ok": True}


# ---------------------------------------------------------------------------------------------
# settings and integrations


class SettingsBody(BaseModel):
    timezone: str | None = None
    model_provider: str | None = None
    task_max_cost_usd: float | None = Field(None, gt=0, le=100)
    daily_max_cost_usd: float | None = Field(None, gt=0, le=1000)
    anthropic_effort: str | None = None


@api.get("/settings")
def get_settings_view(request: Request):
    svc = services(request)
    s = svc.settings
    return {
        "timezone": svc.runtime("timezone"),
        "model_provider": svc.runtime("model_provider"),
        "anthropic_effort": svc.runtime("anthropic_effort"),
        "task_max_cost_usd": svc.runtime("task_max_cost_usd"),
        "daily_max_cost_usd": svc.runtime("daily_max_cost_usd"),
        "providers": configured_providers(s),
        "local_only": s.local_only,
        "limits": {
            "max_steps": s.task_max_steps, "max_tool_calls": s.task_max_tool_calls, "max_searches": s.task_max_searches,
            "max_fetches": s.task_max_fetches, "max_minutes": round(s.task_max_seconds / 60), "max_tokens": s.task_max_tokens,
            "max_concurrent_tasks": s.max_concurrent_tasks,
        },  # fmt: skip
        "search_provider": s.search_provider,
        "browser_enabled": s.browser_enabled,
        "outbound_proxy_set": bool(s.outbound_proxy),
        "data_dir": str(s.data_dir),
        "workspace_dir": str(s.workspace_dir),
        "public_url": s.public_url,
        "spend": spend_summary(svc.db, svc.runtime("timezone")),
    }


@api.put("/settings")
async def put_settings(body: SettingsBody, request: Request):
    svc = services(request)
    changes = body.model_dump(exclude_none=True)
    if "timezone" in changes and not valid_timezone(changes["timezone"]):
        raise HTTPException(400, f"Unknown time zone '{changes['timezone']}'")
    if "model_provider" in changes:
        allowed = {p["name"] for p in configured_providers(svc.settings) if p["allowed"]}
        if changes["model_provider"] not in allowed:
            raise HTTPException(400, "That provider is not allowed (LOCAL_ONLY) or unknown")
    if "anthropic_effort" in changes and changes["anthropic_effort"] not in ("low", "medium", "high", "xhigh", "max"):
        raise HTTPException(400, "Effort must be low, medium, high, xhigh, or max")
    for key, value in changes.items():
        profile_service.set_runtime_setting(svc.db, key, value)
    svc.db.audit("user", "settings.changed", changes)
    if "anthropic_effort" in changes or "model_provider" in changes:
        await svc.reset_providers()
    return get_settings_view(request)


@api.get("/integrations")
def integrations(request: Request):
    svc = services(request)
    s = svc.settings
    name = svc.runtime("model_provider")
    model: dict = {"provider": name, "ok": True, "error": None}
    try:
        provider = svc.get_provider(name)
        model.update(provider.describe())
    except ModelConfigError as exc:
        model.update({"ok": False, "error": str(exc)})
    search_ok = getattr(svc.search, "name", "none") != "none"
    return {
        "model": model,
        "search": {"provider": s.search_provider, "name": getattr(svc.search, "name", "none"), "ok": search_ok,
                   "error": None if search_ok else getattr(svc.search, "reason", "Search not configured")},  # fmt: skip
        "browser": {"enabled": s.browser_enabled, **svc.browser.status()},
        "gmail": svc.gmail.status(),
        "outbound_proxy": bool(s.outbound_proxy),
        "local_only": s.local_only,
        "spend": spend_summary(svc.db, svc.runtime("timezone")),
    }


@api.post("/integrations/test-model")
async def test_model(request: Request):
    svc = services(request)
    try:
        provider = svc.get_provider(svc.runtime("model_provider"))
        return {"ok": True, "message": await provider.check()}
    except (ModelConfigError, ModelError) as exc:
        return {"ok": False, "message": str(exc)}


@api.post("/integrations/test-search")
async def test_search(request: Request):
    svc = services(request)
    try:
        results = await svc.search.search("Home Agent connectivity test", 3, None)
        return {"ok": True, "message": f"{svc.search.name} returned {len(results)} results",
                "results": [{"title": r.title, "url": r.url} for r in results]}  # fmt: skip
    except SearchError as exc:
        return {"ok": False, "message": str(exc)}


@api.post("/integrations/test-browser")
async def test_browser(request: Request):
    svc = services(request)
    if not svc.settings.browser_enabled:
        return {"ok": False, "message": "Browser rendering is disabled (BROWSER_ENABLED=false)"}
    try:
        result = await svc.browser.render("https://example.com/")
        return {"ok": True, "message": f"Rendered example.com (HTTP {result.status_code}, title '{result.title}')"}
    except Exception as exc:
        return {"ok": False, "message": f"{type(exc).__name__}: {str(exc).splitlines()[0][:300] if str(exc) else ''}"}


class GmailComplete(BaseModel):
    redirect_url: str = Field(max_length=4000)


@api.post("/integrations/gmail/start")
def gmail_start(request: Request):
    svc = services(request)
    try:
        return {"auth_url": svc.gmail.start_authorization(), "redirect_uri": svc.settings.effective_gmail_redirect_uri}
    except GmailError as exc:
        raise HTTPException(400, str(exc)) from None


@api.post("/integrations/gmail/complete")
async def gmail_complete(body: GmailComplete, request: Request):
    svc = services(request)
    code, state, error = svc.gmail.parse_redirect(body.redirect_url)
    try:
        account = await svc.gmail.complete_authorization(code, state, error)
    except GmailError as exc:
        raise HTTPException(400, str(exc)) from None
    return {"ok": True, "account": account}


@api.post("/integrations/gmail/disconnect")
async def gmail_disconnect(request: Request):
    await services(request).gmail.disconnect()
    return {"ok": True}


@public.get("/integrations/gmail/callback", response_class=HTMLResponse)
async def gmail_callback(request: Request, code: str | None = None, state: str | None = None, error: str | None = None):
    """OAuth redirect target. Protected by the single-use state value bound to the flow."""
    svc = services(request)
    try:
        account = await svc.gmail.complete_authorization(code, state, error)
        message = f"Gmail connected ({account}). You can close this tab and return to Home Agent."
    except GmailError as exc:
        message = f"Gmail connection failed: {exc}"
    import html

    return HTMLResponse(
        "<!doctype html><meta charset='utf-8'><meta name='viewport' content='width=device-width'>"
        f"<title>Home Agent</title><body style='font-family:system-ui;padding:2rem'><p>{html.escape(message)}</p>"
        "<p><a href='/#/settings'>Back to Home Agent</a></p></body>"
    )


# ---------------------------------------------------------------------------------------------
# workspace files


@api.get("/files")
def files(request: Request):
    return {"items": services(request).workspace.list()}


@api.post("/files")
async def upload_file(request: Request, file: UploadFile = File(...)):
    svc = services(request)
    data = await file.read(15_000_001)
    try:
        return svc.workspace.save_upload(file.filename or "upload", data)
    except WorkspaceError as exc:
        raise HTTPException(400, str(exc)) from None


@api.get("/files/content")
def file_content(request: Request, path: str, start: int = 0):
    try:
        text, total = services(request).workspace.read_text(path, start, 50000)
    except WorkspaceError as exc:
        raise HTTPException(400, str(exc)) from None
    return {"path": path, "text": text, "total_chars": total}


@api.delete("/files")
def delete_file(request: Request, path: str):
    try:
        deleted = services(request).workspace.delete(path)
    except WorkspaceError as exc:
        raise HTTPException(400, str(exc)) from None
    if not deleted:
        raise HTTPException(404, "File not found")
    return {"ok": True}


__all__ = ["public", "api", "loads"]
