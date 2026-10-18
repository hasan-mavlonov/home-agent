"""Workspace confinement and opportunity storage rules (no network)."""

from __future__ import annotations

import os

import pytest
from conftest import make_settings

from home_agent.db import Database, now_iso
from home_agent.services import opportunities as opp
from home_agent.services.workspace import Workspace, WorkspaceError


def test_workspace_rejects_escapes(tmp_path):
    ws = Workspace(tmp_path / "ws")
    outside = tmp_path / "secret.txt"
    outside.write_text("top secret")
    for bad in ["../secret.txt", "/etc/passwd", "notes/../../secret.txt", "~/x.md", ".env", "a/.hidden.md", "C:/x.md", ""]:
        with pytest.raises(WorkspaceError):
            ws.resolve(bad)
    os.symlink(outside, ws.root / "link.txt")
    with pytest.raises(WorkspaceError):
        ws.read_text("link.txt")
    os.symlink(tmp_path, ws.root / "dirlink")
    with pytest.raises(WorkspaceError):
        ws.write_text("dirlink/escape.md", "x")
    assert not (tmp_path / "escape.md").exists()


def test_workspace_write_rules(tmp_path):
    ws = Workspace(tmp_path / "ws")
    assert ws.write_text("notes/plan.md", "# Plan")["path"] == "notes/plan.md"
    assert ws.read_text("notes/plan.md")[0] == "# Plan"
    with pytest.raises(WorkspaceError):
        ws.write_text("run.sh", "rm -rf /")
    with pytest.raises(WorkspaceError):
        ws.write_text("big.txt", "x" * 1_100_000)
    saved = ws.save_upload("../../evil name.pdf", b"%PDF-1.4")
    assert saved["path"] == "uploads/evil name.pdf"
    with pytest.raises(WorkspaceError):
        ws.save_upload("tool.exe", b"MZ")


def _db(tmp_path):
    db = Database(make_settings(tmp_path).db_path)
    db.migrate()
    return db


def base(**kw):
    data = {"name": "Global AI Grant 2026", "category": "grant", "summary": "A grant", "relevance_score": 70,
            "relevance_reason": "AI", "eligibility_status": "unclear", "official_url": "https://grants.example.org/ai-2026",
            "facts": [], "source_urls": ["https://grants.example.org/ai-2026"], "deadline": {"date": "2026-12-01"}}  # fmt: skip
    data.update(kw)
    return data


def test_dedupe_by_url_name_and_fuzzy_match_keeps_user_fields(tmp_path):
    db = _db(tmp_path)
    first = opp.save_opportunity(db, None, base(), None, "Asia/Shanghai")
    assert first["created"]
    opp.update_flags(db, first["id"], bookmarked=True, notes="apply with MindForm")
    same_url = opp.save_opportunity(db, None, base(name="AI Grant (Global) 2026", prize_or_funding="$50k"), None, "Asia/Shanghai")
    assert not same_url["created"] and same_url["id"] == first["id"]
    fuzzy = opp.save_opportunity(db, None, base(official_url=None, name="Global AI Grants 2026"), None, "Asia/Shanghai")
    assert fuzzy["id"] == first["id"]
    other_year = opp.save_opportunity(db, None, base(official_url=None, name="Global AI Grant 2027", deadline={"date": "2027-12-01"}), None, "Asia/Shanghai")
    assert other_year["created"]
    row = opp.get_opportunity(db, first["id"], "Asia/Shanghai")
    assert row["bookmarked"] and row["notes"] == "apply with MindForm" and row["prize"] == "$50k"
    bare_domain = opp.save_opportunity(db, None, base(name="Different Program", official_url="https://grants.example.org/"), None, "Asia/Shanghai")
    assert bare_domain["created"]  # a shared home page is not treated as the same opportunity


def test_expiry_uses_deadline_time_zone(tmp_path):
    db = _db(tmp_path)
    past = opp.save_opportunity(db, None, base(name="Old Hackathon 2025", official_url="https://old.example.org/x", deadline={"date": "2025-03-01"}), None, "Asia/Shanghai")
    assert past["expired"]
    future = opp.save_opportunity(db, None, base(name="Future 2027", official_url="https://future.example.org/x", deadline={"date": "2027-03-01"}), None, "Asia/Shanghai")
    assert not future["expired"]
    no_deadline_past_event = opp.save_opportunity(db, None, base(name="Past Event", official_url="https://ev.example.org/x", deadline=None, event_start="2024-01-01"), None, "Asia/Shanghai")
    assert no_deadline_past_event["expired"]
    listing = opp.list_opportunities(db, "Asia/Shanghai")
    assert {o["name"]: o["expired"] for o in listing}["Old Hackathon 2025"] is True


def test_csv_export_and_dismiss(tmp_path):
    db = _db(tmp_path)
    saved = opp.save_opportunity(db, None, base(), None, "Asia/Shanghai")
    opp.update_flags(db, saved["id"], dismissed=True)
    assert opp.list_opportunities(db, "Asia/Shanghai") == []
    assert len(opp.list_opportunities(db, "Asia/Shanghai", include_dismissed=True)) == 1
    csv_text = opp.export_csv(db, "Asia/Shanghai")
    assert "Global AI Grant 2026" in csv_text.splitlines()[1]


