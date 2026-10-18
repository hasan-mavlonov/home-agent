"""Single-user password login with server-side sessions and CSRF tokens."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import threading
import time
from dataclasses import dataclass
from datetime import timedelta

from ..db import Database, iso, now_iso, parse_iso, utcnow

SCRYPT_N, SCRYPT_R, SCRYPT_P = 2**15, 8, 1
MIN_PASSWORD_LENGTH = 10
SESSION_COOKIE = "ha_session"


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, maxmem=64 * 1024 * 1024)
    return "scrypt${}${}${}${}${}".format(
        SCRYPT_N, SCRYPT_R, SCRYPT_P, base64.b64encode(salt).decode(), base64.b64encode(digest).decode()
    )


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt_b64, digest_b64 = stored.split("$")
        if scheme != "scrypt":
            return False
        expected = base64.b64decode(digest_b64)
        actual = hashlib.scrypt(password.encode("utf-8"), salt=base64.b64decode(salt_b64), n=int(n), r=int(r), p=int(p),
                                maxmem=64 * 1024 * 1024, dklen=len(expected))  # fmt: skip
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


def _token_id(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


@dataclass
class Session:
    id: str
    csrf_token: str
    expires_at: str


class LoginLimiter:
    """Slows down password guessing: per-client and global failure windows."""

    def __init__(self, per_client: int = 5, global_limit: int = 20, window: float = 900.0):
        self.per_client = per_client
        self.global_limit = global_limit
        self.window = window
        self._failures: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def _recent(self, key: str, now: float) -> list[float]:
        items = [t for t in self._failures.get(key, []) if now - t < self.window]
        self._failures[key] = items
        return items

    def blocked_for(self, client: str) -> float:
        now = time.time()
        with self._lock:
            for key, limit in ((client, self.per_client), ("*", self.global_limit)):
                items = self._recent(key, now)
                if len(items) >= limit:
                    return max(1.0, self.window - (now - items[0]))
        return 0.0

    def failure(self, client: str) -> None:
        now = time.time()
        with self._lock:
            for key in (client, "*"):
                self._recent(key, now).append(now)

    def success(self, client: str) -> None:
        with self._lock:
            self._failures.pop(client, None)


class AuthService:
    def __init__(self, db: Database, session_days: int = 30):
        self.db = db
        self.session_days = session_days
        self.limiter = LoginLimiter()

    def password_is_set(self) -> bool:
        return self.db.one("SELECT value FROM meta WHERE key = 'password_hash'") is not None

    def set_password(self, password: str) -> None:
        if len(password) < MIN_PASSWORD_LENGTH:
            raise ValueError(f"Use at least {MIN_PASSWORD_LENGTH} characters")
        self.db.execute(
            "INSERT INTO meta (key, value) VALUES ('password_hash', ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (hash_password(password),),
        )
        self.db.execute("DELETE FROM sessions")  # changing the password signs out every device
        self.db.audit("cli", "password.changed")

    def check_password(self, password: str) -> bool:
        row = self.db.one("SELECT value FROM meta WHERE key = 'password_hash'")
        return row is not None and verify_password(password, row["value"])

    def create_session(self, user_agent: str | None, ip: str | None) -> tuple[str, Session]:
        token = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(32)
        expires = iso(utcnow() + timedelta(days=self.session_days))
        self.db.insert(
            "sessions",
            {"id": _token_id(token), "csrf_token": csrf, "created_at": now_iso(), "last_seen_at": now_iso(),
             "expires_at": expires, "user_agent": (user_agent or "")[:300], "ip": (ip or "")[:100]},  # fmt: skip
        )
        self.db.audit("user", "login", {"ip": ip})
        return token, Session(id=_token_id(token), csrf_token=csrf, expires_at=expires)  # type: ignore[arg-type]

    def get_session(self, token: str | None) -> Session | None:
        if not token or len(token) > 200:
            return None
        row = self.db.one("SELECT * FROM sessions WHERE id = ?", (_token_id(token),))
        if row is None:
            return None
        now = utcnow()
        if (parse_iso(row["expires_at"]) or now) <= now:
            self.db.execute("DELETE FROM sessions WHERE id = ?", (row["id"],))
            return None
        last_seen = parse_iso(row["last_seen_at"]) or now
        if now - last_seen > timedelta(minutes=5):
            self.db.execute(
                "UPDATE sessions SET last_seen_at = ?, expires_at = ? WHERE id = ?",
                (iso(now), iso(now + timedelta(days=self.session_days)), row["id"]),
            )
        return Session(id=row["id"], csrf_token=row["csrf_token"], expires_at=row["expires_at"])

    def delete_session(self, token: str | None) -> None:
        if token:
            self.db.execute("DELETE FROM sessions WHERE id = ?", (_token_id(token),))

    def revoke_all(self) -> int:
        return self.db.execute("DELETE FROM sessions").rowcount
