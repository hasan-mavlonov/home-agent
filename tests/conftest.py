from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent))

from helpers import FakeSearch, ScriptedProvider, pages_transport, make_guard  # noqa: E402

from home_agent.config import Settings  # noqa: E402
from home_agent.db import Database  # noqa: E402
from home_agent.net.fetcher import WebFetcher  # noqa: E402
from home_agent.security.auth import AuthService  # noqa: E402
from home_agent.services.container import Services  # noqa: E402
from home_agent.web.app import create_app  # noqa: E402

PASSWORD = "correct horse battery"


def make_settings(tmp_path: Path, **overrides) -> Settings:
    values = dict(
        data_dir=tmp_path / "data",
        secret_key="test-secret-key-" + "x" * 40,
        search_provider="none",
        browser_enabled=False,
        allowed_hosts="testserver",
        model_provider="ollama",
        task_max_cost_usd=5.0,
        daily_max_cost_usd=50.0,
    )
    values.update(overrides)
    return Settings(_env_file=None, **values)


class Harness:
    """App + mocks wired together. MOCKED: model, search, web pages, Gmail."""

    def __init__(self, tmp_path: Path, provider: ScriptedProvider | None = None, pages=None, search_results=None,
                 settings_overrides=None, gmail=None, start_background=True):  # fmt: skip
        self.settings = make_settings(tmp_path, **(settings_overrides or {}))
        self.db = Database(self.settings.db_path)
        self.db.migrate()
        self.provider = provider or ScriptedProvider([lambda m: ("text", "hello")])
        self.search = FakeSearch(search_results)
        self.page_hits: list[str] = []
        guard = make_guard()
        fetcher = WebFetcher(guard, transport=pages_transport(pages or {}, self.page_hits))
        self.services = Services(self.settings, self.db, search=self.search, fetcher=fetcher, guard=guard,
                                 provider_factory=lambda name: self.provider, gmail=gmail)  # fmt: skip
        self.app = create_app(self.settings, services=self.services, start_background=start_background, use_lock=False)
        self.client = TestClient(self.app, base_url="http://testserver")
        self.csrf = None

    def __enter__(self):
        self.client.__enter__()
        AuthService(self.db).set_password(PASSWORD)
        return self

    def __exit__(self, *exc):
        self.client.__exit__(*exc)

    def login(self):
        r = self.client.post("/api/auth/login", json={"password": PASSWORD})
        assert r.status_code == 200, r.text
        self.csrf = r.json()["csrf_token"]
        return r

    def post(self, path, json=None, **kw):
        return self.client.post(path, json=json, headers={"X-CSRF-Token": self.csrf or ""}, **kw)

    def put(self, path, json=None):
        return self.client.put(path, json=json, headers={"X-CSRF-Token": self.csrf or ""})

    def patch(self, path, json=None):
        return self.client.patch(path, json=json, headers={"X-CSRF-Token": self.csrf or ""})

    def delete(self, path):
        return self.client.delete(path, headers={"X-CSRF-Token": self.csrf or ""})

    def get(self, path, **kw):
        return self.client.get(path, **kw)


@pytest.fixture
def harness_factory(tmp_path):
    created = []

    def factory(**kwargs):
        h = Harness(tmp_path, **kwargs)
        created.append(h)
        return h

    yield factory
