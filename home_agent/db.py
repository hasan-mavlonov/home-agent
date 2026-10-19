"""SQLite storage: one database file, WAL mode, simple ordered migrations.

Connections are per-thread (FastAPI runs sync endpoints in a thread pool, the worker
and scheduler run on the event loop thread). Writes that must be atomic go through
`Database.tx()`, which takes the write lock up front with BEGIN IMMEDIATE.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterator

MIGRATIONS: list[str] = [
    # 1: initial schema
    """
    CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

    CREATE TABLE sessions (
        id TEXT PRIMARY KEY,
        csrf_token TEXT NOT NULL,
        created_at TEXT NOT NULL,
        last_seen_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        user_agent TEXT,
        ip TEXT
    );

    CREATE TABLE conversations (
        id INTEGER PRIMARY KEY,
        title TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );

    CREATE TABLE messages (
        id INTEGER PRIMARY KEY,
        conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
        role TEXT NOT NULL,
        content TEXT NOT NULL,
        task_id INTEGER,
        created_at TEXT NOT NULL
    );
    CREATE INDEX messages_conversation ON messages(conversation_id, id);

    CREATE TABLE schedules (
        id INTEGER PRIMARY KEY,
        name TEXT NOT NULL,
        prompt TEXT NOT NULL,
        recurrence TEXT NOT NULL,
        timezone TEXT NOT NULL,
        status TEXT NOT NULL,
        next_run_at TEXT,
        last_run_at TEXT,
        last_task_id INTEGER,
        created_by TEXT NOT NULL,
        source_task_id INTEGER,
        source_call_id TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE (source_task_id, source_call_id)
    );

    CREATE TABLE tasks (
        id INTEGER PRIMARY KEY,
        kind TEXT NOT NULL,
        title TEXT NOT NULL,
        prompt TEXT NOT NULL,
        conversation_id INTEGER REFERENCES conversations(id) ON DELETE SET NULL,
        user_message_id INTEGER,
        schedule_id INTEGER REFERENCES schedules(id) ON DELETE SET NULL,
        scheduled_for TEXT,
        status TEXT NOT NULL,
        status_detail TEXT,
        cancel_requested INTEGER NOT NULL DEFAULT 0,
        attempts INTEGER NOT NULL DEFAULT 0,
        lease_owner TEXT,
        lease_expires_at TEXT,
        provider TEXT,
        model TEXT,
        steps INTEGER NOT NULL DEFAULT 0,
        tool_calls INTEGER NOT NULL DEFAULT 0,
        searches INTEGER NOT NULL DEFAULT 0,
        fetches INTEGER NOT NULL DEFAULT 0,
        input_tokens INTEGER NOT NULL DEFAULT 0,
        output_tokens INTEGER NOT NULL DEFAULT 0,
        cost_usd REAL NOT NULL DEFAULT 0,
        new_opportunities INTEGER NOT NULL DEFAULT 0,
        result_text TEXT,
        error TEXT,
        checkpoint TEXT,
        created_at TEXT NOT NULL,
        started_at TEXT,
        finished_at TEXT,
        updated_at TEXT NOT NULL,
        UNIQUE (schedule_id, scheduled_for)
    );
    CREATE INDEX tasks_status ON tasks(status, id);
    CREATE INDEX tasks_conversation ON tasks(conversation_id, id);

    CREATE TABLE task_events (
        id INTEGER PRIMARY KEY,
        task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
        ts TEXT NOT NULL,
        type TEXT NOT NULL,
        summary TEXT NOT NULL,
        data TEXT
    );
    CREATE INDEX task_events_task ON task_events(task_id, id);

    CREATE TABLE tool_calls (
        id INTEGER PRIMARY KEY,
        task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
        call_id TEXT NOT NULL,
        name TEXT NOT NULL,
        input TEXT NOT NULL,
        status TEXT NOT NULL,
        output TEXT,
        is_error INTEGER NOT NULL DEFAULT 0,
        started_at TEXT NOT NULL,
        finished_at TEXT,
        UNIQUE (task_id, call_id)
    );

    CREATE TABLE sources (
        id INTEGER PRIMARY KEY,
        task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
        url TEXT NOT NULL,
        final_url TEXT,
        title TEXT,
        kind TEXT NOT NULL,
        ok INTEGER NOT NULL DEFAULT 0,
        status_code INTEGER,
        content_type TEXT,
        error TEXT,
        snippet TEXT,
        text TEXT,
        links TEXT,
        accessed_at TEXT NOT NULL
    );
    CREATE INDEX sources_task ON sources(task_id, url);

    CREATE TABLE opportunities (
        id INTEGER PRIMARY KEY,
        dedupe_key TEXT NOT NULL UNIQUE,
        name TEXT NOT NULL,
        category TEXT NOT NULL,
        organizer TEXT,
        summary TEXT,
        relevance_score INTEGER,
        relevance_reason TEXT,
        eligibility TEXT,
        eligibility_status TEXT,
        deadline_date TEXT,
        deadline_time TEXT,
        deadline_timezone TEXT,
        deadline_text TEXT,
        event_start TEXT,
        event_end TEXT,
        location TEXT,
        format TEXT,
        cost TEXT,
        prize TEXT,
        application_url TEXT,
        official_url TEXT,
        facts TEXT NOT NULL DEFAULT '[]',
        verification_status TEXT NOT NULL,
        sources TEXT NOT NULL DEFAULT '[]',
        first_seen_at TEXT NOT NULL,
        last_checked_at TEXT NOT NULL,
        first_seen_task_id INTEGER,
        last_task_id INTEGER,
        bookmarked INTEGER NOT NULL DEFAULT 0,
        dismissed INTEGER NOT NULL DEFAULT 0,
        notes TEXT NOT NULL DEFAULT '',
        seen_at TEXT
    );

    CREATE TABLE drafts (
        id INTEGER PRIMARY KEY,
        to_addrs TEXT NOT NULL DEFAULT '[]',
        cc_addrs TEXT NOT NULL DEFAULT '[]',
        bcc_addrs TEXT NOT NULL DEFAULT '[]',
        subject TEXT NOT NULL DEFAULT '',
        body TEXT NOT NULL DEFAULT '',
        attachments TEXT NOT NULL DEFAULT '[]',
        warnings TEXT NOT NULL DEFAULT '[]',
        opportunity_id INTEGER REFERENCES opportunities(id) ON DELETE SET NULL,
        source_task_id INTEGER,
        source_call_id TEXT,
        status TEXT NOT NULL,
        approved_hash TEXT,
        approved_at TEXT,
        approval_expires_at TEXT,
        gmail_draft_id TEXT,
        gmail_synced_hash TEXT,
        gmail_message_id TEXT,
        send_attempt_id TEXT,
        send_started_at TEXT,
        sent_at TEXT,
        last_error TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE (source_task_id, source_call_id)
    );

    CREATE TABLE notifications (
        id INTEGER PRIMARY KEY,
        kind TEXT NOT NULL,
        title TEXT NOT NULL,
        body TEXT NOT NULL DEFAULT '',
        link TEXT,
        task_id INTEGER,
        created_at TEXT NOT NULL,
        read_at TEXT
    );

    CREATE TABLE memories (
        id INTEGER PRIMARY KEY,
        content TEXT NOT NULL,
        source TEXT NOT NULL,
        task_id INTEGER,
        source_call_id TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE (task_id, source_call_id)
    );

    CREATE TABLE profile (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        data TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );

    CREATE TABLE settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );

    CREATE TABLE spend (
        id INTEGER PRIMARY KEY,
        task_id INTEGER,
        ts TEXT NOT NULL,
        day TEXT NOT NULL,
        provider TEXT,
        model TEXT,
        input_tokens INTEGER NOT NULL DEFAULT 0,
        output_tokens INTEGER NOT NULL DEFAULT 0,
        cache_read_tokens INTEGER NOT NULL DEFAULT 0,
        cache_write_tokens INTEGER NOT NULL DEFAULT 0,
        cost_usd REAL NOT NULL DEFAULT 0
    );
    CREATE INDEX spend_day ON spend(day);

    CREATE TABLE oauth_tokens (
        provider TEXT PRIMARY KEY,
        data TEXT NOT NULL,
        account TEXT,
        scopes TEXT,
        updated_at TEXT NOT NULL
    );

    CREATE TABLE oauth_states (
        state_hash TEXT PRIMARY KEY,
        provider TEXT NOT NULL,
        verifier TEXT NOT NULL,
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        used_at TEXT
    );

    CREATE TABLE audit_log (
        id INTEGER PRIMARY KEY,
        ts TEXT NOT NULL,
        actor TEXT NOT NULL,
        action TEXT NOT NULL,
        detail TEXT
    );
    """,
    # 2: notices from the latest check of an opportunity (e.g. "may describe a past edition")
    """
    ALTER TABLE opportunities ADD COLUMN warnings TEXT NOT NULL DEFAULT '[]';
    """,
]


def utcnow() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def now_iso() -> str:
    return iso(utcnow())  # type: ignore[return-value]


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def in_seconds(seconds: float) -> str:
    return iso(utcnow() + timedelta(seconds=seconds))  # type: ignore[return-value]


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def loads(value: str | None, default: Any = None) -> Any:
    if value is None or value == "":
        return default
    return json.loads(value)


class Database:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._local = threading.local()
        self._all: list[sqlite3.Connection] = []
        self._lock = threading.Lock()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 30000")
        conn.execute("PRAGMA synchronous = NORMAL")
        return conn

    @property
    def conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._connect()
            self._local.conn = conn
            with self._lock:
                self._all.append(conn)
        return conn

    def migrate(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = self.conn
        conn.execute("PRAGMA journal_mode = WAL")
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        for index, script in enumerate(MIGRATIONS, start=1):
            if index <= version:
                continue
            conn.execute("BEGIN IMMEDIATE")
            try:
                for statement in _split_sql(script):
                    conn.execute(statement)
                conn.execute(f"PRAGMA user_version = {index}")
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        conn = self.conn
        if conn.in_transaction:
            # nested use: join the outer transaction
            yield conn
            return
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")

    def execute(self, sql: str, params: tuple | dict = ()) -> sqlite3.Cursor:
        return self.conn.execute(sql, params)

    def one(self, sql: str, params: tuple | dict = ()) -> dict | None:
        row = self.conn.execute(sql, params).fetchone()
        return dict(row) if row is not None else None

    def all(self, sql: str, params: tuple | dict = ()) -> list[dict]:
        return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def scalar(self, sql: str, params: tuple | dict = ()) -> Any:
        row = self.conn.execute(sql, params).fetchone()
        return row[0] if row is not None else None

    def insert(self, table: str, values: dict) -> int:
        cols = ", ".join(values)
        marks = ", ".join("?" for _ in values)
        cur = self.conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", tuple(values.values()))
        return int(cur.lastrowid)

    def update(self, table: str, row_id: int, values: dict) -> int:
        if not values:
            return 0
        assignments = ", ".join(f"{k} = ?" for k in values)
        cur = self.conn.execute(f"UPDATE {table} SET {assignments} WHERE id = ?", (*values.values(), row_id))
        return cur.rowcount

    def audit(self, actor: str, action: str, detail: Any = None) -> None:
        self.insert(
            "audit_log",
            {"ts": now_iso(), "actor": actor, "action": action, "detail": dumps(detail) if detail is not None else None},
        )

    def close(self) -> None:
        with self._lock:
            for conn in self._all:
                try:
                    conn.close()
                except sqlite3.Error:
                    pass
            self._all.clear()
        self._local = threading.local()


def _split_sql(script: str) -> list[str]:
    return [s.strip() for s in script.split(";") if s.strip()]
