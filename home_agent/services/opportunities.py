"""Saved opportunities: verification checks, de-duplication, expiry, user flags."""

from __future__ import annotations

import csv
import difflib
import io
from datetime import date, datetime, time

from ..db import Database, dumps, loads, now_iso
from ..util import get_zone, normalize_url, quote_in_text, slugify, url_key
from .sources import TaskSources

CATEGORIES = ["hackathon", "competition", "grant", "accelerator", "fellowship", "scholarship", "conference",
              "program", "other"]  # fmt: skip
FACT_FIELDS = ["deadline", "event_dates", "location", "format", "eligibility", "cost", "prize", "application_url",
               "edition_year", "organizer"]  # fmt: skip
KEY_FIELDS = ("deadline", "eligibility", "application_url")
STATUS_RANK = {"verified": 3, "inferred": 2, "unverified": 1, "unknown": 0}


def _year_of(*values: str | None) -> str:
    for value in values:
        if value and len(value) >= 4 and value[:4].isdigit():
            return value[:4]
    return ""


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def is_expired(row: dict, user_tz: str) -> bool:
    today = datetime.now(get_zone(row.get("deadline_timezone") or user_tz, user_tz)).date()
    deadline = _parse_date(row.get("deadline_date"))
    if deadline is not None:
        if deadline < today:
            return True
        if deadline == today and row.get("deadline_time"):
            try:
                cutoff = datetime.combine(deadline, time.fromisoformat(row["deadline_time"]),
                                          get_zone(row.get("deadline_timezone") or user_tz, user_tz))  # fmt: skip
                return datetime.now(cutoff.tzinfo) > cutoff
            except ValueError:
                return False
        return False
    end = _parse_date(row.get("event_end")) or _parse_date(row.get("event_start"))
    return end is not None and end < today


def check_facts(facts: list[dict], sources: TaskSources) -> tuple[list[dict], list[str]]:
    """Downgrade 'verified' facts that are not backed by a page fetched in this task."""
    checked, warnings = [], []
    for fact in facts:
        fact = dict(fact)
        field = fact.get("field")
        status = fact.get("status", "unknown")
        if status == "verified":
            page = sources.fetched(fact.get("source_url"))
            quote = (fact.get("quote") or "").strip()
            if page is None:
                fact["status"] = "unverified"
                fact["check"] = "source page was not fetched in this task"
                warnings.append(f"{field}: marked verified but its source page was not fetched; saved as unverified")
            elif not quote:
                fact["status"] = "unverified"
                fact["check"] = "no supporting quote given"
                warnings.append(f"{field}: marked verified without a quote; saved as unverified")
            elif not quote_in_text(quote, page["text"] or ""):
                fact["status"] = "unverified"
                fact["check"] = "quote not found on the fetched page"
                warnings.append(f"{field}: the quote was not found on {fact.get('source_url')}; saved as unverified")
            else:
                fact["check"] = "quote found on fetched page"
                fact["checked_at"] = page["accessed_at"]
        checked.append(fact)
    return checked, warnings


def _fact_status(facts: list[dict], field: str) -> str:
    statuses = [f.get("status", "unknown") for f in facts if f.get("field") == field]
    if not statuses:
        return "unknown"
    return max(statuses, key=lambda s: STATUS_RANK.get(s, 0))


def overall_status(facts: list[dict]) -> str:
    key_statuses = [_fact_status(facts, f) for f in KEY_FIELDS]
    if all(s == "verified" for s in key_statuses):
        return "verified"
    if any(f.get("status") == "verified" for f in facts):
        return "partially_verified"
    return "unverified"


def _find_existing(db: Database, name: str, year: str, urls: list[str]) -> dict | None:
    for url in urls:
        key = url_key(url)
        if "/" not in key.strip("/"):
            continue  # a bare domain (organizer home page) can host many programs
        for row in db.all(
            "SELECT * FROM opportunities WHERE official_url IS NOT NULL OR application_url IS NOT NULL"
        ):
            for existing in (row["official_url"], row["application_url"]):
                if existing and url_key(existing) == key:
                    return row
    slug = slugify(name)
    row = db.one("SELECT * FROM opportunities WHERE dedupe_key = ?", (f"name:{slug}|{year}",))
    if row:
        return row
    for row in db.all("SELECT * FROM opportunities"):
        other_year = _year_of(row["deadline_date"], row["event_start"])
        if year and other_year and year != other_year:
            continue
        if difflib.SequenceMatcher(None, slug, slugify(row["name"])).ratio() >= 0.9:
            return row
    return None


def _merge_sources(old: list[dict], new: list[dict]) -> list[dict]:
    merged: dict[str, dict] = {}
    for item in old + new:
        key = normalize_url(item["url"])
        if key not in merged or (item.get("accessed_at") or "") > (merged[key].get("accessed_at") or ""):
            merged[key] = item
    return sorted(merged.values(), key=lambda s: s.get("accessed_at") or "", reverse=True)[:30]


