"""User profile, saved preferences ("memories"), and runtime-editable settings."""

from __future__ import annotations

from ..db import Database, dumps, loads, now_iso

DEFAULT_PROFILE = {
    "name": "",
    "location": "Shanghai, China",
    "citizenship": "Uzbekistan",
    "education": "Undergraduate student in Artificial Intelligence",
    "interests": ["AI", "robotics", "research", "hackathons", "startup opportunities"],
    "projects": [
        {"name": "MindForm", "description": ""},
        {"name": "Defex Robotics", "description": ""},
    ],
    "languages": "",
    "constraints": "",
    "goals": "",
    "notes": "",
}

MAX_MEMORIES = 200
MAX_MEMORY_CHARS = 600

RUNTIME_KEYS = {"timezone", "model_provider", "task_max_cost_usd", "daily_max_cost_usd", "anthropic_effort"}


def get_profile(db: Database) -> dict:
    row = db.one("SELECT data FROM profile WHERE id = 1")
    if row is None:
        return dict(DEFAULT_PROFILE)
    data = loads(row["data"], {})
    merged = dict(DEFAULT_PROFILE)
    merged.update(data)
    return merged


def ensure_profile(db: Database) -> None:
    if db.one("SELECT id FROM profile WHERE id = 1") is None:
        db.execute("INSERT INTO profile (id, data, updated_at) VALUES (1, ?, ?)", (dumps(DEFAULT_PROFILE), now_iso()))


def save_profile(db: Database, data: dict) -> dict:
    clean = dict(DEFAULT_PROFILE)
    for key in DEFAULT_PROFILE:
        if key in data:
            clean[key] = data[key]
    if not isinstance(clean["interests"], list):
        clean["interests"] = [s.strip() for s in str(clean["interests"]).split(",") if s.strip()]
    clean["interests"] = [str(s)[:100] for s in clean["interests"]][:30]
    projects = []
    for item in clean.get("projects") or []:
        if isinstance(item, dict) and str(item.get("name", "")).strip():
            projects.append(
                {"name": str(item["name"]).strip()[:120], "description": str(item.get("description", "")).strip()[:2000]}
            )
    clean["projects"] = projects[:20]
    for key in ("name", "location", "citizenship", "education", "languages", "constraints", "goals", "notes"):
        clean[key] = str(clean.get(key) or "").strip()[:3000]
    db.execute(
        "INSERT INTO profile (id, data, updated_at) VALUES (1, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET data = excluded.data, updated_at = excluded.updated_at",
        (dumps(clean), now_iso()),
    )
    return clean


def render_profile(profile: dict) -> str:
    lines = []
    for label, key in (
        ("Name", "name"),
        ("Based in", "location"),
        ("Citizenship", "citizenship"),
        ("Education", "education"),
        ("Languages", "languages"),
    ):
        if profile.get(key):
            lines.append(f"- {label}: {profile[key]}")
    if profile.get("interests"):
        lines.append(f"- Interests: {', '.join(profile['interests'])}")
    projects = profile.get("projects") or []
    if projects:
        lines.append("- Projects:")
        for p in projects:
            desc = p.get("description") or "(no description yet)"
            lines.append(f"  - {p['name']}: {desc}")
    for label, key in (("Goals", "goals"), ("Constraints", "constraints"), ("Other notes", "notes")):
        if profile.get(key):
            lines.append(f"- {label}: {profile[key]}")
    return "\n".join(lines) if lines else "- (profile is empty)"


# -- memories ------------------------------------------------------------------------

def list_memories(db: Database) -> list[dict]:
    return db.all("SELECT id, content, source, task_id, created_at, updated_at FROM memories ORDER BY id")


def add_memory(db: Database, content: str, source: str = "user", task_id: int | None = None,
               call_id: str | None = None) -> dict:  # fmt: skip
    content = (content or "").strip()
    if not content:
        raise ValueError("Memory text is empty")
    if len(content) > MAX_MEMORY_CHARS:
        raise ValueError(f"Memory text is longer than {MAX_MEMORY_CHARS} characters")
    if task_id is not None and call_id is not None:
        existing = db.one("SELECT * FROM memories WHERE task_id = ? AND source_call_id = ?", (task_id, call_id))
        if existing:
            return existing
    if (db.scalar("SELECT COUNT(*) FROM memories") or 0) >= MAX_MEMORIES:
        raise ValueError(f"There are already {MAX_MEMORIES} saved notes; delete some first")
    now = now_iso()
    memory_id = db.insert(
        "memories",
        {"content": content, "source": source, "task_id": task_id, "source_call_id": call_id, "created_at": now,
         "updated_at": now},  # fmt: skip
    )
    return db.one("SELECT * FROM memories WHERE id = ?", (memory_id,))  # type: ignore[return-value]


def update_memory(db: Database, memory_id: int, content: str) -> dict | None:
    content = (content or "").strip()
    if not content or len(content) > MAX_MEMORY_CHARS:
        raise ValueError("Memory text must be 1-600 characters")
    db.execute("UPDATE memories SET content = ?, updated_at = ? WHERE id = ?", (content, now_iso(), memory_id))
    return db.one("SELECT * FROM memories WHERE id = ?", (memory_id,))


def delete_memory(db: Database, memory_id: int) -> bool:
    return db.execute("DELETE FROM memories WHERE id = ?", (memory_id,)).rowcount > 0


def render_memories(memories: list[dict]) -> str:
    if not memories:
        return "- (none yet)"
    return "\n".join(f"- {m['content']}" for m in memories[-60:])


# -- runtime settings -------------------------------------------------------------------

def get_runtime_settings(db: Database) -> dict:
    return {row["key"]: loads(row["value"]) for row in db.all("SELECT key, value FROM settings")}


def set_runtime_setting(db: Database, key: str, value) -> None:
    if key not in RUNTIME_KEYS:
        raise ValueError(f"'{key}' cannot be changed at runtime")
    if value is None:
        db.execute("DELETE FROM settings WHERE key = ?", (key,))
        return
    db.execute(
        "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
        (key, dumps(value), now_iso()),
    )
