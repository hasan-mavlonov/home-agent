"""Test doubles. Everything here is a MOCK: no real model, search engine, or Gmail."""

from __future__ import annotations

import asyncio
import json
import time
from typing import Callable

import httpx2

from home_agent.llm.base import ModelProvider, ModelTurn, ToolCall, ToolResult, ToolSpec, Usage
from home_agent.net.guard import NetGuard
from home_agent.search import SearchResult

PUBLIC_TEST_IP = "93.184.215.14"


def fake_resolver(mapping: dict[str, list[str]] | None = None):
    mapping = mapping or {}

    async def resolve(host: str, port: int) -> list[str]:
        if host in mapping:
            return mapping[host]
        if host.endswith(".test") or host.endswith(".example.org"):
            return [PUBLIC_TEST_IP]
        raise OSError(f"unknown host {host}")

    return resolve


def make_guard(mapping=None) -> NetGuard:
    return NetGuard(resolver=fake_resolver(mapping))


class FakeSearch:
    name = "FakeSearch (mock)"

    def __init__(self, results: list[SearchResult] | None = None):
        self.results = results or []
        self.queries: list[str] = []

    async def search(self, query: str, max_results: int = 8, freshness=None):
        self.queries.append(query)
        return self.results[:max_results]


def pages_transport(pages: dict[str, tuple[int, str, str]], hits: list[str] | None = None) -> httpx2.MockTransport:
    """pages: url -> (status, content_type, body). Redirects: body 'REDIRECT <url>'."""

    def handler(request: httpx2.Request) -> httpx2.Response:
        url = str(request.url)
        if hits is not None:
            hits.append(url)
        if url not in pages:
            return httpx2.Response(404, text="not found", headers={"content-type": "text/plain"})
        status, ctype, body = pages[url]
        if body.startswith("REDIRECT "):
            return httpx2.Response(status, headers={"location": body.split(" ", 1)[1]})
        return httpx2.Response(status, content=body.encode(), headers={"content-type": ctype})

    return httpx2.MockTransport(handler)


Step = Callable[[list[dict]], tuple[str, object]]


class ScriptedProvider(ModelProvider):
    """A fake model that follows a script. Each step is a function of the history
    returning ("tools", [(name, input), ...]) or ("text", "final answer")."""

    name = "scripted"
    model = "scripted-test-model"
    is_local = True

    def __init__(self, steps: list[Step], delay: float = 0.0, cost_per_call: float = 0.0):
        self.steps = steps
        self.delay = delay
        self.cost_per_call = cost_per_call
        self.calls = 0
        self.allow_tools_log: list[bool] = []

    def initial_messages(self, history, user_text):
        return [{"role": r, "content": t} for r, t in history] + [{"role": "user", "content": user_text}]

    def tool_results_messages(self, results: list[ToolResult], note=None):
        out = [{"role": "tool", "tool_call_id": r.call_id, "name": r.name, "content": r.content, "is_error": r.is_error}
               for r in results]  # fmt: skip
        if note:
            out.append({"role": "user", "content": note})
        return out

    async def complete(self, *, system, messages, tools: list[ToolSpec], allow_tools: bool) -> ModelTurn:
        self.allow_tools_log.append(allow_tools)
        if self.delay:
            await asyncio.sleep(self.delay)
        index = min(self.calls, len(self.steps) - 1)
        self.calls += 1
        kind, payload = self.steps[index](messages)
        usage = Usage(input_tokens=1000, output_tokens=100, cost_usd=self.cost_per_call, model=self.model)
        if kind == "tools" and allow_tools:
            calls = [ToolCall(id=f"call_{self.calls}_{i}_{int(time.time()*1000)%100000}", name=n, input=a)
                     for i, (n, a) in enumerate(payload)]  # fmt: skip
            message = {"role": "assistant", "content": "", "tool_calls": [c.to_dict() for c in calls]}
            return ModelTurn(assistant_message=message, text="", tool_calls=calls, stop_reason="tool_use", usage=usage)
        text = payload if kind == "text" else "Final answer written without more tools."
        return ModelTurn(assistant_message={"role": "assistant", "content": text}, text=text, tool_calls=[],
                         stop_reason="end_turn", usage=usage)  # fmt: skip

    async def check(self):
        return "scripted provider"


def last_tool_results(messages: list[dict]) -> list[dict]:
    out = []
    for message in reversed(messages):
        if message.get("role") == "tool":
            out.append(message)
        elif out:
            break
    return list(reversed(out))


def tool_json(messages: list[dict]) -> list[str]:
    return [m["content"] for m in last_tool_results(messages)]


def wait_for(predicate, timeout: float = 15.0, interval: float = 0.05):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    raise AssertionError("condition not met in time")


__all__ = ["json"]
