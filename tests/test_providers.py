"""Model adapters against MOCKED HTTP endpoints (the real Anthropic SDK runs, the network does not)."""

from __future__ import annotations

import asyncio
import json

import anthropic
import httpx2
import pytest
from conftest import make_settings

from home_agent.llm.anthropic_provider import AnthropicProvider
from home_agent.llm.base import LocalModeViolation, ModelConfigError, ToolResult, ToolSpec
from home_agent.llm.local_providers import OllamaProvider, OpenAICompatProvider

TOOLS = [ToolSpec("web_search", "Search the web", {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]})]


def sse(events: list[dict]) -> bytes:
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()


def anthropic_stream(blocks: list[dict], stop_reason: str, model: str = "claude-opus-5-5", usage=None, stop_details=None) -> bytes:
    events = [{"type": "message_start", "message": {"id": "msg_1", "type": "message", "role": "assistant", "model": model,
                                                     "content": [], "stop_reason": None, "stop_sequence": None,
                                                     "usage": {"input_tokens": 1200, "output_tokens": 1,
                                                               "cache_creation_input_tokens": 3000, "cache_read_input_tokens": 9000}}}]  # fmt: skip
    for index, block in enumerate(blocks):
        if block["type"] == "tool_use":
            events.append({"type": "content_block_start", "index": index, "content_block": {**block, "input": {}}})
            events.append({"type": "content_block_delta", "index": index,
                           "delta": {"type": "input_json_delta", "partial_json": json.dumps(block["input"])}})  # fmt: skip
        elif block["type"] == "text":
            events.append({"type": "content_block_start", "index": index, "content_block": {"type": "text", "text": ""}})
            events.append({"type": "content_block_delta", "index": index, "delta": {"type": "text_delta", "text": block["text"]}})
        elif block["type"] == "thinking":
            events.append({"type": "content_block_start", "index": index, "content_block": {"type": "thinking", "thinking": "", "signature": ""}})
            events.append({"type": "content_block_delta", "index": index, "delta": {"type": "signature_delta", "signature": block["signature"]}})
        else:
            events.append({"type": "content_block_start", "index": index, "content_block": block})
        events.append({"type": "content_block_stop", "index": index})
    delta = {"type": "message_delta", "delta": {"stop_reason": stop_reason, "stop_sequence": None}, "usage": usage or {"output_tokens": 500}}
    if stop_details:
        delta["delta"]["stop_details"] = stop_details
    events += [delta, {"type": "message_stop"}]
    return sse(events)


def anthropic_provider(tmp_path, handler, **settings):
    client = anthropic.AsyncAnthropic(api_key="sk-ant-test", max_retries=0,
                                      http_client=anthropic.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(handler)))  # fmt: skip
    return AnthropicProvider(make_settings(tmp_path, anthropic_api_key="sk-ant-test", **settings), client=client)


def test_anthropic_request_shape_and_tool_call(tmp_path):
    captured = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        captured["body"] = json.loads(request.content)
        captured["beta"] = request.headers.get("anthropic-beta", "")
        body = anthropic_stream([{"type": "thinking", "signature": "sig-abc"},
                                 {"type": "tool_use", "id": "toolu_1", "name": "web_search", "input": {"query": "ai hackathon 2026"}}],
                                "tool_use")  # fmt: skip
        return httpx2.Response(200, content=body, headers={"content-type": "text/event-stream"})

    provider = anthropic_provider(tmp_path, handler, anthropic_effort="high")
    messages = provider.initial_messages([("user", "earlier"), ("assistant", "earlier answer")], "Find hackathons")
    turn = asyncio.run(provider.complete(system="SYSTEM", messages=messages, tools=TOOLS, allow_tools=True))

    body = captured["body"]
    assert body["model"] == "claude-opus-5-5"
    assert body["output_config"] == {"effort": "high"}
    assert body["thinking"]["type"] == "adaptive" and "budget_tokens" not in body["thinking"]
    assert body["thinking"]["block_binding"] == {"prefix_mismatch_behavior": "drop_block"}
    assert body["tool_choice"] == {"type": "auto"}
    assert body["fallbacks"] == "default"
    assert body["cache_control"] == {"type": "ephemeral"}
    assert body["stream"] is True
    assert "temperature" not in body
    assert "server-side-fallback-2026-07-01" in captured["beta"] and "thinking-binding-controls-2026-08-01" in captured["beta"]
    assert [m["role"] for m in body["messages"]] == ["user", "assistant", "user"]

    assert turn.stop_reason == "tool_use"
    assert turn.tool_calls[0].name == "web_search" and turn.tool_calls[0].input == {"query": "ai hackathon 2026"}
    # thinking block is replayed verbatim (signature kept) but never shown as text
    assert turn.assistant_message["content"][0] == {"type": "thinking", "thinking": "", "signature": "sig-abc"}
    assert turn.text == ""
    # cost: 1200 in * $4 + 500 out * $20 + 3000 cache write * $5 + 9000 cache read * $0.20 per MTok
    assert turn.usage.cost_usd == pytest.approx((1200 * 4 + 500 * 20 + 3000 * 5 + 9000 * 0.2) / 1e6)

    results = provider.tool_results_messages([ToolResult("toolu_1", "web_search", "results...", False)], "note")
    assert results[0]["content"][0]["tool_use_id"] == "toolu_1" and results[0]["content"][-1] == {"type": "text", "text": "note"}