def test_source_must_have_been_consulted(tmp_path):
    from home_agent.services.sources import TaskSources, record_fetch

    db = _db(tmp_path)
    task_id = db.insert("tasks", {"kind": "chat", "title": "t", "prompt": "p", "status": "running", "created_at": now_iso(), "updated_at": now_iso()})
    with pytest.raises(ValueError, match="consulted"):
        opp.save_opportunity(db, task_id, base(), TaskSources(db, task_id), "Asia/Shanghai")
    record_fetch(db, task_id, url="https://grants.example.org/ai-2026", final_url="https://grants.example.org/ai-2026", ok=True,
                 title="AI Grant", text="Deadline: 1 December 2026. Applicants must be enrolled students.", links=[],
                 status_code=200, content_type="text/html", error=None, kind="fetched")  # fmt: skip
    facts = [{"field": "deadline", "value": "2026-12-01", "status": "verified", "source_url": "https://www.grants.example.org/ai-2026/",
              "quote": "Deadline: 1 December 2026"},
             {"field": "eligibility", "value": "students", "status": "verified", "source_url": "https://grants.example.org/ai-2026",
              "quote": "applicants must be enrolled  students"},
             {"field": "prize", "value": "$1M", "status": "verified", "source_url": "https://grants.example.org/ai-2026", "quote": "$1,000,000 prize"}]  # fmt: skip
    result = opp.save_opportunity(db, task_id, base(facts=facts, application_url="https://elsewhere.example.org/apply"), TaskSources(db, task_id), "Asia/Shanghai")
    row = opp.get_opportunity(db, result["id"], "Asia/Shanghai")
    status = {f["field"]: f["status"] for f in row["facts"]}
    assert status == {"deadline": "verified", "eligibility": "verified", "prize": "unverified", "application_url": "unverified"}
    assert row["verification_status"] == "partially_verified"
    assert any("application URL" in w for w in result["warnings"])


def test_quote_shortened_with_ellipsis_needs_every_part():
    from home_agent.util import quote_in_text

    page = "Prizes\n\n## Total prize pool: $17,000+\n\nAMD Prizes\n\n### $5,000 in prizes from AMD\n\nTotal: €1,011.50"
    assert quote_in_text("Total prize pool: $17,000+...AMD Prizes $5,000", page)
    assert quote_in_text("Total prize pool: $17,000+ [...] AMD Prizes $5,000", page)
    assert not quote_in_text("Total prize pool: $17,000+...Evolus Track: €1,011.50", page)  # second part is stitched
    assert not quote_in_text("Total prize pool: $17,000+...$5", page)  # parts too short to prove anything


def test_fact_without_source_url_is_checked_on_the_cited_pages_only(tmp_path):
    from home_agent.services.sources import TaskSources, record_fetch

    db = _db(tmp_path)
    task_id = db.insert("tasks", {"kind": "chat", "title": "t", "prompt": "p", "status": "running", "created_at": now_iso(), "updated_at": now_iso()})
    for url, text in [("https://grants.example.org/ai-2026", "Applicants must be enrolled students. Free to apply."),
                      ("https://other.example.org/event", "Deadline: 1 December 2026. Free to apply.")]:  # fmt: skip
        record_fetch(db, task_id, url=url, final_url=url, ok=True, title="Page", text=text, links=[], status_code=200,
                     content_type="text/html", error=None, kind="fetched")  # fmt: skip
    facts = [{"field": "eligibility", "value": "students", "status": "verified", "quote": "Applicants must be enrolled students"},
             {"field": "deadline", "value": "2026-12-01", "status": "verified", "quote": "Deadline: 1 December 2026"}]  # fmt: skip
    checked, warnings = opp.check_facts(facts, TaskSources(db, task_id), ["https://grants.example.org/ai-2026"])
    by_field = {f["field"]: f for f in checked}
    assert by_field["eligibility"]["status"] == "verified"
    assert by_field["eligibility"]["source_url"] == "https://grants.example.org/ai-2026"
    # the deadline quote is only on a page that was not cited for this opportunity
    assert by_field["deadline"]["status"] == "unverified" and by_field["deadline"]["check"] == "quote not found on the fetched page"
    assert any("the cited pages" in w for w in warnings)


def test_recap_page_is_flagged_as_a_possible_past_edition(tmp_path):
    from home_agent.services.sources import TaskSources, record_fetch

    db = _db(tmp_path)
    task_id = db.insert("tasks", {"kind": "chat", "title": "t", "prompt": "p", "status": "running", "created_at": now_iso(), "updated_at": now_iso()})
    url = "https://grants.example.org/ai-2026"
    record_fetch(db, task_id, url=url, final_url=url, ok=True, title="[Recap] Global AI Grant 2026", text="Thanks to all teams.",
                 links=[], status_code=200, content_type="text/html", error=None, kind="fetched")  # fmt: skip
    result = opp.save_opportunity(db, task_id, base(), TaskSources(db, task_id), "Asia/Shanghai")
    assert result["past_edition"]
    assert any("finished or closed edition" in w for w in result["warnings"])
    other = db.insert("tasks", {"kind": "chat", "title": "t", "prompt": "p", "status": "running", "created_at": now_iso(), "updated_at": now_iso()})
    record_fetch(db, other, url=url, final_url=url, ok=True, title="Global AI Grant 2026", text="Applications close on 1 December 2026.",
                 links=[], status_code=200, content_type="text/html", error=None, kind="fetched")  # fmt: skip
    assert not opp.save_opportunity(db, other, base(), TaskSources(db, other), "Asia/Shanghai")["past_edition"]
