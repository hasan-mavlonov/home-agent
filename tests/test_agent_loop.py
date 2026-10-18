"""Agent loop behaviour with a MOCKED (scripted) model: cancellation, limits, budgets,
restart recovery without repeated side effects, and provider errors."""

from __future__ import annotations

import asyncio
import time

import pytest
from conftest import PASSWORD, Harness
from helpers import ScriptedProvider, wait_for

from home_agent.db import dumps, now_iso
from home_agent.llm import build_provider
from home_agent.llm.base import LocalModeViolation, ModelConfigError, ModelError
from home_agent.services.tasks import create_task, get_task
from home_agent.util import local_now


def start_chat(h, text="Research something"):
    conv = h.post("/api/conversations", json={}).json()
    r = h.post(f"/api/conversations/{conv['id']}/messages", json={"content": text})
    assert r.status_code == 200, r.text
    return conv["id"], r.json()["task"]["id"]


def finished(h, task_id, timeout=15):
    return wait_for(lambda: (t := h.get(f"/api/tasks/{task_id}").json())["status"] in ("succeeded", "failed", "cancelled") and t, timeout)


def test_cancel_running_task(harness_factory):
    provider = ScriptedProvider([lambda m: ("tools", [("list_files", {})])], delay=0.3)
    h = harness_factory(provider=provider)
    with h:
        h.login()
        conv_id, task_id = start_chat(h)
        wait_for(lambda: h.get(f"/api/tasks/{task_id}").json()["status"] == "running")
        started = time.time()
        assert h.post(f"/api/tasks/{task_id}/cancel").status_code == 200
        task = finished(h, task_id, timeout=5)
        assert task["status"] == "cancelled"
        assert time.time() - started < 3
        calls_at_cancel = provider.calls
        time.sleep(0.8)
        assert provider.calls == calls_at_cancel  # nothing keeps running after cancellation
        messages = h.get(f"/api/conversations/{conv_id}").json()["messages"]
        assert messages[-1]["role"] == "notice" and "cancelled" in messages[-1]["content"].lower()
        # the chat accepts a new request afterwards
        assert h.post(f"/api/conversations/{conv_id}/messages", json={"content": "again"}).status_code == 200


def test_cancel_queued_task(harness_factory):
    h = harness_factory(start_background=False)
    with h:
        h.login()
        conv = h.post("/api/conversations", json={}).json()
        task_id = h.post(f"/api/conversations/{conv['id']}/messages", json={"content": "x"}).json()["task"]["id"]
        assert h.post(f"/api/tasks/{task_id}/cancel").json()["status"] == "cancelled"


def test_one_request_at_a_time_per_chat(harness_factory):
    provider = ScriptedProvider([lambda m: ("tools", [("list_files", {})])], delay=0.2)
    h = harness_factory(provider=provider)
    with h:
        h.login()
        conv_id, task_id = start_chat(h)
        r = h.post(f"/api/conversations/{conv_id}/messages", json={"content": "second"})
        assert r.status_code == 409
        h.post(f"/api/tasks/{task_id}/cancel")


def test_step_limit_forces_a_tool_free_final_turn(harness_factory):
    provider = ScriptedProvider([lambda m: ("tools", [("list_files", {})])])  # would loop forever
    h = harness_factory(provider=provider, settings_overrides={"task_max_steps": 4})
    with h:
        h.login()
        _, task_id = start_chat(h)
        task = finished(h, task_id)
        assert task["status"] == "succeeded"
        assert "limit" in task["status_detail"].lower()
        assert provider.calls <= 4
        assert provider.allow_tools_log[-1] is False  # last call had tools disabled
        assert any("Approaching the step limit" in e["summary"] for e in task["events"])


def test_task_budget_stops_spending(harness_factory):
    provider = ScriptedProvider([lambda m: ("tools", [("list_files", {})])], cost_per_call=0.40)
    h = harness_factory(provider=provider, settings_overrides={"task_max_cost_usd": 1.0, "task_max_steps": 50})
    with h:
        h.login()
        _, task_id = start_chat(h)
        task = finished(h, task_id)
        assert task["cost_usd"] <= 1.0 + 0.40  # at most one wrap-up call past the soft limit
        assert provider.allow_tools_log[-1] is False
        assert "budget" in (task["status_detail"] or "").lower() or "budget" in str(task["events"]).lower()