def test_anthropic_wrap_up_turn_disables_tools_without_changing_tool_list(tmp_path):
    captured = {}

    def handler(request):
        captured["body"] = json.loads(request.content)
        return httpx2.Response(200, content=anthropic_stream([{"type": "text", "text": "Final"}], "end_turn"),
                               headers={"content-type": "text/event-stream"})  # fmt: skip

    provider = anthropic_provider(tmp_path, handler)
    turn = asyncio.run(provider.complete(system="S", messages=provider.initial_messages([], "x"), tools=TOOLS, allow_tools=False))
    assert captured["body"]["tool_choice"] == {"type": "none"}
    assert captured["body"]["tools"][0]["name"] == "web_search"  # same tools: keeps the cache/thinking prefix valid
    assert turn.text == "Final" and turn.stop_reason == "end_turn"


def test_anthropic_fallback_echo_rule(tmp_path):
    blocks = [
        {"type": "text", "text": "Partial before decline."},
        {"type": "thinking", "signature": "declined-sig"},
        {"type": "tool_use", "id": "toolu_declined", "name": "web_search", "input": {"query": "x"}},
        {"type": "fallback", "from": {"model": "claude-opus-5-5"}, "to": {"model": "claude-opus-5"}},
        {"type": "thinking", "signature": "fallback-sig"},
        {"type": "tool_use", "id": "toolu_ok", "name": "web_search", "input": {"query": "y"}},
    ]

    def handler(request):
        return httpx2.Response(200, content=anthropic_stream(blocks, "tool_use", model="claude-opus-5"),
                               headers={"content-type": "text/event-stream"})  # fmt: skip

    provider = anthropic_provider(tmp_path, handler)
    turn = asyncio.run(provider.complete(system="S", messages=provider.initial_messages([], "x"), tools=TOOLS, allow_tools=True))
    types = [b["type"] for b in turn.assistant_message["content"]]
    assert types == ["text", "thinking", "tool_use"]
    assert [c.id for c in turn.tool_calls] == ["toolu_ok"]  # the declined attempt's call is never executed
    assert any("fallback model claude-opus-5" in n for n in turn.notes)
    assert turn.usage.cost_usd == pytest.approx((1200 * 5 + 500 * 25 + 3000 * 6.25 + 9000 * 0.5) / 1e6)  # priced as the serving model


def test_anthropic_refusal_is_reported(tmp_path):
    def handler(request):
        return httpx2.Response(200, content=anthropic_stream([], "refusal", stop_details={"type": "refusal", "category": "cyber", "explanation": None}),
                               headers={"content-type": "text/event-stream"})  # fmt: skip

    provider = anthropic_provider(tmp_path, handler)
    turn = asyncio.run(provider.complete(system="S", messages=provider.initial_messages([], "x"), tools=TOOLS, allow_tools=True))
    assert turn.stop_reason == "refusal" and "declined" in turn.refusal and turn.assistant_message is None


def test_anthropic_errors_are_actionable(tmp_path):
    def handler(request):
        return httpx2.Response(401, json={"type": "error", "error": {"type": "authentication_error", "message": "invalid x-api-key"}})

    provider = anthropic_provider(tmp_path, handler)
    with pytest.raises(ModelConfigError, match="subscription does not include API access"):
        asyncio.run(provider.complete(system="S", messages=provider.initial_messages([], "x"), tools=TOOLS, allow_tools=True))
    with pytest.raises(ModelConfigError, match="ANTHROPIC_API_KEY is not set"):
        AnthropicProvider(make_settings(tmp_path))


def test_ollama_native_api_sets_context_and_parses_tool_calls(tmp_path):
    captured = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/api/show":
            return httpx2.Response(200, json={"details": {"family": "qwen3"}})
        captured["body"] = json.loads(request.content)
        return httpx2.Response(200, json={
            "model": "qwen3:8b", "done": True, "done_reason": "stop", "prompt_eval_count": 900, "eval_count": 40,
            "message": {"role": "assistant", "content": "<think>private reasoning</think>", "thinking": "more private",
                        "tool_calls": [{"function": {"name": "web_search", "arguments": {"query": "robotics grants"}}}]},
        })  # fmt: skip

    provider = OllamaProvider(make_settings(tmp_path, ollama_num_ctx=32768), transport=httpx2.MockTransport(handler))
    turn = asyncio.run(provider.complete(system="S", messages=provider.initial_messages([], "x"), tools=TOOLS, allow_tools=True))
    body = captured["body"]
    assert body["options"]["num_ctx"] == 32768 and body["stream"] is False
    assert body["messages"][0] == {"role": "system", "content": "S"}
    assert body["tools"][0]["function"]["name"] == "web_search"
    assert turn.tool_calls[0].input == {"query": "robotics grants"}
    assert "private" not in turn.text and turn.usage.cost_usd == 0
    msgs = provider.tool_results_messages([ToolResult(turn.tool_calls[0].id, "web_search", "r", False)])
    assert msgs == [{"role": "tool", "tool_name": "web_search", "content": "r"}]