def _merge_facts(old: list[dict], new: list[dict]) -> list[dict]:
    by_field: dict[str, dict] = {f.get("field"): f for f in old}
    for fact in new:
        by_field[fact.get("field")] = fact  # the latest check wins
    return list(by_field.values())


def save_opportunity(db: Database, task_id: int | None, data: dict, sources: TaskSources | None,
                     user_tz: str) -> dict:  # fmt: skip
    warnings: list[str] = []
    facts = data.get("facts") or []
    if sources is not None:
        facts, fact_warnings = check_facts(facts, sources)
        warnings += fact_warnings
        consulted, dropped = [], []
        for url in data.get("source_urls") or []:
            row = sources.consulted(url)
            if row is None:
                dropped.append(url)
            else:
                consulted.append(
                    {"url": row["final_url"] or row["url"], "title": row["title"] or "", "accessed_at": row["accessed_at"],
                     "kind": row["kind"], "task_id": task_id}  # fmt: skip
                )
        if dropped:
            warnings.append(
                "Dropped sources that were not opened or seen in search during this task: " + ", ".join(dropped[:5])
            )
        if not consulted:
            raise ValueError(
                "None of the source_urls were consulted in this task. Search for or fetch the official page first; "
                "opportunities must cite pages you actually looked at."
            )
        application_url = data.get("application_url")
        if application_url and not (sources.consulted(application_url) or sources.url_seen_on_fetched_page(application_url)):
            warnings.append("The application URL was not seen on any fetched page; marked unverified")
            facts = [f for f in facts if f.get("field") != "application_url"]
            facts.append({"field": "application_url", "value": application_url, "status": "unverified",
                          "check": "link not seen on a fetched page"})  # fmt: skip
    else:
        consulted = [{"url": u, "title": "", "accessed_at": now_iso(), "kind": "manual", "task_id": None}
                     for u in data.get("source_urls") or []]  # fmt: skip

    deadline = data.get("deadline") or {}
    name = data["name"].strip()
    year = _year_of(deadline.get("date"), data.get("event_start"))
    values = {
        "name": name[:300],
        "category": data.get("category") if data.get("category") in CATEGORIES else "other",
        "organizer": data.get("organizer"),
        "summary": data.get("summary"),
        "relevance_score": max(0, min(100, int(data.get("relevance_score") or 0))),
        "relevance_reason": data.get("relevance_reason"),
        "eligibility": data.get("eligibility"),
        "eligibility_status": data.get("eligibility_status"),
        "deadline_date": deadline.get("date"),
        "deadline_time": deadline.get("time"),
        "deadline_timezone": deadline.get("timezone"),
        "deadline_text": deadline.get("text"),
        "event_start": data.get("event_start"),
        "event_end": data.get("event_end"),
        "location": data.get("location"),
        "format": data.get("format") or "unknown",
        "cost": data.get("cost"),
        "prize": data.get("prize_or_funding"),
        "application_url": data.get("application_url"),
        "official_url": data.get("official_url"),
    }
    now = now_iso()
    with db.tx():
        existing = _find_existing(db, name, year, [u for u in (values["official_url"], values["application_url"]) if u])
        if existing is None:
            merged_facts = facts
            row_values = {
                **values,
                "dedupe_key": f"name:{slugify(name)}|{year}",
                "facts": dumps(merged_facts),
                "verification_status": overall_status(merged_facts),
                "sources": dumps(_merge_sources([], consulted)),
                "first_seen_at": now,
                "last_checked_at": now,
                "first_seen_task_id": task_id,
                "last_task_id": task_id,
            }
            opp_id = db.insert("opportunities", row_values)
            created = True
        else:
            merged_facts = _merge_facts(loads(existing["facts"], []), facts)
            # keep the first name as the record's stable identity; refresh everything else
            update = {k: v for k, v in values.items() if v not in (None, "") and k != "name"}
            update.update(
                {
                    "facts": dumps(merged_facts),
                    "verification_status": overall_status(merged_facts),
                    "sources": dumps(_merge_sources(loads(existing["sources"], []), consulted)),
                    "last_checked_at": now,
                    "last_task_id": task_id,
                }
            )
            db.update("opportunities", existing["id"], update)
            opp_id = existing["id"]
            created = False
    row = get_opportunity(db, opp_id, user_tz)
    return {
        "id": opp_id,
        "created": created,
        "verification_status": row["verification_status"] if row else None,
        "expired": row["expired"] if row else False,
        "warnings": warnings,
    }


