"""What the agent actually looked at during a task (search results and fetched pages).

These records back every "verified" claim: the opportunity and email tools check
quotes, links and addresses against the pages fetched in the same task.
"""

from __future__ import annotations

from ..db import Database, dumps, loads, now_iso
from ..util import EMAIL_PATTERN, normalize_url


def record_search_results(db: Database, task_id: int, results) -> None:
    now = now_iso()
    for r in results:
        db.insert(
            "sources",
            {"task_id": task_id, "url": r.url, "final_url": r.url, "title": r.title[:500], "kind": "search_result",
             "ok": 1, "snippet": (r.snippet or "")[:1000], "accessed_at": now},  # fmt: skip
        )


def record_fetch(db: Database, task_id: int, *, url: str, final_url: str, ok: bool, title: str | None, text: str,
                 links: list[dict], status_code: int | None, content_type: str | None, error: str | None,
                 kind: str) -> int:  # fmt: skip
    return db.insert(
        "sources",
        {"task_id": task_id, "url": url, "final_url": final_url, "title": (title or "")[:500], "kind": kind,
         "ok": 1 if ok else 0, "status_code": status_code, "content_type": content_type, "error": error,
         "text": text if ok else None, "links": dumps(links[:200]) if ok else None, "accessed_at": now_iso()},  # fmt: skip
    )


class TaskSources:
    def __init__(self, db: Database, task_id: int):
        self.rows = db.all(
            "SELECT id, url, final_url, title, kind, ok, text, links, accessed_at, snippet FROM sources WHERE task_id = ? ORDER BY id",
            (task_id,),
        )
        self._fetched: dict[str, dict] = {}
        self._consulted: dict[str, dict] = {}
        for row in self.rows:
            keys = {normalize_url(row["url"]), normalize_url(row["final_url"] or row["url"])}
            for key in keys:
                self._consulted.setdefault(key, row)
                if row["kind"] in ("fetched", "rendered") and row["ok"] and row["text"]:
                    self._fetched[key] = row

    def fetched(self, url: str | None) -> dict | None:
        if not url:
            return None
        return self._fetched.get(normalize_url(url))

    def consulted(self, url: str | None) -> dict | None:
        if not url:
            return None
        return self._consulted.get(normalize_url(url))

    def fetched_rows(self) -> list[dict]:
        seen = set()
        rows = []
        for row in self._fetched.values():
            if row["id"] not in seen:
                seen.add(row["id"])
                rows.append(row)
        return rows

    def url_seen_on_fetched_page(self, url: str) -> bool:
        target = normalize_url(url)
        for row in self.fetched_rows():
            if normalize_url(row["final_url"] or row["url"]) == target:
                return True
            for link in loads(row["links"], []) or []:
                if normalize_url(link.get("url", "")) == target:
                    return True
            if url in (row["text"] or ""):
                return True
        return False

    def email_seen(self, address: str) -> bool:
        address = address.lower()
        for row in self.fetched_rows():
            if address in (row["text"] or "").lower():
                return True
            for link in loads(row["links"], []) or []:
                if link.get("url", "").lower() == f"mailto:{address}":
                    return True
        return False

    def emails_found(self) -> set[str]:
        found = set()
        for row in self.fetched_rows():
            found.update(e.lower() for e in EMAIL_PATTERN.findall(row["text"] or ""))
        return found


def list_task_sources(db: Database, task_id: int) -> list[dict]:
    rows = db.all(
        "SELECT id, url, final_url, title, kind, ok, status_code, error, snippet, accessed_at FROM sources "
        "WHERE task_id = ? ORDER BY id",
        (task_id,),
    )
    # one entry per URL, preferring the strongest evidence (rendered/fetched over search snippet)
    rank = {"rendered": 0, "fetched": 1, "search_result": 2}
    best: dict[str, dict] = {}
    order: list[str] = []
    for row in rows:
        key = normalize_url(row["final_url"] or row["url"])
        current = best.get(key)
        if current is None:
            best[key] = row
            order.append(key)
        elif (rank.get(row["kind"], 3), 0 if row["ok"] else 1) < (rank.get(current["kind"], 3), 0 if current["ok"] else 1):
            best[key] = row
    return [best[k] for k in order]
