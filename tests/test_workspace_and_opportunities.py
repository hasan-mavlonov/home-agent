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


def test_quote_with_ellipsis_needs_every_piece_in_order_and_close():
    import time

    from home_agent.util import quote_in_text

    page = ("Prizes: $10,000 for first place, $5,000 for second place and $2,500 for third place. "
            + "Filler text about the venue and the schedule. " * 40
            + "Judging criteria: impact and originality.")  # fmt: skip
    assert quote_in_text("Prizes: $10,000 ... $2,500 for third place", page)
    assert quote_in_text("Prizes: $10,000 … $2,500 for third place", page)  # one-character ellipsis
    assert quote_in_text("prizes: $10,000 [...] third place.", page)
    assert not quote_in_text("Prizes: $10,000 ... $50,000 grand prize", page)  # a piece that is not on the page
    assert not quote_in_text("$2,500 for third place ... Prizes: $10,000", page)  # wrong order
    assert not quote_in_text("Prizes: $10,000 ... Judging criteria: impact", page)  # pieces ~1,900 characters apart
    assert quote_in_text("Prizes: $10,000 for first place", page)  # plain quotes behave as before
    assert not quote_in_text("... ...", page)
    # the first "second round" is too far from the final piece; the later one is close enough
    pages = "Round one opens. Second round. " + "x " * 150 + "Second round. " + "y " * 150 + "Final round closes."
    assert quote_in_text("Round one opens ... Second round ... Final round closes", pages)
    assert not quote_in_text("Round one opens ... Final round closes", pages)
    started = time.monotonic()
    assert not quote_in_text("aaaa ... aaaa ... aaaa ... aaaa ... aaab", "a" * 100_000 + "b")  # every piece is present
    assert not quote_in_text("ab ab ... ab ab ... zz", "ab " * 9_000 + "c" * 600 + "zz")  # under the hit cap, too far
    assert quote_in_text("ab ab ... ab ab ... zz", "ab " * 9_000 + "zz")
    assert time.monotonic() - started < 2  # repetitive pages cannot make the check explode


def _task_with_pages(db, pages: dict[str, tuple[str, str]]):
    from home_agent.services.sources import record_fetch

    task_id = db.insert("tasks", {"kind": "chat", "title": "t", "prompt": "p", "status": "running", "created_at": now_iso(), "updated_at": now_iso()})
    for url, (title, text) in pages.items():
        record_fetch(db, task_id, url=url, final_url=url, ok=True, title=title, text=text, links=[], status_code=200,
                     content_type="text/html", error=None, kind="fetched")  # fmt: skip
    return task_id


def test_facts_without_a_source_are_checked_against_the_opportunity_pages_only(tmp_path):
    from home_agent.services.sources import TaskSources

    db = _db(tmp_path)
    official, rules, unrelated = "https://grants.example.org/ai-2026", "https://grants.example.org/ai-2026/rules", "https://news.example.org/x"
    apply = "https://grants.example.org/ai-2026/apply"
    task_id = _task_with_pages(db, {
        official: ("AI Grant 2026", "Submissions close on 30 November 2026 (AoE)."),
        rules: ("Rules", "Applicants must be enrolled at a university in any country."),
        unrelated: ("Other news", "Prize pool: $1,000,000 for the winning team."),
        apply: ("Apply", "Applying is free of charge. Start your application."),
    })  # fmt: skip
    facts = [
        # no source_url: found on the official page, which is attached to the fact
        {"field": "deadline", "value": "2026-11-30", "status": "verified", "quote": "Submissions close on 30 November 2026"},
        # names the official page, but the quote is on the cited rules page: verified there, source corrected
        {"field": "eligibility", "value": "students", "status": "verified", "source_url": official,
         "quote": "must be enrolled at a university"},
        # only on a page the task read but this opportunity does not cite: not accepted
        {"field": "prize", "value": "$1M", "status": "verified", "quote": "Prize pool: $1,000,000"},
        # a deadline-style claim that appears nowhere
        {"field": "event_dates", "value": "2026-12-12", "status": "verified", "quote": "Final event on 12 December 2026"},
        # on the application page, which is cited only as application_url
        {"field": "cost", "value": "free", "status": "verified", "quote": "Applying is free of charge"},
    ]  # fmt: skip
    data = base(facts=facts, source_urls=[official, rules], application_url=apply)
    result = opp.save_opportunity(db, task_id, data, TaskSources(db, task_id), "Asia/Shanghai")
    row = opp.get_opportunity(db, result["id"], "Asia/Shanghai")
    by_field = {f["field"]: f for f in row["facts"]}
    assert by_field["deadline"]["status"] == "verified" and by_field["deadline"]["source_url"] == official
    assert by_field["eligibility"]["status"] == "verified" and by_field["eligibility"]["source_url"] == rules
    assert any("source corrected" in w for w in result["warnings"])
    assert by_field["prize"]["status"] == "unverified"
    assert by_field["prize"]["check"] == "quote not found on the pages cited for this opportunity"
    assert by_field["event_dates"]["status"] == "unverified"
    assert result["unconfirmed"] == ["event_dates", "prize"]
    assert row["field_checks"]["prize"] == "quote not found on the pages cited for this opportunity"
    assert row["warnings"] == []  # nothing suggests a past edition
    assert by_field["cost"]["status"] == "verified" and by_field["cost"]["source_url"] == apply
    assert {s["url"] for s in row["sources"]} == {official, rules, apply}  # the page the cost quote is on is listed

    # a fact naming a page that was never fetched, with no cited page fetched either
    other = _task_with_pages(db, {})
    db.insert("sources", {"task_id": other, "url": official, "final_url": official, "title": "x", "kind": "search_result",
                          "ok": 1, "accessed_at": now_iso()})  # fmt: skip
    lone = [{"field": "deadline", "value": "2026-11-30", "status": "verified", "source_url": official, "quote": "Submissions close"}]
    result = opp.save_opportunity(db, other, base(name="Other Grant", official_url=official + "/b", facts=lone, source_urls=[official]),
                                  TaskSources(db, other), "Asia/Shanghai")  # fmt: skip
    fact = opp.get_opportunity(db, result["id"], "Asia/Shanghai")["facts"][0]
    assert fact["status"] == "unverified" and fact["check"] == "source page was not fetched in this task"


