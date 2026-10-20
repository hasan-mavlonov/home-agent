"""Recurring research schedules.

Recurrence is daily / weekly / monthly at a local time in the schedule's time zone.
The scheduler creates at most one task per (schedule, slot) - enforced by a UNIQUE
constraint - and coalesces missed slots after downtime into a single catch-up run.
Schedules the agent proposes start as `pending_confirmation` and never run until the
user activates them in the app.
"""

from __future__ import annotations

import calendar
import re
from datetime import UTC, datetime, timedelta

from ..db import Database, dumps, iso, loads, now_iso, parse_iso, utcnow
from ..util import get_zone, valid_timezone

FREQUENCIES = ("daily", "weekly", "monthly")
WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


class ScheduleError(Exception):
    pass


def validate_recurrence(rec: dict) -> dict:
    freq = rec.get("freq")
    if freq not in FREQUENCIES:
        raise ScheduleError("Frequency must be daily, weekly, or monthly")
    time_text = str(rec.get("time") or "09:00")
    if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", time_text):
        raise ScheduleError("Time must be HH:MM (24-hour)")
    out = {"freq": freq, "time": time_text}
    if freq == "weekly":
        weekday = int(rec.get("weekday", 0))
        if not 0 <= weekday <= 6:
            raise ScheduleError("Weekday must be 0 (Monday) to 6 (Sunday)")
        out["weekday"] = weekday
    if freq == "monthly":
        day = int(rec.get("day", 1))
        if not 1 <= day <= 31:
            raise ScheduleError("Day of month must be 1-31")
        out["day"] = day
    return out


def describe_recurrence(rec: dict) -> str:
    if rec["freq"] == "daily":
        return f"Every day at {rec['time']}"
    if rec["freq"] == "weekly":
        return f"Every {WEEKDAYS[rec['weekday']]} at {rec['time']}"
    return f"Monthly on day {rec['day']} at {rec['time']}"


def next_run_after(rec: dict, tz_name: str, after: datetime) -> datetime:
    """First occurrence strictly after `after` (UTC), computed in local time."""
    tz = get_zone(tz_name)
    local_after = after.astimezone(tz)
    hour, minute = (int(x) for x in rec["time"].split(":"))
    candidate_day = local_after.date()
    for _ in range(400):
        if rec["freq"] == "daily":
            ok = True
        elif rec["freq"] == "weekly":
            ok = candidate_day.weekday() == rec["weekday"]
        else:
            last = calendar.monthrange(candidate_day.year, candidate_day.month)[1]
            ok = candidate_day.day == min(rec["day"], last)
        if ok:
            local = datetime(candidate_day.year, candidate_day.month, candidate_day.day, hour, minute, tzinfo=tz)
            if local > after:
                return local.astimezone(UTC)
        candidate_day = candidate_day + timedelta(days=1)
    raise ScheduleError("Could not compute the next run time")


def _present(row: dict) -> dict:
    out = dict(row)
    out["recurrence"] = loads(row["recurrence"], {})
    out["description"] = describe_recurrence(out["recurrence"]) if out["recurrence"] else ""
    return out


def get_schedule(db: Database, schedule_id: int) -> dict | None:
    row = db.one("SELECT * FROM schedules WHERE id = ?", (schedule_id,))
    return _present(row) if row else None


def list_schedules(db: Database) -> list[dict]:
    return [_present(r) for r in db.all("SELECT * FROM schedules ORDER BY id")]


def create_schedule(db: Database, *, name: str, prompt: str, recurrence: dict, timezone: str, created_by: str,
                    status: str | None = None, task_id: int | None = None, call_id: str | None = None) -> dict:  # fmt: skip
    name = (name or "").strip()[:200]
    prompt = (prompt or "").strip()
    if not name or not prompt:
        raise ScheduleError("A schedule needs a name and a prompt")
    if len(prompt) > 4000:
        raise ScheduleError("The prompt is too long (max 4000 characters)")
    if not valid_timezone(timezone):
        raise ScheduleError(f"Unknown time zone '{timezone}'")
    rec = validate_recurrence(recurrence)
    if task_id is not None and call_id is not None:
        existing = db.one("SELECT * FROM schedules WHERE source_task_id = ? AND source_call_id = ?", (task_id, call_id))
        if existing:
            return _present(existing)
    status = status or ("active" if created_by == "user" else "pending_confirmation")
    now = now_iso()
    schedule_id = db.insert(
        "schedules",
        {
            "name": name,
            "prompt": prompt,
            "recurrence": dumps(rec),
            "timezone": timezone,
            "status": status,
            "next_run_at": iso(next_run_after(rec, timezone, utcnow())) if status == "active" else None,
            "created_by": created_by,
            "source_task_id": task_id,
            "source_call_id": call_id,
            "created_at": now,
            "updated_at": now,
        },
    )
    return get_schedule(db, schedule_id)  # type: ignore[return-value]