def test_daily_budget_blocks_new_spending(harness_factory):
    provider = ScriptedProvider([lambda m: ("text", "should not run")])
    h = harness_factory(provider=provider, settings_overrides={"daily_max_cost_usd": 1.0})
    with h:
        h.login()
        today = local_now(h.settings.timezone).date().isoformat()
        h.db.insert("spend", {"task_id": None, "ts": now_iso(), "day": today, "provider": "x", "model": "x", "cost_usd": 1.5})
        _, task_id = start_chat(h)
        task = finished(h, task_id)
        assert task["status"] == "failed" and "daily budget" in task["error"]
        assert provider.calls == 0


def test_search_limit_is_enforced_in_tools(harness_factory):
    provider = ScriptedProvider([lambda m: ("tools", [("web_search", {"query": "ai hackathon 2026"})])] * 3 + [lambda m: ("text", "done")])
    h = harness_factory(provider=provider, settings_overrides={"task_max_searches": 2})
    with h:
        h.login()
        _, task_id = start_chat(h)
        task = finished(h, task_id)
        assert task["searches"] == 2 and len(h.search.queries) == 2
        assert any(e["type"] == "tool_error" and "Search limit" in e["summary"] for e in task["events"])


def test_invalid_tool_input_is_reported_to_the_model(harness_factory):
    seen = {}

    def second(messages):
        seen["result"] = [m for m in messages if m.get("role") == "tool"][-1]
        return ("text", "ok")

    provider = ScriptedProvider([lambda m: ("tools", [("web_search", {"query": "x", "unexpected": True})]), second])
    h = harness_factory(provider=provider)
    with h:
        h.login()
        _, task_id = start_chat(h)
        assert finished(h, task_id)["status"] == "succeeded"
        assert seen["result"]["is_error"] and "Invalid tool input" in seen["result"]["content"]
        assert h.search.queries == []


def test_model_config_error_is_clear_and_nothing_is_faked(harness_factory, tmp_path):
    def factory(name):
        raise ModelConfigError("ANTHROPIC_API_KEY is not set.")

    h = harness_factory()
    with h:
        h.services._provider_factory = factory
        h.services._providers.clear()
        h.login()
        conv = h.post("/api/conversations", json={}).json()
        r = h.post(f"/api/conversations/{conv['id']}/messages", json={"content": "hi"})
        assert r.status_code == 400 and "ANTHROPIC_API_KEY" in r.json()["detail"]


def test_provider_failure_fails_task_without_fallback(harness_factory):
    class Broken(ScriptedProvider):
        async def complete(self, **kw):
            self.calls += 1
            raise ModelError("Ollama is not reachable at http://127.0.0.1:11434.", retryable=False)

    provider = Broken([lambda m: ("text", "x")])
    h = harness_factory(provider=provider)
    with h:
        h.login()
        _, task_id = start_chat(h)
        task = finished(h, task_id)
        assert task["status"] == "failed" and "not reachable" in task["error"]
        assert provider.calls == 1


def test_local_only_never_builds_a_cloud_provider(tmp_path):
    from conftest import make_settings

    with pytest.raises(LocalModeViolation):
        build_provider(make_settings(tmp_path, local_only=True, anthropic_api_key="sk-ant-x"), "anthropic")
    with pytest.raises(LocalModeViolation):
        build_provider(make_settings(tmp_path, local_only=True, ollama_base_url="https://ollama.example.com"), "ollama")
    with pytest.raises(LocalModeViolation):
        build_provider(make_settings(tmp_path, local_only=True, ollama_model="gpt-oss:120b-cloud"), "ollama")
    with pytest.raises(LocalModeViolation):
        build_provider(make_settings(tmp_path, local_only=True, openai_base_url="https://api.deepseek.com/v1", openai_model="deepseek-chat"), "openai_compatible")
    ok = build_provider(make_settings(tmp_path, local_only=True, openai_base_url="http://127.0.0.1:1234/v1", openai_model="local"), "openai_compatible")
    assert ok.is_local
    tailnet = build_provider(make_settings(tmp_path, local_only=True, ollama_base_url="http://100.101.102.103:11434"), "ollama")
    assert tailnet.is_local


