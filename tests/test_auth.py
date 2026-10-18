"""Authentication, sessions, CSRF, host checks and security headers (no mocks needed)."""

from __future__ import annotations

from conftest import PASSWORD

from home_agent.security.auth import AuthService, hash_password, verify_password


def test_password_hashing():
    stored = hash_password("a long password")
    assert stored.startswith("scrypt$")
    assert verify_password("a long password", stored)
    assert not verify_password("wrong", stored)
    assert not verify_password("a long password", "garbage")


def test_every_api_route_requires_a_session(harness_factory):
    from home_agent.web.routes import api, public

    public_paths = {r.path for r in public.routes}
    assert public_paths == {"/api/health", "/api/auth/session", "/api/auth/login", "/api/integrations/gmail/callback"}
    h = harness_factory()
    with h:
        checked = 0
        for route in api.routes:
            for method in route.methods - {"HEAD", "OPTIONS"}:
                url = route.path.replace("{conversation_id}", "1").replace("{task_id}", "1").replace("{opp_id}", "1") \
                    .replace("{draft_id}", "1").replace("{schedule_id}", "1").replace("{memory_id}", "1")  # fmt: skip
                r = h.client.request(method, url)
                assert r.status_code == 401, (method, url, r.status_code)
                checked += 1
        assert checked > 40


def test_login_session_cookie_and_logout(harness_factory):
    h = harness_factory()
    with h:
        assert h.get("/api/auth/session").json() == {"authenticated": False, "password_set": True, "csrf_token": None}
        assert h.client.post("/api/auth/login", json={"password": "nope"}).status_code == 401
        r = h.login()
        cookie = r.headers["set-cookie"].lower()
        assert "httponly" in cookie and "samesite=strict" in cookie and "path=/" in cookie
        assert h.get("/api/auth/session").json()["authenticated"] is True
        assert h.get("/api/tasks").status_code == 200
        assert h.post("/api/auth/logout").status_code == 200
        assert h.get("/api/tasks").status_code == 401


def test_csrf_and_cross_site_protection(harness_factory):
    h = harness_factory()
    with h:
        h.login()
        assert h.client.post("/api/conversations", json={}).status_code == 403  # no token
        assert h.client.post("/api/conversations", json={}, headers={"X-CSRF-Token": "wrong"}).status_code == 403
        r = h.client.post("/api/conversations", json={}, headers={"X-CSRF-Token": h.csrf, "Sec-Fetch-Site": "cross-site"})
        assert r.status_code == 403
        assert h.post("/api/conversations", json={}).status_code == 200
        # GETs do not need the token
        assert h.get("/api/conversations").status_code == 200


def test_login_rate_limit(harness_factory):
    h = harness_factory()
    with h:
        for _ in range(5):
            assert h.client.post("/api/auth/login", json={"password": "wrong"}).status_code == 401
        r = h.client.post("/api/auth/login", json={"password": PASSWORD})
        assert r.status_code == 429  # locked out even with the right password


def test_password_change_signs_out_everyone(harness_factory):
    h = harness_factory()
    with h:
        h.login()
        AuthService(h.db).set_password("another long password")
        assert h.get("/api/tasks").status_code == 401


def test_no_password_set(harness_factory):
    h = harness_factory()
    with h:
        h.db.execute("DELETE FROM meta WHERE key = 'password_hash'")
        r = h.client.post("/api/auth/login", json={"password": "anything at all"})
        assert r.status_code == 409 and "set-password" in r.json()["detail"]


def test_host_header_and_security_headers(harness_factory):
    h = harness_factory()
    with h:
        r = h.client.get("/", headers={"Host": "evil.example.com"})
        assert r.status_code == 421  # DNS-rebinding protection
        assert h.client.get("/api/health", headers={"Host": "my-mac.tail1234.ts.net"}).status_code == 200
        r = h.get("/")
        assert r.status_code == 200
        csp = r.headers["content-security-policy"]
        assert "script-src 'self'" in csp and "frame-ancestors 'none'" in csp
        assert r.headers["x-content-type-options"] == "nosniff"
        assert r.headers["referrer-policy"] == "no-referrer"


def test_secrets_never_returned_by_settings(harness_factory):
    h = harness_factory(settings_overrides={"brave_api_key": "BSA-secret-value-123", "anthropic_api_key": "sk-ant-secret-xyz-123"})
    with h:
        h.login()
        for path in ("/api/settings", "/api/integrations"):
            body = h.get(path).text
            assert "BSA-secret-value-123" not in body and "sk-ant-secret-xyz-123" not in body
