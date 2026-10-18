"""Task records, progress events, notifications, and the spending ledger."""

from __future__ import annotations

from ..db import Database, dumps, loads, now_iso
from ..util import local_now, truncate

ACTIVE = ("queued", "running")
FINAL = ("succeeded", "failed", "cancelled")


# -- tasks ---------------------------------------------------------------------------

def create_task(db: Database, *, kind: str, title: str, prompt: str, conversation_id: int | None = None,
                user_message_id: int | None = None, schedule_id: int | None = None,
                scheduled_for: str | None = None) -> int:  # fmt: skip
    now = now_iso()
    return db.insert(
        "tasks",
        {"kind": kind, "title": truncate(title.strip() or "Task", 200), "prompt": prompt,
         "conversation_id": conversation_id, "user_message_id": user_message_id, "schedule_id": schedule_id,
         "scheduled_for": scheduled_for, "status": "queued", "created_at": now, "updated_at": now},  # fmt: skip
    )


def get_task(db: Database, task_id: int) -> dict | None:
    return db.one("SELECT * FROM tasks WHERE id = ?", (task_id,))


def public_task(row: dict) -> dict:
    out = {k: v for k, v in row.items() if k not in ("checkpoint", "lease_owner")}
    out["cancel_requested"] = bool(row.get("cancel_requested"))
    return out


def list_tasks(db: Database, *, status: str | None = None, limit: int = 100, conversation_id: int | None = None) -> list[dict]:
    clauses, params = [], []
    if status == "active":
        clauses.append("status IN ('queued', 'running')")
    elif status:
        clauses.append("status = ?")
        params.append(status)
    if conversation_id is not None:
        clauses.append("conversation_id = ?")
        params.append(conversation_id)
    sql = "SELECT * FROM tasks"
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    return [public_task(r) for r in db.all(sql, tuple(params))]


def add_event(db: Database, task_id: int, type_: str, summary: str, data: dict | None = None) -> int:
    return db.insert(
        "task_events",
        {"task_id": task_id, "ts": now_iso(), "type": type_, "summary": truncate(summary, 1000),
         "data": dumps(data) if data else None},  # fmt: skip
    )


def list_events(db: Database, task_id: int, after: int = 0, limit: int = 500) -> list[dict]:
    rows = db.all(
        "SELECT id, ts, type, summary, data FROM task_events WHERE task_id = ? AND id > ? ORDER BY id LIMIT ?",
        (task_id, after, limit),
    )
    for row in rows:
        row["data"] = loads(row["data"])
    return rows


def request_cancel(db: Database, task_id: int) -> dict | None:
    task = get_task(db, task_id)
    if task is None:
        return None
    if task["status"] == "queued":
        db.execute(
            "UPDATE tasks SET status = 'cancelled', cancel_requested = 1, status_detail = 'Cancelled before it started', "
            "finished_at = ?, updated_at = ? WHERE id = ? AND status = 'queued'",
            (now_iso(), now_iso(), task_id),
        )
        add_event(db, task_id, "status", "Cancelled before it started")
    elif task["status"] == "running":
        db.execute("UPDATE tasks SET cancel_requested = 1, updated_at = ? WHERE id = ?", (now_iso(), task_id))
        add_event(db, task_id, "status", "Cancellation requested")
    return get_task(db, task_id)


# -- notifications ----------------------------------------------------------------------

def notify(db: Database, kind: str, title: str, body: str = "", link: str | None = None, task_id: int | None = None) -> int:
    return db.insert(
        "notifications",
        {"kind": kind, "title": truncate(title, 200), "body": truncate(body, 2000), "link": link, "task_id": task_id,
         "created_at": now_iso()},  # fmt: skip
    )


def list_notifications(db: Database, unread_only: bool = False, limit: int = 50) -> list[dict]:
    sql = "SELECT * FROM notifications"
    if unread_only:
        sql += " WHERE read_at IS NULL"
    sql += " ORDER BY id DESC LIMIT ?"
    return db.all(sql, (limit,))


def unread_count(db: Database) -> int:
    return int(db.scalar("SELECT COUNT(*) FROM notifications WHERE read_at IS NULL") or 0)


def mark_read(db: Database, ids: list[int] | None = None) -> int:
    if ids is None:
        return db.execute("UPDATE notifications SET read_at = ? WHERE read_at IS NULL", (now_iso(),)).rowcount
    count = 0
    for notification_id in ids:
        count += db.execute(
            "UPDATE notifications SET read_at = ? WHERE id = ? AND read_at IS NULL", (now_iso(), int(notification_id))
        ).rowcount
    return count


# -- spending ------------------------------------------------------------------------------

def record_spend(db: Database, task_id: int, tz: str, provider: str, usage) -> None:
    db.insert(
        "spend",
        {"task_id": task_id, "ts": now_iso(), "day": local_now(tz).date().isoformat(), "provider": provider,
         "model": usage.model, "input_tokens": usage.input_tokens, "output_tokens": usage.output_tokens,
         "cache_read_tokens": usage.cache_read_tokens, "cache_write_tokens": usage.cache_write_tokens,
         "cost_usd": usage.cost_usd},  # fmt: skip
    )


def spend_today(db: Database, tz: str) -> float:
    return float(db.scalar("SELECT COALESCE(SUM(cost_usd), 0) FROM spend WHERE day = ?", (local_now(tz).date().isoformat(),)) or 0)


def spend_summary(db: Database, tz: str) -> dict:
    today = local_now(tz).date().isoformat()
    month = today[:7]
    return {
        "today_usd": spend_today(db, tz),
        "month_usd": float(db.scalar("SELECT COALESCE(SUM(cost_usd), 0) FROM spend WHERE day LIKE ?", (f"{month}%",)) or 0),
        "today_tokens": int(db.scalar(
            "SELECT COALESCE(SUM(input_tokens + output_tokens + cache_read_tokens + cache_write_tokens), 0) FROM spend WHERE day = ?",
            (today,)) or 0),  # fmt: skip
    }