def test_ollama_cloud_model_is_refused_in_local_only_mode(tmp_path):
    def handler(request):
        if request.url.path == "/api/show":
            return httpx2.Response(200, json={"remote_host": "https://ollama.com:443", "remote_model": "gpt-oss:120b"})
        raise AssertionError("prompt must not be sent")

    provider = OllamaProvider(make_settings(tmp_path, local_only=True, ollama_model="gpt-oss-proxy"), transport=httpx2.MockTransport(handler))
    with pytest.raises(LocalModeViolation):
        asyncio.run(provider.complete(system="S", messages=provider.initial_messages([], "x"), tools=TOOLS, allow_tools=True))


def test_ollama_missing_model_message(tmp_path):
    def handler(request):
        if request.url.path == "/api/show":
            return httpx2.Response(404, json={"error": "model not found"})
        return httpx2.Response(404, json={"error": "model 'qwen3:8b' not found"})

    provider = OllamaProvider(make_settings(tmp_path), transport=httpx2.MockTransport(handler))
    with pytest.raises(ModelConfigError, match="ollama pull"):
        asyncio.run(provider.complete(system="S", messages=provider.initial_messages([], "x"), tools=TOOLS, allow_tools=True))


def test_openai_compatible_string_args_text_fallback_and_tool_choice_retry(tmp_path):
    bodies = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        bodies.append(body)
        if "tool_choice" in body:
            return httpx2.Response(400, json={"error": {"message": "tool_choice is not supported"}})
        if len(bodies) == 2:
            return httpx2.Response(200, json={"choices": [{"finish_reason": "tool_calls", "message": {"role": "assistant", "content": None,
                "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "web_search", "arguments": "{\"query\": \"q1\"}"}}]}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5}})  # fmt: skip
        return httpx2.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"role": "assistant",
            "content": "<tool_call>{\"name\": \"web_search\", \"arguments\": {\"query\": \"q2\"}}</tool_call>"}}]})  # fmt: skip

    provider = OpenAICompatProvider(make_settings(tmp_path, openai_base_url="http://127.0.0.1:1234/v1", openai_model="local-model"),
                                    transport=httpx2.MockTransport(handler))  # fmt: skip
    first = asyncio.run(provider.complete(system="S", messages=provider.initial_messages([], "x"), tools=TOOLS, allow_tools=True))
    assert first.tool_calls[0].input == {"query": "q1"} and "tool_choice" not in bodies[1]
    second = asyncio.run(provider.complete(system="S", messages=provider.initial_messages([], "x"), tools=TOOLS, allow_tools=True))
    assert second.tool_calls[0].name == "web_search" and second.tool_calls[0].input == {"query": "q2"}
    assert provider.is_local and first.usage.cost_usd == 0


def test_anthropic_older_model_gets_no_adaptive_or_fallback_fields(tmp_path):
    captured = {}

    def handler(request):
        captured["body"] = json.loads(request.content)
        return httpx2.Response(200, content=anthropic_stream([{"type": "text", "text": "ok"}], "end_turn", model="claude-haiku-4-5"),
                               headers={"content-type": "text/event-stream"})  # fmt: skip

    provider = anthropic_provider(tmp_path, handler, anthropic_model="claude-haiku-4-5")
    asyncio.run(provider.complete(system="S", messages=provider.initial_messages([], "x"), tools=TOOLS, allow_tools=True))
    body = captured["body"]
    assert "thinking" not in body and "output_config" not in body and "fallbacks" not in body


def test_anthropic_retries_without_rejected_optional_feature(tmp_path):
    bodies = []

    def handler(request):
        body = json.loads(request.content)
        bodies.append(body)
        if "fallbacks" in body:
            return httpx2.Response(400, json={"type": "error", "error": {"type": "invalid_request_error",
                                                                          "message": "fallbacks: not available for this organization"}})  # fmt: skip
        return httpx2.Response(200, content=anthropic_stream([{"type": "text", "text": "ok"}], "end_turn"),
                               headers={"content-type": "text/event-stream"})  # fmt: skip

    provider = anthropic_provider(tmp_path, handler)
    turn = asyncio.run(provider.complete(system="S", messages=provider.initial_messages([], "x"), tools=TOOLS, allow_tools=True))
    assert turn.text == "ok" and len(bodies) == 2 and "fallbacks" not in bodies[1]
    assert bodies[1]["thinking"]["type"] == "adaptive"  # everything else kept
    asyncio.run(provider.complete(system="S", messages=provider.initial_messages([], "x"), tools=TOOLS, allow_tools=True))
    assert len(bodies) == 3  # remembered: no second failing attempt
