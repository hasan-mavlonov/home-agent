"""Optional Gmail integration: OAuth (PKCE) and draft create/update/send.

Scope requested: https://www.googleapis.com/auth/gmail.compose only - the narrowest
scope that covers creating and sending drafts. Home Agent never reads your mailbox.
Sending is a separate switch (GMAIL_ALLOW_SEND) and is only reachable through the
approval flow in services/drafts.py, never from the agent's tools.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import secrets
import time
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx2

from ..config import Settings
from ..db import Database, in_seconds, now_iso, parse_iso, utcnow
from ..security.crypto import SecretBox

SCOPE = "https://www.googleapis.com/auth/gmail.compose"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
API_BASE = "https://gmail.googleapis.com/gmail/v1/users/me"


class GmailError(Exception):
    def __init__(self, message: str, *, status: int | None = None, definitely_not_sent: bool = False):
        super().__init__(message)
        self.status = status
        self.definitely_not_sent = definitely_not_sent


class GmailService:
    def __init__(self, settings: Settings, db: Database, box: SecretBox | None,
                 transport: httpx2.AsyncBaseTransport | None = None):  # fmt: skip
        self.settings = settings
        self.db = db
        self.box = box
        self._lock = asyncio.Lock()
        self.last_error: str | None = None
        self.client = httpx2.AsyncClient(
            timeout=httpx2.Timeout(30.0, connect=10.0),
            proxy=settings.outbound_proxy,
            trust_env=False,
            transport=transport,
        )

    # -- status -----------------------------------------------------------------------
    @property
    def configured(self) -> bool:
        return bool(self.settings.gmail_client_id and self.settings.gmail_client_secret and self.box)

    def _token_row(self) -> dict | None:
        return self.db.one("SELECT * FROM oauth_tokens WHERE provider = 'gmail'")

    @property
    def connected(self) -> bool:
        return self._token_row() is not None

    def status(self) -> dict:
        row = self._token_row()
        return {
            "configured": self.configured,
            "connected": row is not None,
            "account": row["account"] if row else None,
            "scopes": row["scopes"] if row else SCOPE,
            "allow_send": self.settings.gmail_allow_send,
            "redirect_uri": self.settings.effective_gmail_redirect_uri,
            "last_error": self.last_error,
        }

    # -- OAuth --------------------------------------------------------------------------
    def start_authorization(self) -> str:
        if not self.configured:
            raise GmailError("Set GMAIL_CLIENT_ID and GMAIL_CLIENT_SECRET in .env first (see README: Gmail setup).")
        state = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(64)[:96]
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        self.db.execute("DELETE FROM oauth_states WHERE expires_at < ?", (now_iso(),))
        self.db.insert(
            "oauth_states",
            {"state_hash": _hash(state), "provider": "gmail", "verifier": self.box.encrypt(verifier),  # type: ignore[union-attr]
             "created_at": now_iso(), "expires_at": in_seconds(600)},  # fmt: skip
        )
        params = {
            "client_id": self.settings.gmail_client_id,
            "redirect_uri": self.settings.effective_gmail_redirect_uri,
            "response_type": "code",
            "scope": SCOPE,
            "access_type": "offline",
            "prompt": "consent",
            "include_granted_scopes": "false",
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        return f"{AUTH_URL}?{urlencode(params)}"

    @staticmethod
    def parse_redirect(url: str) -> tuple[str | None, str | None, str | None]:
        query = parse_qs(urlsplit(url.strip()).query)
        return (query.get("code") or [None])[0], (query.get("state") or [None])[0], (query.get("error") or [None])[0]

    async def complete_authorization(self, code: str | None, state: str | None, error: str | None = None) -> str:
        if error:
            raise GmailError(f"Google returned an error: {error}")
        if not code or not state:
            raise GmailError("The redirect URL is missing the code or state parameter.")
        row = self.db.one("SELECT * FROM oauth_states WHERE state_hash = ?", (_hash(state),))
        if row is None or row["used_at"] or (parse_iso(row["expires_at"]) or utcnow()) < utcnow():
            raise GmailError("This authorization link is invalid or expired. Start the Gmail connection again.")
        self.db.execute("UPDATE oauth_states SET used_at = ? WHERE state_hash = ?", (now_iso(), row["state_hash"]))
        verifier = self.box.decrypt(row["verifier"])  # type: ignore[union-attr]
        response = await self.client.post(
            TOKEN_URL,
            data={
                "client_id": self.settings.gmail_client_id,
                "client_secret": self.settings.gmail_client_secret.get_secret_value(),  # type: ignore[union-attr]
                "code": code,
                "code_verifier": verifier,
                "grant_type": "authorization_code",
                "redirect_uri": self.settings.effective_gmail_redirect_uri,
            },
        )
        if response.status_code != 200:
            raise GmailError(f"Token exchange failed (HTTP {response.status_code}): {_google_error(response)}")
        token = response.json()
        if not token.get("refresh_token"):
            raise GmailError("Google did not return a refresh token. Remove Home Agent's access in your Google account "
                             "settings and connect again.")  # fmt: skip
        granted = token.get("scope", "")
        if SCOPE not in granted.split():
            raise GmailError("The gmail.compose permission was not granted, so drafts cannot be created.")
        self._store_token(token, account=None)
        account = await self._fetch_account()
        self.db.execute("UPDATE oauth_tokens SET account = ? WHERE provider = 'gmail'", (account,))
        self.db.audit("user", "gmail.connected", {"account": account})
        self.last_error = None
        return account or "connected"

    def _store_token(self, token: dict, account: str | None) -> None:
        existing = self._load_token()
        refresh = token.get("refresh_token") or (existing or {}).get("refresh_token")
        data = {
            "access_token": token.get("access_token"),
            "expires_at": time.time() + float(token.get("expires_in", 3000)) - 60,
            "refresh_token": refresh,
            "scope": token.get("scope") or (existing or {}).get("scope", SCOPE),
        }
        encrypted = self.box.encrypt(json.dumps(data))  # type: ignore[union-attr]
        self.db.execute(
            "INSERT INTO oauth_tokens (provider, data, account, scopes, updated_at) VALUES ('gmail', ?, ?, ?, ?) "
            "ON CONFLICT(provider) DO UPDATE SET data = excluded.data, scopes = excluded.scopes, "
            "updated_at = excluded.updated_at, account = COALESCE(excluded.account, oauth_tokens.account)",
            (encrypted, account, data["scope"], now_iso()),
        )

    def _load_token(self) -> dict | None:
        row = self._token_row()
        if row is None or self.box is None:
            return None
        return json.loads(self.box.decrypt(row["data"]))

    async def _access_token(self) -> str:
        async with self._lock:
            token = self._load_token()
            if token is None:
                raise GmailError("Gmail is not connected.", definitely_not_sent=True)
            if token.get("access_token") and token.get("expires_at", 0) > time.time():
                return token["access_token"]
            response = await self.client.post(
                TOKEN_URL,
                data={
                    "client_id": self.settings.gmail_client_id,
                    "client_secret": self.settings.gmail_client_secret.get_secret_value(),  # type: ignore[union-attr]
                    "grant_type": "refresh_token",
                    "refresh_token": token["refresh_token"],
                },
            )
            if response.status_code != 200:
                detail = _google_error(response)
                self.last_error = (
                    f"Could not refresh Gmail access ({detail}). If your Google Cloud OAuth app is in 'Testing' mode, "
                    "refresh tokens expire after 7 days - reconnect Gmail, or set the app to 'In production'."
                )
                raise GmailError(self.last_error, status=response.status_code, definitely_not_sent=True)
            self._store_token(response.json(), account=None)
            return response.json()["access_token"]

    async def _fetch_account(self) -> str | None:
        try:
            data = await self._api("GET", "/profile")
            return data.get("emailAddress")
        except GmailError:
            return None

    async def disconnect(self) -> None:
        token = self._load_token()
        if token and token.get("refresh_token"):
            try:
                await self.client.post(REVOKE_URL, data={"token": token["refresh_token"]})
            except httpx2.HTTPError:
                pass
        self.db.execute("DELETE FROM oauth_tokens WHERE provider = 'gmail'")
        self.db.audit("user", "gmail.disconnected")

    # -- API calls -------------------------------------------------------------------------
    async def _api(self, method: str, path: str, body: dict | None = None, params: dict | None = None) -> dict:
        access = await self._access_token()
        try:
            response = await self.client.request(
                method, f"{API_BASE}{path}", json=body, params=params, headers={"Authorization": f"Bearer {access}"}
            )
        except httpx2.TimeoutException:
            raise GmailError("Gmail did not respond in time.") from None
        except httpx2.HTTPError as exc:
            raise GmailError(f"Could not reach Gmail ({type(exc).__name__}).") from None
        if response.status_code >= 400:
            raise GmailError(
                f"Gmail API error (HTTP {response.status_code}): {_google_error(response)}",
                status=response.status_code,
                definitely_not_sent=400 <= response.status_code < 500 and response.status_code != 408,
            )
        return response.json() if response.content else {}

    async def create_draft(self, raw: str) -> str:
        data = await self._api("POST", "/drafts", {"message": {"raw": raw}})
        return data["id"]

    async def update_draft(self, draft_id: str, raw: str) -> str:
        data = await self._api("PUT", f"/drafts/{draft_id}", {"id": draft_id, "message": {"raw": raw}})
        return data.get("id", draft_id)

    async def send_draft(self, draft_id: str, raw: str) -> str:
        # The approved content is sent together with the send call, so edits made in
        # Gmail after approval cannot change what goes out.
        data = await self._api("POST", "/drafts/send", {"id": draft_id, "message": {"raw": raw}})
        return data.get("id", "")

    async def draft_exists(self, draft_id: str) -> bool | None:
        """True/False if known; None if Gmail could not be asked."""
        try:
            await self._api("GET", f"/drafts/{draft_id}", params={"format": "minimal"})
            return True
        except GmailError as exc:
            if exc.status == 404:
                return False
            return None

    async def aclose(self) -> None:
        await self.client.aclose()


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _google_error(response: httpx2.Response) -> str:
    try:
        data = response.json()
    except ValueError:
        return response.text[:200]
    error = data.get("error")
    if isinstance(error, dict):
        return str(error.get("message") or error.get("status") or error)[:200]
    return str(data.get("error_description") or error or data)[:200]