def test_past_edition_pages_are_flagged_and_the_notice_is_kept_on_the_record(tmp_path):
    from datetime import datetime

    from home_agent.services.sources import TaskSources

    db = _db(tmp_path)
    year = datetime.now().year
    recap = "https://events.example.org/ai-hackathon-act-iii"
    task_id = _task_with_pages(db, {recap: ("[Recap] Example AI Hackathon: Act III",
                                            "Thank you for participating! 120 teams built agents over three days.")})  # fmt: skip
    facts = [{"field": "deadline", "value": f"{year}-12-01", "status": "verified", "source_url": recap,
              "quote": f"Registration closes 1 December {year}"}]  # not on the page
    data = base(name="Example AI Hackathon: Act III", official_url=recap, source_urls=[recap], facts=facts,
                deadline={"date": f"{year}-12-01"})  # fmt: skip
    result = opp.save_opportunity(db, task_id, data, TaskSources(db, task_id), "Asia/Shanghai")
    assert result["unconfirmed"] == ["deadline"]
    assert len(result["past_editions"]) == 1
    reasons = result["past_editions"][0]["reasons"]
    assert reasons == ['the title says "Recap"', 'the page says "thank you for participating"']
    assert any("may describe a past edition" in w and "say so in your answer" in w for w in result["warnings"])
    row = opp.get_opportunity(db, result["id"], "Asia/Shanghai")
    assert [(n["kind"], n["url"]) for n in row["warnings"]] == [("past_edition", recap)]
    assert row["field_status"]["deadline"] == "unverified"
    assert opp.compact_list(db, "Asia/Shanghai", None, False, 10)[0]["warnings"] == [row["warnings"][0]["message"]]
    assert "may describe a past edition" in opp.export_csv(db, "Asia/Shanghai")

    # a manual edit (no task sources) keeps the notice; a later clean check replaces it
    opp.save_opportunity(db, None, data | {"facts": []}, None, "Asia/Shanghai")
    assert opp.get_opportunity(db, result["id"], "Asia/Shanghai")["warnings"] == row["warnings"]
    current = "https://events.example.org/ai-hackathon-act-iv"
    later = _task_with_pages(db, {current: (f"Example AI Hackathon {year}", f"Registration closes on 1 December {year}.")})
    opp.save_opportunity(db, later, data | {"official_url": current, "source_urls": [current], "facts": []},
                         TaskSources(db, later), "Asia/Shanghai")  # fmt: skip
    assert opp.get_opportunity(db, result["id"], "Asia/Shanghai")["warnings"] == []


def test_past_edition_signals():
    y = 2026
    reasons = opp.past_edition_reasons
    assert reasons({"title": "Example Hackathon 2024", "text": ""}, y) == ["the title only mentions 2024"]
    assert reasons({"title": "Example Fellowship 2025-26", "text": ""}, y) == []  # spans the current year
    assert reasons({"title": "Example Fellowship 2025/2026", "text": ""}, y) == []
    assert reasons({"title": "Results published 2025-12-01", "text": ""}, y) == ["the title only mentions 2025"]
    assert reasons({"title": "Demo day 2026-03-01", "text": ""}, y) == []
    assert reasons({"title": "Example Hackathon 2026", "text": "Registration closes on 1 December."}, y) == []
    assert reasons({"title": "Challenge", "text": "Submissions are now closed. Winners will be announced soon."}, y) == [
        'the page says "submissions are now closed"']
    assert reasons({"title": "Challenge", "text": "The hackathon has ended."}, y) == ['the page says "the hackathon has ended"']
    # an aggregator listing other, closed events further down is not flagged
    listing = "Upcoming hackathons this month. " + "Details about another event. " * 120 + "Registration closed"
    assert reasons({"title": "All hackathons", "text": listing}, y) == []


def test_upgrade_from_first_schema_adds_opportunity_warnings(tmp_path):
    import sqlite3

    from home_agent.db import MIGRATIONS, _split_sql

    path = make_settings(tmp_path).db_path
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    for statement in _split_sql(MIGRATIONS[0]):
        conn.execute(statement)
    conn.execute("PRAGMA user_version = 1")
    conn.execute("INSERT INTO opportunities (dedupe_key, name, category, verification_status, first_seen_at, last_checked_at) "
                 "VALUES ('name:old|2026', 'Old record', 'grant', 'unverified', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')")  # fmt: skip
    conn.commit()
    conn.close()
    db = Database(path)
    db.migrate()
    assert db.scalar("PRAGMA user_version") == len(MIGRATIONS)
    row = opp.list_opportunities(db, "Asia/Shanghai")[0]
    assert row["name"] == "Old record" and row["warnings"] == []