def _present(row: dict, user_tz: str) -> dict:
    out = dict(row)
    out["facts"] = loads(row["facts"], [])
    out["sources"] = loads(row["sources"], [])
    out["expired"] = is_expired(row, user_tz)
    out["is_new"] = row["seen_at"] is None
    out["bookmarked"] = bool(row["bookmarked"])
    out["dismissed"] = bool(row["dismissed"])
    present = {
        "deadline": row["deadline_date"] or row["deadline_text"],
        "event_dates": row["event_start"],
        "location": row["location"] or (row["format"] if row["format"] != "unknown" else None),
        "format": row["format"] if row["format"] != "unknown" else None,
        "eligibility": row["eligibility"],
        "cost": row["cost"],
        "prize": row["prize"],
        "application_url": row["application_url"],
        "organizer": row["organizer"],
        "edition_year": row["deadline_date"],
    }
    statuses = {}
    for field in FACT_FIELDS:
        status = _fact_status(out["facts"], field)
        if status == "unknown" and present.get(field) and not any(f.get("field") == field for f in out["facts"]):
            status = "unverified"  # a value was saved without saying where it came from
        statuses[field] = status
    out["field_status"] = statuses
    out.pop("dedupe_key", None)
    return out


def get_opportunity(db: Database, opp_id: int, user_tz: str) -> dict | None:
    row = db.one("SELECT * FROM opportunities WHERE id = ?", (opp_id,))
    return _present(row, user_tz) if row else None


def list_opportunities(db: Database, user_tz: str, *, include_dismissed: bool = False, query: str | None = None,
                       limit: int = 500) -> list[dict]:  # fmt: skip
    sql = "SELECT * FROM opportunities"
    params: list = []
    clauses = []
    if not include_dismissed:
        clauses.append("dismissed = 0")
    if query:
        clauses.append("(name LIKE ? OR organizer LIKE ? OR summary LIKE ? OR category LIKE ?)")
        like = f"%{query}%"
        params += [like, like, like, like]
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY COALESCE(deadline_date, '9999-12-31'), id DESC LIMIT ?"
    params.append(limit)
    return [_present(r, user_tz) for r in db.all(sql, tuple(params))]


def update_flags(db: Database, opp_id: int, *, bookmarked: bool | None = None, dismissed: bool | None = None,
                 notes: str | None = None, seen: bool | None = None) -> bool:  # fmt: skip
    values: dict = {}
    if bookmarked is not None:
        values["bookmarked"] = 1 if bookmarked else 0
    if dismissed is not None:
        values["dismissed"] = 1 if dismissed else 0
    if notes is not None:
        values["notes"] = notes[:5000]
    if seen is not None:
        values["seen_at"] = now_iso() if seen else None
    return db.update("opportunities", opp_id, values) > 0 if values else True


def mark_all_seen(db: Database) -> int:
    return db.execute("UPDATE opportunities SET seen_at = ? WHERE seen_at IS NULL", (now_iso(),)).rowcount


def delete_opportunity(db: Database, opp_id: int) -> bool:
    return db.execute("DELETE FROM opportunities WHERE id = ?", (opp_id,)).rowcount > 0


def compact_list(db: Database, user_tz: str, query: str | None, include_dismissed: bool, limit: int) -> list[dict]:
    rows = list_opportunities(db, user_tz, include_dismissed=include_dismissed, query=query, limit=limit)
    return [
        {
            "id": r["id"],
            "name": r["name"],
            "category": r["category"],
            "deadline": r["deadline_date"],
            "deadline_timezone": r["deadline_timezone"],
            "expired": r["expired"],
            "verification": r["verification_status"],
            "official_url": r["official_url"],
            "application_url": r["application_url"],
            "last_checked_at": r["last_checked_at"],
            "dismissed": r["dismissed"],
            "bookmarked": r["bookmarked"],
        }
        for r in rows
    ]


def _cell(value) -> str:
    """Neutralise spreadsheet formulas: values come from web pages."""
    text = "" if value is None else str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


def export_csv(db: Database, user_tz: str) -> str:
    rows = list_opportunities(db, user_tz, include_dismissed=True)
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["name", "category", "relevance", "eligibility_status", "eligibility", "deadline", "deadline_time",
                     "deadline_timezone", "location", "format", "cost", "prize_or_funding", "application_url",
                     "official_url", "verification", "expired", "bookmarked", "dismissed", "notes", "sources",
                     "last_checked_at"])  # fmt: skip
    for r in rows:
        writer.writerow([_cell(v) for v in [r["name"], r["category"], r["relevance_score"], r["eligibility_status"], r["eligibility"],
                         r["deadline_date"], r["deadline_time"], r["deadline_timezone"], r["location"], r["format"],
                         r["cost"], r["prize"], r["application_url"], r["official_url"], r["verification_status"],
                         r["expired"], r["bookmarked"], r["dismissed"], r["notes"],
                         " ".join(s["url"] for s in r["sources"]), r["last_checked_at"]]])  # fmt: skip
    return buffer.getvalue()
