"""Schedules: recurrence math, exactly-once slots, missed-run coalescing, confirmation rules."""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta

from conftest import make_settings
from helpers import ScriptedProvider, wait_for

from home_agent.db import Database, iso, utcnow
from home_agent.scheduler import Scheduler
from home_agent.services import schedules as svc


def db_for(tmp_path) -> Database:
    db = Database(make_settings(tmp_path).db_path)
    db.migrate()
    return db


def test_next_run_daily_weekly_monthly_in_shanghai():
    after = datetime(2026, 10, 18, 2, 0, tzinfo=UTC)  # 10:00 in Shanghai, a Sunday
    daily = svc.next_run_after({"freq": "daily", "time": "09:00"}, "Asia/Shanghai", after)
    assert daily == datetime(2026, 10, 19, 1, 0, tzinfo=UTC)  # tomorrow 09:00 local
    weekly = svc.next_run_after({"freq": "weekly", "weekday": 0, "time": "09:00"}, "Asia/Shanghai", after)
    assert weekly == datetime(2026, 10, 19, 1, 0, tzinfo=UTC)  # Monday
    same_day = svc.next_run_after({"freq": "weekly", "weekday": 6, "time": "18:30"}, "Asia/Shanghai", after)
    assert same_day == datetime(2026, 10, 18, 10, 30, tzinfo=UTC)
    monthly = svc.next_run_after({"freq": "monthly", "day": 31, "time": "08:00"}, "Asia/Shanghai", after)
    assert monthly == datetime(2026, 10, 31, 0, 0, tzinfo=UTC)
    february = svc.next_run_after({"freq": "monthly", "day": 31, "time": "08:00"}, "Asia/Shanghai", datetime(2027, 2, 1, tzinfo=UTC))
    assert february.date().isoformat() == "2027-02-28"  # clamped to the last day


def test_dst_zone_keeps_local_time():
    after = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)
    first = svc.next_run_after({"freq": "daily", "time": "09:00"}, "Europe/Berlin", after)
    second = svc.next_run_after({"freq": "daily", "time": "09:00"}, "Europe/Berlin", first + timedelta(hours=23))
    # Berlin leaves summer time on 2026-10-25; both runs are at 09:00 local = 08:00 UTC
    assert first.hour == 8 and second.hour == 8