def update_schedule(db: Database, schedule_id: int, fields: dict) -> dict:
    current = get_schedule(db, schedule_id)
    if current is None:
        raise ScheduleError("Schedule not found")
    values: dict = {}
    if "name" in fields:
        values["name"] = str(fields["name"]).strip()[:200] or current["name"]
    if "prompt" in fields:
        prompt = str(fields["prompt"]).strip()
        if not prompt or len(prompt) > 4000:
            raise ScheduleError("The prompt must be 1-4000 characters")
        values["prompt"] = prompt
    rec = current["recurrence"]
    tz = current["timezone"]
    if "recurrence" in fields:
        rec = validate_recurrence(fields["recurrence"])
        values["recurrence"] = dumps(rec)
    if "timezone" in fields:
        if not valid_timezone(fields["timezone"]):
            raise ScheduleError(f"Unknown time zone '{fields['timezone']}'")
        tz = fields["timezone"]
        values["timezone"] = tz
    if ("recurrence" in fields or "timezone" in fields) and current["status"] == "active":
        values["next_run_at"] = iso(next_run_after(rec, tz, utcnow()))
    values["updated_at"] = now_iso()
    db.update("schedules", schedule_id, values)
    return get_schedule(db, schedule_id)  # type: ignore[return-value]


def set_status(db: Database, schedule_id: int, status: str) -> dict:
    current = get_schedule(db, schedule_id)
    if current is None:
        raise ScheduleError("Schedule not found")
    if status not in ("active", "paused"):
        raise ScheduleError("Status must be active or paused")
    next_run = iso(next_run_after(current["recurrence"], current["timezone"], utcnow())) if status == "active" else None
    db.execute(
        "UPDATE schedules SET status = ?, next_run_at = ?, updated_at = ? WHERE id = ?",
        (status, next_run, now_iso(), schedule_id),
    )
    db.audit("user", f"schedule.{status}", {"schedule_id": schedule_id})
    return get_schedule(db, schedule_id)  # type: ignore[return-value]


def delete_schedule(db: Database, schedule_id: int) -> bool:
    return db.execute("DELETE FROM schedules WHERE id = ?", (schedule_id,)).rowcount > 0


def due_schedules(db: Database, now: datetime | None = None) -> list[dict]:
    now = now or utcnow()
    return [
        _present(r)
        for r in db.all(
            "SELECT * FROM schedules WHERE status = 'active' AND next_run_at IS NOT NULL AND next_run_at <= ? ORDER BY next_run_at",
            (iso(now),),
        )
    ]


def scheduled_prompt(schedule: dict) -> str:
    return (
        f"This is an automatic scheduled run of \"{schedule['name']}\" ({schedule['description'].lower()}). "
        "Nobody is waiting to answer questions, so use sensible defaults instead of asking. "
        "If the task is about opportunities, call list_saved_opportunities first so you can skip ones that are already "
        "saved and unchanged, re-check saved ones whose deadlines or details may have changed, and clearly list what "
        "is NEW in this run.\n\n"
        f"Task: {schedule['prompt']}"
    )


def claim_due_slot(db: Database, schedule: dict, now: datetime | None = None) -> int | None:
    """Create the task for this schedule's due slot exactly once and advance the schedule.

    Returns the new task id, or None if the slot was already claimed, the previous run
    is still going, or the schedule changed under us.
    """
    now = now or utcnow()
    slot = schedule["next_run_at"]
    with db.tx():
        fresh = db.one("SELECT * FROM schedules WHERE id = ?", (schedule["id"],))
        if fresh is None or fresh["status"] != "active" or fresh["next_run_at"] != slot:
            return None
        rec = loads(fresh["recurrence"], {})
        following = iso(next_run_after(rec, fresh["timezone"], max(now, parse_iso(slot) or now)))
        busy = db.one(
            "SELECT id FROM tasks WHERE schedule_id = ? AND status IN ('queued', 'running') LIMIT 1", (schedule["id"],)
        )
        db.execute(
            "UPDATE schedules SET next_run_at = ?, updated_at = ? WHERE id = ? AND next_run_at = ?",
            (following, now_iso(), schedule["id"], slot),
        )
        if busy:
            db.audit("system", "schedule.skipped_busy", {"schedule_id": schedule["id"], "slot": slot})
            return None
        cursor = db.execute(
            "INSERT OR IGNORE INTO tasks (kind, title, prompt, schedule_id, scheduled_for, status, created_at, updated_at) "
            "VALUES ('scheduled', ?, ?, ?, ?, 'queued', ?, ?)",
            (fresh["name"][:200], scheduled_prompt(_present(fresh)), schedule["id"], slot, now_iso(), now_iso()),
        )
        if cursor.rowcount != 1:
            return None
        task_id = int(cursor.lastrowid)
        db.execute("UPDATE schedules SET last_run_at = ?, last_task_id = ? WHERE id = ?", (now_iso(), task_id, schedule["id"]))
        return task_id
