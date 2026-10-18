"""Email drafts and the approval -> send state machine.

Rules enforced here (not in the model prompt):

* The agent can only create drafts. Sending is a user action through the web API.
* Approval binds to a hash of the exact recipients, subject, body and attachment
  contents. Any edit changes the hash, which clears the approval.
* Sending atomically moves an approved draft to `sending`; a second click, a retried
  request or a restart cannot send it again. Interrupted sends are never retried
  automatically; on restart they are checked against Gmail (a sent Gmail draft no
  longer exists) and marked sent / not sent / unknown.
"""

from __future__ import annotations

import base64
import hashlib
import json
import mimetypes
import uuid
from email.message import EmailMessage
from email.utils import formatdate, getaddresses

from ..db import Database, dumps, in_seconds, loads, now_iso, parse_iso, utcnow
from .gmail import GmailError, GmailService
from .workspace import Workspace, WorkspaceError

APPROVAL_SECONDS = 30 * 60
EDITABLE_STATES = {"draft", "approved", "failed"}
LOCKED_STATES = {"sending", "sent", "send_unknown"}


class DraftError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def parse_addresses(values) -> list[str]:
    if isinstance(values, str):
        values = [values]
    out = []
    for name, addr in getaddresses([str(v) for v in values or [] if str(v).strip()]):
        addr = addr.strip()
        if not addr:
            continue
        if "@" not in addr or " " in addr or addr.count("@") != 1 or "." not in addr.split("@")[1]:
            raise DraftError(f"'{addr}' is not a valid email address")
        out.append(f"{name} <{addr}>" if name else addr)
    return out


def _attachment_meta(workspace: Workspace, attachments: list[dict]) -> list[dict]:
    """Read each attachment once; the same bytes are hashed for approval and put in the email."""
    meta = []
    for item in attachments:
        try:
            data = workspace.read_bytes(item["path"])
        except (WorkspaceError, KeyError) as exc:
            raise DraftError(f"Attachment problem: {exc}") from None
        meta.append({"path": item["path"], "size": len(data), "sha256": hashlib.sha256(data).hexdigest(),
                     "mime": mimetypes.guess_type(item["path"])[0] or "application/octet-stream", "data": data})  # fmt: skip
    return meta