def test_slot_is_claimed_exactly_once_even_concurrently(tmp_path):
    db = db_for(tmp_path)
    schedule = svc.create_schedule(db, name="Weekly scan", prompt="Find AI hackathons", recurrence={"freq": "weekly", "weekday": 0, "time": "09:00"},
                                   timezone="Asia/Shanghai", created_by="user")  # fmt: skip
    slot = iso(utcnow() - timedelta(minutes=1))
    db.execute("UPDATE schedules SET next_run_at = ? WHERE id = ?", (slot, schedule["id"]))
    due = svc.due_schedules(db)
    assert len(due) == 1
    results = []

    def claim():
        local = Database(db.path)
        results.append(svc.claim_due_slot(local, due[0]))

    threads = [threading.Thread(target=claim) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len([r for r in results if r]) == 1
    assert db.scalar("SELECT COUNT(*) FROM tasks WHERE schedule_id = ?", (schedule["id"],)) == 1
    nxt = db.one("SELECT next_run_at FROM schedules WHERE id = ?", (schedule["id"],))["next_run_at"]
    assert nxt > iso(utcnow())  # advanced into the future


def test_missed_slots_after_downtime_run_once(tmp_path):
    db = db_for(tmp_path)
    schedule = svc.create_schedule(db, name="Daily", prompt="Check things", recurrence={"freq": "daily", "time": "09:00"},
                                   timezone="Asia/Shanghai", created_by="user")  # fmt: skip
    # the computer was off for ten days
    db.execute("UPDATE schedules SET next_run_at = ? WHERE id = ?", (iso(utcnow() - timedelta(days=10)), schedule["id"]))
    for _ in range(3):  # several scheduler ticks after the restart
        for due in svc.due_schedules(db):
            svc.claim_due_slot(db, due)
    assert db.scalar("SELECT COUNT(*) FROM tasks WHERE schedule_id = ?", (schedule["id"],)) == 1
    assert svc.due_schedules(db) == []


def test_busy_schedule_skips_slot(tmp_path):
    db = db_for(tmp_path)
    schedule = svc.create_schedule(db, name="Daily", prompt="Check things", recurrence={"freq": "daily", "time": "09:00"},
                                   timezone="Asia/Shanghai", created_by="user")  # fmt: skip
    db.execute("UPDATE schedules SET next_run_at = ? WHERE id = ?", (iso(utcnow() - timedelta(minutes=5)), schedule["id"]))
    first = svc.claim_due_slot(db, svc.due_schedules(db)[0])
    assert first
    db.execute("UPDATE schedules SET next_run_at = ? WHERE id = ?", (iso(utcnow() - timedelta(minutes=1)), schedule["id"]))
    assert svc.claim_due_slot(db, svc.due_schedules(db)[0]) is None  # previous run still queued
    assert db.scalar("SELECT COUNT(*) FROM tasks") == 1


def test_agent_proposals_need_confirmation(tmp_path):
    db = db_for(tmp_path)
    proposed = svc.create_schedule(db, name="Weekly", prompt="Find grants weekly", recurrence={"freq": "weekly", "weekday": 2, "time": "10:00"},
                                   timezone="Asia/Shanghai", created_by="agent", status="pending_confirmation", task_id=1, call_id="c1")  # fmt: skip
    assert proposed["status"] == "pending_confirmation" and proposed["next_run_at"] is None
    again = svc.create_schedule(db, name="Weekly", prompt="Find grants weekly", recurrence={"freq": "weekly", "weekday": 2, "time": "10:00"},
                                timezone="Asia/Shanghai", created_by="agent", status="pending_confirmation", task_id=1, call_id="c1")  # fmt: skip
    assert again["id"] == proposed["id"]  # idempotent per tool call
    db.execute("UPDATE schedules SET next_run_at = ? WHERE id = ?", (iso(utcnow() - timedelta(days=1)), proposed["id"]))
    assert svc.due_schedules(db) == []  # never runs until confirmed
    active = svc.set_status(db, proposed["id"], "active")
    assert active["next_run_at"] is not None
    paused = svc.set_status(db, proposed["id"], "paused")
    assert paused["next_run_at"] is None


def test_scheduled_task_runs_and_reports_new_items(harness_factory):
    from home_agent.agent.tools import tools_for

    assert "propose_schedule" not in {t.name for t in tools_for("scheduled")}
    provider = ScriptedProvider([lambda m: ("text", "Nothing new this week.")])
    h = harness_factory(provider=provider)
    with h:
        h.login()
        created = h.post("/api/schedules", json={"name": "Weekly AI scan", "prompt": "Find new AI hackathons",
                                                 "recurrence": {"freq": "weekly", "weekday": 0, "time": "09:00"}}).json()  # fmt: skip
        assert created["status"] == "active" and created["timezone"] == "Asia/Shanghai"
        h.db.execute("UPDATE schedules SET next_run_at = ? WHERE id = ?", (iso(utcnow() - timedelta(seconds=5)), created["id"]))
        scheduler = Scheduler(h.services)
        first = scheduler.tick()
        second = scheduler.tick()
        assert len(first) == 1 and second == []
        task = wait_for(lambda: (t := h.get(f"/api/tasks/{first[0]}").json())["status"] == "succeeded" and t)
        assert task["kind"] == "scheduled" and "list_saved_opportunities" in task["prompt"]
        assert any(n["title"].startswith("Done: Weekly AI scan") for n in h.get("/api/notifications").json()["items"])
        # pause / edit / delete through the API
        assert h.post(f"/api/schedules/{created['id']}/pause").json()["status"] == "paused"
        edited = h.put(f"/api/schedules/{created['id']}", json={"recurrence": {"freq": "daily", "time": "07:30"}}).json()
        assert edited["description"] == "Every day at 07:30"
        assert h.delete(f"/api/schedules/{created['id']}").status_code == 200
        assert h.get("/api/schedules").json()["items"] == []