def test_restart_resumes_from_checkpoint_without_repeating_side_effects(tmp_path):
    """Process 1 creates a draft, then is stopped mid-task. Process 2 resumes the task:
    the completed draft_email call is not executed again and the model continues."""
    gate = {"block": True}

    async def blocking(messages):
        while gate["block"]:
            await asyncio.sleep(0.05)
        return ("text", "Finished after restart")

    class Provider(ScriptedProvider):
        async def complete(self, *, system, messages, tools, allow_tools):
            if self.calls == 2:  # third model call blocks until the "process" is stopped
                self.calls += 1
                while gate["block"]:
                    await asyncio.sleep(0.05)
            return await super().complete(system=system, messages=messages, tools=tools, allow_tools=allow_tools)

    steps = [
        lambda m: ("tools", [("draft_email", {"to": [], "subject": "Hello", "body": "Draft body"})]),
        lambda m: ("tools", [("list_files", {})]),
        lambda m: ("text", "unused"),
        lambda m: ("text", "Finished after restart"),
    ]
    first = Harness(tmp_path, provider=Provider(steps))
    with first:
        first.login()
        _, task_id = start_chat(first, "Draft an email")
        wait_for(lambda: first.provider.calls >= 3)
        assert len(first.get("/api/drafts").json()["items"]) == 1
    # first process is gone; the task was interrupted while running
    row = first.db.one("SELECT status, checkpoint FROM tasks WHERE id = ?", (task_id,))
    assert row["status"] == "running" and row["checkpoint"]
    gate["block"] = False

    second_provider = ScriptedProvider([lambda m: ("text", "Finished after restart")])
    second = Harness(tmp_path, provider=second_provider)
    with second:
        second.login()
        task = finished(second, task_id)
        assert task["status"] == "succeeded" and task["result_text"] == "Finished after restart"
        assert any("Resumed" in e["summary"] or "Interrupted" in e["summary"] for e in task["events"])
        assert len(second.get("/api/drafts").json()["items"]) == 1  # not duplicated
        assert second_provider.calls == 1  # resumed at the right step


def test_pending_tool_call_with_completed_ledger_is_not_rerun(harness_factory):
    """A crash after a tool ran but before its result was checkpointed: the ledger answers."""
    provider = ScriptedProvider([lambda m: ("text", "done")])
    h = harness_factory(provider=provider, start_background=False)
    with h:
        h.login()
        task_id = create_task(h.db, kind="chat", title="t", prompt="p")
        state = {
            "v": 1, "provider": "scripted", "model": "m", "system": "s", "tools": ["draft_email"],
            "messages": [{"role": "user", "content": "p"},
                         {"role": "assistant", "content": "", "tool_calls": [{"id": "call_x", "name": "draft_email"}]}],
            "pending": [{"id": "call_x", "name": "draft_email", "input": {"subject": "S", "body": "B"}, "parse_error": None}],
            "steps": 1, "tool_calls": 1, "searches": 0, "fetches": 0, "input_tokens": 0, "output_tokens": 0,
            "total_tokens": 0, "cost": 0.0, "cost_known": True, "active_seconds": 0.0, "wrap_up": False,
            "wrap_up_turns": 0, "new_opportunities": [],
        }  # fmt: skip
        h.db.execute("UPDATE tasks SET status = 'running', checkpoint = ?, provider = 'scripted' WHERE id = ?", (dumps(state), task_id))
        # the draft was created and the ledger completed before the crash
        from home_agent.services.drafts import create_draft

        create_draft(h.db, subject="S", body="B", task_id=task_id, call_id="call_x")
        h.db.execute("INSERT INTO tool_calls (task_id, call_id, name, input, status, output, started_at) VALUES (?, 'call_x', 'draft_email', '{}', 'completed', 'Created draft #1', ?)",
                     (task_id, now_iso()))  # fmt: skip
        from home_agent.worker import Worker

        async def run():
            worker = Worker(h.services, 1)
            worker.recover()
            worker.start()
            for _ in range(200):
                if get_task(h.db, task_id)["status"] == "succeeded":
                    break
                await asyncio.sleep(0.05)
            await worker.stop()

        asyncio.run(run())
        assert get_task(h.db, task_id)["status"] == "succeeded"
        assert h.db.scalar("SELECT COUNT(*) FROM drafts") == 1


def test_password_constant_is_used():
    assert len(PASSWORD) >= 10
