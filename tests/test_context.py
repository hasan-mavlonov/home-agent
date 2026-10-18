"""Fitting a task's messages into a local model's context window (MOCKED model servers)."""

from __future__ import annotations

import asyncio
import json

import httpx2
from conftest import make_settings

from home_agent.llm.base import ToolSpec
from home_agent.llm.context import REMOVED_NOTE, fit_messages, message_tokens
from home_agent.llm.local_providers import OllamaProvider, OpenAICompatProvider

PAGE = "Page text about a robotics hackathon. " * 400  # about 15,000 characters
REQUEST = "Find robotics hackathons for me."
TOOLS = [ToolSpec("fetch_page", "Read a page.", {"type": "object", "properties": {"url": {"type": "string"}}})]


def research_messages() -> list[dict]:
    def read(url):
        return [{"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "fetch_page", "arguments": {"url": url}}}]},
                {"role": "tool", "tool_name": "fetch_page", "content": f"URL: {url}\nTitle: {url[8]}\n{PAGE}"}]  # fmt: skip

    return [
        {"role": "user", "content": "Earlier question " + "h" * 3000},
        {"role": "assistant", "content": "Earlier answer " + "a" * 3000},
        {"role": "user", "content": REQUEST},
        *read("https://a.example/"),
        *read("https://b.example/"),
        *read("https://c.example/"),
    ]


def total(messages: list[dict]) -> int:
    return sum(message_tokens(m) for m in messages)


def test_messages_that_fit_are_untouched():
    messages = research_messages()
    assert fit_messages(messages, total(messages)) is messages


def test_oldest_tool_results_are_shortened_first_and_the_request_is_kept():
    messages = research_messages()
    budget = total(messages) - 4000
    fitted = fit_messages(messages, budget)
    assert total(fitted) <= budget
    assert fitted[2]["content"] == REQUEST
    assert fitted[4]["content"].startswith("URL: https://a.example/\nTitle: a") and fitted[4]["content"].endswith(REMOVED_NOTE)
    assert fitted[6] == messages[6] and fitted[8] == messages[8]  # newer pages and earlier turns untouched
    assert fitted[0] == messages[0] and fitted[1] == messages[1]
    assert messages[4]["content"].endswith(PAGE)  # the task's own record is not modified


def test_earlier_turns_and_then_the_newest_result_are_shortened_when_needed():
    messages = research_messages()
    fitted = fit_messages(messages, 3000)
    assert total(fitted) <= 3000
    assert fitted[2]["content"] == REQUEST
    assert fitted[4]["content"].endswith(REMOVED_NOTE) and fitted[6]["content"].endswith(REMOVED_NOTE)
    assert fitted[0]["content"].endswith("[...]") and len(fitted[0]["content"]) < 400
    newest = fitted[8]["content"]
    assert newest.startswith("URL: https://c.example/") and newest.endswith(REMOVED_NOTE)
    assert len(messages[8]["content"]) > len(newest) > 1500


def test_an_impossible_budget_still_keeps_the_request_and_part_of_the_newest_result():
    fitted = fit_messages(research_messages(), 100)
    assert fitted[2]["content"] == REQUEST
    assert len(fitted[8]["content"]) >= 1500


def test_ollama_request_fits_num_ctx(tmp_path):
    captured = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/api/show":
            return httpx2.Response(200, json={})
        captured["body"] = json.loads(request.content)
        return httpx2.Response(200, json={"done": True, "message": {"role": "assistant", "content": "Done."}})

    provider = OllamaProvider(make_settings(tmp_path, ollama_num_ctx=8192), transport=httpx2.MockTransport(handler))
    messages = research_messages()
    asyncio.run(provider.complete(system="S", messages=messages, tools=TOOLS, allow_tools=True))
    sent = captured["body"]["messages"][1:]  # after the system message
    assert total(sent) <= 8192 - 2048
    assert sent[2]["content"] == REQUEST
    assert sent[4]["content"].endswith(REMOVED_NOTE)
    assert sent[8] == messages[8]  # the page just read is complete


def test_openai_compatible_fits_only_when_a_context_size_is_set(tmp_path):
    bodies = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        bodies.append(json.loads(request.content))
        return httpx2.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": "ok"}}]})

    for context_tokens, shortened in ((None, False), (8192, True)):
        settings = make_settings(tmp_path, openai_base_url="http://127.0.0.1:1234/v1", openai_model="m", openai_max_tokens=2048,
                                 openai_context_tokens=context_tokens)  # fmt: skip
        provider = OpenAICompatProvider(settings, transport=httpx2.MockTransport(handler))
        asyncio.run(provider.complete(system="S", messages=research_messages(), tools=TOOLS, allow_tools=True))
        sent = bodies[-1]["messages"][1:]
        assert sent[4]["content"].endswith(REMOVED_NOTE) is shortened
        assert sent[2]["content"] == REQUEST