def content_hash(draft: dict, attachment_meta: list[dict]) -> str:
    canonical = {
        "to": loads(draft["to_addrs"], []),
        "cc": loads(draft["cc_addrs"], []),
        "bcc": loads(draft["bcc_addrs"], []),
        "subject": draft["subject"],
        "body": draft["body"],
        "attachments": [{"path": m["path"], "sha256": m["sha256"], "size": m["size"]} for m in attachment_meta],
    }
    return hashlib.sha256(json.dumps(canonical, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def present(draft: dict, workspace: Workspace) -> dict:
    out = dict(draft)
    for key in ("to_addrs", "cc_addrs", "bcc_addrs", "attachments", "warnings"):
        out[key] = loads(draft[key], [])
    try:
        out["content_hash"] = content_hash(draft, _attachment_meta(workspace, out["attachments"]))
        out["attachment_error"] = None
    except DraftError as exc:
        out["content_hash"] = None
        out["attachment_error"] = str(exc)
    expires = parse_iso(draft.get("approval_expires_at"))
    out["approval_valid"] = (
        draft["status"] == "approved"
        and draft.get("approved_hash") == out["content_hash"]
        and expires is not None
        and expires > utcnow()
    )
    return out


def get_draft(db: Database, draft_id: int) -> dict | None:
    return db.one("SELECT * FROM drafts WHERE id = ?", (draft_id,))


def list_drafts(db: Database, workspace: Workspace) -> list[dict]:
    return [present(d, workspace) for d in db.all("SELECT * FROM drafts ORDER BY updated_at DESC, id DESC")]


def create_draft(db: Database, *, to=(), cc=(), bcc=(), subject: str = "", body: str = "", attachments=(),
                 opportunity_id: int | None = None, task_id: int | None = None, call_id: str | None = None,
                 warnings=()) -> dict:  # fmt: skip
    if task_id is not None and call_id is not None:
        existing = db.one("SELECT * FROM drafts WHERE source_task_id = ? AND source_call_id = ?", (task_id, call_id))
        if existing:
            return existing
    if len(body) > 50_000 or len(subject) > 500:
        raise DraftError("Subject or body is too long")
    now = now_iso()
    draft_id = db.insert(
        "drafts",
        {
            "to_addrs": dumps(parse_addresses(to)),
            "cc_addrs": dumps(parse_addresses(cc)),
            "bcc_addrs": dumps(parse_addresses(bcc)),
            "subject": subject.strip(),
            "body": body,
            "attachments": dumps([{"path": a["path"]} for a in attachments]),
            "warnings": dumps(list(warnings)),
            "opportunity_id": opportunity_id,
            "source_task_id": task_id,
            "source_call_id": call_id,
            "status": "draft",
            "created_at": now,
            "updated_at": now,
        },
    )
    return get_draft(db, draft_id)  # type: ignore[return-value]


def update_draft(db: Database, workspace: Workspace, draft_id: int, fields: dict, actor: str = "user") -> dict:
    with db.tx():
        draft = get_draft(db, draft_id)
        if draft is None:
            raise DraftError("Draft not found", 404)
        if draft["status"] in LOCKED_STATES:
            raise DraftError(f"This draft is {draft['status'].replace('_', ' ')} and can no longer be edited", 409)
        values: dict = {}
        for key, column in (("to", "to_addrs"), ("cc", "cc_addrs"), ("bcc", "bcc_addrs")):
            if key in fields:
                values[column] = dumps(parse_addresses(fields[key]))
        if "subject" in fields:
            values["subject"] = str(fields["subject"]).strip()[:500]
        if "body" in fields:
            if len(str(fields["body"])) > 50_000:
                raise DraftError("Body is too long")
            values["body"] = str(fields["body"])
        if "attachments" in fields:
            paths = [{"path": str(a["path"] if isinstance(a, dict) else a)} for a in fields["attachments"] or []]
            _attachment_meta(workspace, paths)  # validate now
            values["attachments"] = dumps(paths)
        if not values:
            return draft
        before = present(draft, workspace)["content_hash"]
        candidate = {**draft, **values}
        try:
            after = content_hash(candidate, _attachment_meta(workspace, loads(candidate["attachments"], [])))
        except DraftError:
            after = None
        if after != before:
            # Any change to what would be sent invalidates a previous approval.
            values.update({"status": "draft", "approved_hash": None, "approved_at": None, "approval_expires_at": None})
            if draft["status"] == "approved":
                db.audit(actor, "draft.approval_invalidated", {"draft_id": draft_id})
        values["updated_at"] = now_iso()
        db.update("drafts", draft_id, values)
        return get_draft(db, draft_id)  # type: ignore[return-value]


def delete_draft(db: Database, draft_id: int) -> None:
    draft = get_draft(db, draft_id)
    if draft is None:
        raise DraftError("Draft not found", 404)
    if draft["status"] == "sending":
        raise DraftError("This draft is being sent right now", 409)
    db.execute("DELETE FROM drafts WHERE id = ?", (draft_id,))


def approve(db: Database, workspace: Workspace, draft_id: int, reviewed_hash: str, *, send_enabled: bool) -> dict:
    if not send_enabled:
        raise DraftError("Sending is disabled. Set GMAIL_ALLOW_SEND=true in .env to enable approved sending.", 403)
    with db.tx():
        draft = get_draft(db, draft_id)
        if draft is None:
            raise DraftError("Draft not found", 404)
        if draft["status"] not in EDITABLE_STATES:
            raise DraftError(f"A {draft['status'].replace('_', ' ')} draft cannot be approved", 409)
        current = content_hash(draft, _attachment_meta(workspace, loads(draft["attachments"], [])))
        if not reviewed_hash or reviewed_hash != current:
            raise DraftError("The draft changed after you reviewed it. Review the latest version and approve again.", 409)
        if not loads(draft["to_addrs"], []):
            raise DraftError("Add at least one recipient before approving")
        if not draft["subject"].strip():
            raise DraftError("Add a subject before approving")
        db.execute(
            "UPDATE drafts SET status = 'approved', approved_hash = ?, approved_at = ?, approval_expires_at = ?, "
            "last_error = NULL, updated_at = ? WHERE id = ?",
            (current, now_iso(), in_seconds(APPROVAL_SECONDS), now_iso(), draft_id),
        )
        db.audit("user", "draft.approved", {"draft_id": draft_id, "hash": current})
        return get_draft(db, draft_id)  # type: ignore[return-value]


def revoke_approval(db: Database, draft_id: int) -> dict:
    db.execute(
        "UPDATE drafts SET status = 'draft', approved_hash = NULL, approved_at = NULL, approval_expires_at = NULL, "
        "updated_at = ? WHERE id = ? AND status = 'approved'",
        (now_iso(), draft_id),
    )
    draft = get_draft(db, draft_id)
    if draft is None:
        raise DraftError("Draft not found", 404)
    return draft


def build_mime(draft: dict, workspace: Workspace, message_id: str, attachments: list[dict] | None = None) -> str:
    message = EmailMessage()
    message["To"] = ", ".join(loads(draft["to_addrs"], []))
    cc = loads(draft["cc_addrs"], [])
    bcc = loads(draft["bcc_addrs"], [])
    if cc:
        message["Cc"] = ", ".join(cc)
    if bcc:
        message["Bcc"] = ", ".join(bcc)
    message["Subject"] = draft["subject"]
    message["Date"] = formatdate(localtime=True)
    message["Message-ID"] = message_id
    message.set_content(draft["body"])
    if attachments is None:
        attachments = _attachment_meta(workspace, loads(draft["attachments"], []))
    for meta in attachments:
        maintype, _, subtype = meta["mime"].partition("/")
        message.add_attachment(
            meta["data"], maintype=maintype, subtype=subtype or "octet-stream", filename=meta["path"].rsplit("/", 1)[-1],
        )  # fmt: skip
    return base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")


async def save_to_gmail(db: Database, workspace: Workspace, gmail: GmailService, draft_id: int) -> dict:
    """Create or update the matching Gmail draft (no sending)."""
    draft = get_draft(db, draft_id)
    if draft is None:
        raise DraftError("Draft not found", 404)
    if draft["status"] in LOCKED_STATES:
        raise DraftError("This draft was already sent or is being sent", 409)
    if not gmail.connected:
        raise DraftError("Connect Gmail in Settings first", 409)
    current = content_hash(draft, _attachment_meta(workspace, loads(draft["attachments"], [])))
    raw = build_mime(draft, workspace, f"<draft-{draft_id}-{uuid.uuid4().hex}@home-agent.local>")
    try:
        if draft["gmail_draft_id"]:
            try:
                gmail_id = await gmail.update_draft(draft["gmail_draft_id"], raw)
            except GmailError as exc:
                if exc.status != 404:
                    raise
                gmail_id = await gmail.create_draft(raw)  # deleted in Gmail; make a new one
        else:
            gmail_id = await gmail.create_draft(raw)
    except GmailError as exc:
        raise DraftError(str(exc), 502) from None
    db.execute(
        "UPDATE drafts SET gmail_draft_id = ?, gmail_synced_hash = ?, updated_at = ? WHERE id = ?",
        (gmail_id, current, now_iso(), draft_id),
    )
    return get_draft(db, draft_id)  # type: ignore[return-value]


async def send(db: Database, workspace: Workspace, gmail: GmailService, draft_id: int, reviewed_hash: str, *,
               send_enabled: bool) -> dict:  # fmt: skip
    if not send_enabled:
        raise DraftError("Sending is disabled (GMAIL_ALLOW_SEND=false).", 403)
    if not gmail.connected:
        raise DraftError("Connect Gmail in Settings first", 409)
    attempt = uuid.uuid4().hex
    with db.tx():
        draft = get_draft(db, draft_id)
        if draft is None:
            raise DraftError("Draft not found", 404)
        if draft["status"] != "approved":
            raise DraftError(f"Only an approved draft can be sent (this one is {draft['status'].replace('_', ' ')})", 409)
        attachments = _attachment_meta(workspace, loads(draft["attachments"], []))
        current = content_hash(draft, attachments)
        if draft["approved_hash"] != current or reviewed_hash != current:
            raise DraftError("The draft differs from what was approved. Review and approve it again.", 409)
        expires = parse_iso(draft["approval_expires_at"])
        if expires is None or expires < utcnow():
            raise DraftError("The approval expired. Review and approve the draft again.", 409)
        claimed = db.execute(
            "UPDATE drafts SET status = 'sending', send_attempt_id = ?, send_started_at = ?, updated_at = ? "
            "WHERE id = ? AND status = 'approved' AND approved_hash = ?",
            (attempt, now_iso(), now_iso(), draft_id, current),
        ).rowcount
        if claimed != 1:
            raise DraftError("This draft is already being sent", 409)
        db.audit("user", "draft.send_started", {"draft_id": draft_id, "attempt": attempt, "hash": current})

    raw = build_mime(draft, workspace, f"<{attempt}@home-agent.local>", attachments)
    gmail_id = draft["gmail_draft_id"]
    try:
        if gmail_id:
            exists = await gmail.draft_exists(gmail_id)
            if exists is False:
                gmail_id = None
        if not gmail_id:
            gmail_id = await gmail.create_draft(raw)
            db.execute("UPDATE drafts SET gmail_draft_id = ? WHERE id = ?", (gmail_id, draft_id))
        message_id = await gmail.send_draft(gmail_id, raw)
    except GmailError as exc:
        if exc.definitely_not_sent:
            db.execute(
                "UPDATE drafts SET status = 'failed', approved_hash = NULL, approval_expires_at = NULL, last_error = ?, "
                "updated_at = ? WHERE id = ?",
                (f"Not sent: {exc}", now_iso(), draft_id),
            )
            db.audit("system", "draft.send_failed", {"draft_id": draft_id, "error": str(exc)})
            raise DraftError(f"Not sent: {exc}", 502) from None
        db.execute(
            "UPDATE drafts SET status = 'send_unknown', last_error = ?, updated_at = ? WHERE id = ?",
            (f"Outcome unknown: {exc}", now_iso(), draft_id),
        )
        await verify_unknown(db, gmail, draft_id)
        refreshed = get_draft(db, draft_id)
        raise DraftError(
            "Gmail did not confirm the send. " + (refreshed or {}).get("last_error", ""), 502
        ) from None
    db.execute(
        "UPDATE drafts SET status = 'sent', gmail_message_id = ?, sent_at = ?, last_error = NULL, updated_at = ? "
        "WHERE id = ?",
        (message_id, now_iso(), now_iso(), draft_id),
    )
    db.audit("user", "draft.sent", {"draft_id": draft_id, "gmail_message_id": message_id})
    return get_draft(db, draft_id)  # type: ignore[return-value]


async def verify_unknown(db: Database, gmail: GmailService, draft_id: int) -> None:
    """Resolve an interrupted send without ever re-sending."""
    draft = get_draft(db, draft_id)
    if draft is None or draft["status"] not in ("sending", "send_unknown"):
        return
    gmail_id = draft["gmail_draft_id"]
    if not gmail_id:
        db.execute(
            "UPDATE drafts SET status = 'failed', approved_hash = NULL, last_error = ?, updated_at = ? WHERE id = ?",
            ("Not sent: the send was interrupted before a Gmail draft existed. Approve again to retry.", now_iso(), draft_id),
        )
        return
    exists = await gmail.draft_exists(gmail_id)
    if exists is True:
        db.execute(
            "UPDATE drafts SET status = 'failed', approved_hash = NULL, last_error = ?, updated_at = ? WHERE id = ?",
            ("Not sent: the Gmail draft still exists, so the send did not happen. Approve again to retry.", now_iso(),
             draft_id),  # fmt: skip
        )
    elif exists is False:
        db.execute(
            "UPDATE drafts SET status = 'sent', sent_at = COALESCE(sent_at, ?), last_error = ?, updated_at = ? WHERE id = ?",
            (now_iso(), "Most likely sent: the Gmail draft is gone, which is what sending does. Check your Sent folder.",
             now_iso(), draft_id),  # fmt: skip
        )
    else:
        db.execute(
            "UPDATE drafts SET status = 'send_unknown', last_error = ?, updated_at = ? WHERE id = ?",
            ("Unknown: could not check Gmail. Look in your Gmail Sent folder; Home Agent will not resend automatically.",
             now_iso(), draft_id),  # fmt: skip
        )
    db.audit("system", "draft.send_verified", {"draft_id": draft_id, "draft_exists": exists})


async def recover_interrupted_sends(db: Database, gmail: GmailService) -> int:
    rows = db.all("SELECT id FROM drafts WHERE status = 'sending'")
    for row in rows:
        db.execute("UPDATE drafts SET status = 'send_unknown' WHERE id = ? AND status = 'sending'", (row["id"],))
        if gmail.connected:
            await verify_unknown(db, gmail, row["id"])
    return len(rows)


def mark_resolved(db: Database, draft_id: int, outcome: str) -> dict:
    """Let the user settle a send_unknown draft after checking Gmail themselves."""
    if outcome not in ("sent", "not_sent"):
        raise DraftError("outcome must be 'sent' or 'not_sent'")
    draft = get_draft(db, draft_id)
    if draft is None:
        raise DraftError("Draft not found", 404)
    if draft["status"] != "send_unknown":
        raise DraftError("Only drafts with an unknown send outcome can be resolved", 409)
    if outcome == "sent":
        db.execute("UPDATE drafts SET status = 'sent', sent_at = COALESCE(sent_at, ?), updated_at = ? WHERE id = ?",
                   (now_iso(), now_iso(), draft_id))  # fmt: skip
    else:
        db.execute("UPDATE drafts SET status = 'failed', approved_hash = NULL, updated_at = ? WHERE id = ?",
                   (now_iso(), draft_id))  # fmt: skip
    db.audit("user", "draft.send_resolved", {"draft_id": draft_id, "outcome": outcome})
    return get_draft(db, draft_id)  # type: ignore[return-value]
