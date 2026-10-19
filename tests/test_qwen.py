"""Qwen on Alibaba Cloud Model Studio. MOCKED: Model Studio's OpenAI-compatible streaming
API is simulated with an HTTP mock transport; no request leaves the machine. The response
shapes follow Model Studio's documented OpenAI-compatible streaming format."""

from __future__ import annotations

import asyncio
import json

import httpx2
import pytest
from conftest import make_settings
from helpers import wait_for

from home_agent.llm import build_provider
from home_agent.llm.base import LocalModeViolation, ModelConfigError, ToolSpec
from home_agent.llm.pricing import CONSERVATIVE
from home_agent.llm.qwen_provider import REGION_URLS, QwenProvider, _resolved_regions
from home_agent.search import SearchResult

TOOLS = [ToolSpec("web_search", "Search the web", {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]})]
INTL = REGION_URLS["intl"] + "/chat/completions"
CN = REGION_URLS["cn"] + "/chat/completions"


def stream(*chunks: dict, usage: dict | None = None) -> httpx2.Response:
    events = [{"choices": [c]} for c in chunks]
    if usage is not None:
        events.append({"choices": [], "usage": usage})
    body = "".join(f"data: {json.dumps(e)}\n\n" for e in events) + "data: [DONE]\n\n"
    return httpx2.Response(200, content=body.encode(), headers={"content-type": "text/event-stream"})


def text_reply(text: str) -> httpx2.Response:
    return stream({"delta": {"content": text}}, {"delta": {}, "finish_reason": "stop"}, usage={"prompt_tokens": 50, "completion_tokens": 5})


def qwen(tmp_path, handler, **overrides) -> QwenProvider:
    settings = make_settings(tmp_path, model_provider="qwen", qwen_api_key=overrides.pop("key", "sk-test-qwen-key-1"), **overrides)
    return QwenProvider(settings, transport=httpx2.MockTransport(handler))


@pytest.fixture(autouse=True)
def _forget_regions():
    _resolved_regions.clear()
    yield
    _resolved_regions.clear()


def run(coro):
    return asyncio.run(coro)


def test_streamed_tool_call_is_assembled_reasoning_dropped_and_cost_charged(tmp_path):
    requests = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return stream(
            {"delta": {"reasoning_content": "private reasoning that must not be kept"}},
            {"delta": {"content": "Let me search."}},
            {"delta": {"tool_calls": [{"index": 0, "id": "call_abc", "type": "function", "function": {"name": "web_search", "arguments": ""}}]}},
            {"delta": {"tool_calls": [{"index": 0, "function": {"arguments": '{"query": "AI hack'}}]}},
            {"delta": {"tool_calls": [{"index": 0, "function": {"arguments": 'athon 2026"}'}}]}},
            {"delta": {}, "finish_reason": "tool_calls"},
            usage={"prompt_tokens": 1000, "completion_tokens": 100},
        )  # fmt: skip

    provider = qwen(tmp_path, handler)
    turn = run(provider.complete(system="S", messages=provider.initial_messages([], "find"), tools=TOOLS, allow_tools=True))
    request = requests[0]
    body = json.loads(request.content)
    assert str(request.url) == INTL and request.headers["authorization"] == "Bearer sk-test-qwen-key-1"
    assert body["model"] == "qwen-plus" and body["stream"] is True and body["stream_options"] == {"include_usage": True}
    assert body["enable_thinking"] is False and body["parallel_tool_calls"] is True and body["tools"][0]["function"]["name"] == "web_search"
    assert body["messages"][0] == {"role": "system", "content": "S"}
    assert [(c.id, c.name, c.input) for c in turn.tool_calls] == [("call_abc", "web_search", {"query": "AI hackathon 2026"})]
    assert turn.stop_reason == "tool_use" and "private reasoning" not in json.dumps(turn.assistant_message)
    assert turn.usage.cost_known and turn.usage.cost_usd == pytest.approx((1000 * 0.40 + 100 * 1.20) / 1_000_000)
    # the tool result goes back in OpenAI format, tied to the call id
    follow_up = provider.tool_results_messages([type("R", (), {"call_id": "call_abc", "name": "web_search", "content": "results", "is_error": False})()])
    assert follow_up == [{"role": "tool", "tool_call_id": "call_abc", "content": "results"}]


def test_tool_name_repeated_in_every_chunk_is_not_doubled(tmp_path):
    def handler(request):
        return stream(
            {"delta": {"tool_calls": [{"index": 0, "id": "c1", "function": {"name": "web_search", "arguments": '{"query":'}}]}},
            {"delta": {"tool_calls": [{"index": 0, "id": "c1", "function": {"name": "web_search", "arguments": ' "x"}'}}]}},
            {"delta": {}, "finish_reason": "tool_calls"}, usage={"prompt_tokens": 10, "completion_tokens": 5},
        )  # fmt: skip

    provider = qwen(tmp_path, handler)
    turn = run(provider.complete(system="S", messages=provider.initial_messages([], "q"), tools=TOOLS, allow_tools=True))
    assert [(c.name, c.input) for c in turn.tool_calls] == [("web_search", {"query": "x"})]


def test_auto_region_falls_back_to_beijing_and_remembers_it(tmp_path):
    urls = []

    def handler(request):
        urls.append(str(request.url))
        if str(request.url) == INTL:
            return httpx2.Response(401, json={"error": {"message": "Incorrect API key provided.", "code": "invalid_api_key"}})
        return text_reply("ok")

    provider = qwen(tmp_path, handler, key="sk-beijing-key-123")
    turn = run(provider.complete(system="S", messages=provider.initial_messages([], "hi"), tools=[], allow_tools=False))
    assert turn.text == "ok" and urls == [INTL, CN]
    again = qwen(tmp_path, handler, key="sk-beijing-key-123")  # a new provider (e.g. after a settings change)
    run(again.complete(system="S", messages=again.initial_messages([], "hi"), tools=[], allow_tools=False))
    assert urls[2:] == [CN]
    assert "mainland China (Beijing)" in run(again.check())


def test_rejected_key_names_every_region_tried(tmp_path):
    def handler(request):
        return httpx2.Response(401, json={"error": {"message": "Incorrect API key provided."}})

    provider = qwen(tmp_path, handler)
    with pytest.raises(ModelConfigError) as err:
        run(provider.complete(system="S", messages=provider.initial_messages([], "hi"), tools=[], allow_tools=False))
    assert "international (Singapore) and mainland China (Beijing)" in str(err.value)
    assert "region where it was created" in str(err.value) and "sk-test" not in str(err.value)
    cn_only = qwen(tmp_path, handler, qwen_region="cn")
    with pytest.raises(ModelConfigError, match=r"on the mainland China \(Beijing\) endpoint"):
        run(cn_only.complete(system="S", messages=cn_only.initial_messages([], "hi"), tools=[], allow_tools=False))


def test_model_that_cannot_turn_thinking_off_is_retried_and_priced_as_thinking(tmp_path):
    bodies = []

    def handler(request):
        body = json.loads(request.content)
        bodies.append(body)
        if body.get("enable_thinking") is False:
            return httpx2.Response(400, json={"error": {"message": "The value of the enable_thinking parameter is restricted to True.",
                                                        "code": "InvalidParameter"}})  # fmt: skip
        return stream({"delta": {"reasoning_content": "thinking..."}}, {"delta": {"content": "done"}}, {"delta": {}, "finish_reason": "stop"},
                      usage={"prompt_tokens": 1000, "completion_tokens": 1000})  # fmt: skip

    provider = qwen(tmp_path, handler)
    turn = run(provider.complete(system="S", messages=provider.initial_messages([], "hi"), tools=[], allow_tools=False))
    assert turn.text == "done" and len(bodies) == 2 and "enable_thinking" not in bodies[1]
    assert turn.usage.cost_usd == pytest.approx((1000 * 0.40 + 1000 * 4.0) / 1_000_000)  # qwen-plus thinking output rate
    run(provider.complete(system="S", messages=provider.initial_messages([], "hi"), tools=[], allow_tools=False))
    assert len(bodies) == 3 and "enable_thinking" not in bodies[2]  # not sent again
    assert provider.describe()["thinking"] is False


def test_unknown_model_is_capped_at_the_highest_rate_and_missing_usage_is_estimated(tmp_path):
    def handler(request):
        return stream({"delta": {"content": "an answer"}}, {"delta": {}, "finish_reason": "stop"})  # no usage chunk

    provider = qwen(tmp_path, handler, qwen_model="qwen-some-future-model")
    turn = run(provider.complete(system="S", messages=provider.initial_messages([], "x" * 4000), tools=[], allow_tools=False))
    assert provider.price == CONSERVATIVE and not provider.describe()["price_known"]
    assert turn.usage.input_tokens > 500 and turn.usage.cost_known and turn.usage.cost_usd > 0
    assert "most expensive rate" in run(provider.check())
    priced = qwen(tmp_path, handler, qwen_model="qwen-some-future-model", model_input_price=1.0, model_output_price=2.0)
    assert priced.price[:2] == (1.0, 2.0) and priced.describe()["price_known"]


def test_errors_are_actionable(tmp_path):
    def unknown_model(request):
        return httpx2.Response(404, json={"error": {"message": "The model `qwen-nope` does not exist.", "code": "model_not_found"}})

    provider = qwen(tmp_path, unknown_model, qwen_model="qwen-nope")
    with pytest.raises(ModelConfigError, match="QWEN_MODEL"):
        run(provider.complete(system="S", messages=provider.initial_messages([], "hi"), tools=[], allow_tools=False))

    def arrears(request):
        return httpx2.Response(403, json={"error": {"message": "Access denied, please make sure your account is in good standing.",
                                                    "code": "Arrearage"}})  # fmt: skip

    provider = qwen(tmp_path, arrears)
    with pytest.raises(ModelConfigError, match="good standing"):
        run(provider.complete(system="S", messages=provider.initial_messages([], "hi"), tools=[], allow_tools=False))

    def busy(request):
        return httpx2.Response(429, json={"error": {"message": "Requests rate limit exceeded"}})

    provider = qwen(tmp_path, busy)
    with pytest.raises(Exception) as err:
        run(provider.complete(system="S", messages=provider.initial_messages([], "hi"), tools=[], allow_tools=False))
    assert getattr(err.value, "retryable", False) and "rate limit" in str(err.value)


def test_configuration(tmp_path, monkeypatch):
    with pytest.raises(ModelConfigError, match="QWEN_API_KEY"):
        build_provider(make_settings(tmp_path, model_provider="qwen"))
    with pytest.raises(LocalModeViolation):
        build_provider(make_settings(tmp_path, model_provider="qwen", qwen_api_key="sk-x-123456", local_only=True))
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-from-dashscope-var")  # Alibaba's own variable name also works
    settings = make_settings(tmp_path, model_provider="qwen")
    assert settings.qwen_api_key.get_secret_value() == "sk-from-dashscope-var"
    assert "sk-from-dashscope-var" in settings.secret_values()  # scrubbed from logs and errors
    explicit = QwenProvider(make_settings(tmp_path, qwen_api_key="sk-x-123456", qwen_base_url="https://example.test/compatible-mode/v1/"))
    assert explicit.base_url == "https://example.test/compatible-mode/v1" and explicit.regions == []
    from home_agent.config import Settings

    assert Settings.model_fields["model_provider"].default == "qwen"
    env = tmp_path / "blank.env"
    env.write_text("QWEN_API_KEY=sk-x-123456\nQWEN_MODEL=\nQWEN_REGION=\nQWEN_THINKING=\nQWEN_MAX_TOKENS=\nQWEN_CONTEXT_TOKENS=\n")
    blank = Settings(_env_file=str(env))  # blank lines in .env fall back to the defaults instead of failing
    assert (blank.qwen_model, blank.qwen_region, blank.qwen_thinking, blank.qwen_context_tokens) == ("qwen-plus", "auto", False, 128_000)


def test_agent_loop_runs_on_qwen(harness_factory, tmp_path):
    """The real agent loop, tools and storage on the Qwen adapter (Model Studio itself is mocked)."""
    requests = []

    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            return stream({"delta": {"tool_calls": [{"index": 0, "id": "call_1", "type": "function",
                                                     "function": {"name": "web_search", "arguments": '{"query": "robotics hackathon 2026"}'}}]}},
                          {"delta": {}, "finish_reason": "tool_calls"}, usage={"prompt_tokens": 3000, "completion_tokens": 40})  # fmt: skip
        return text_reply("I found one lead: [Robotics Hackathon](https://robo.example.org/2026).")

    settings = make_settings(tmp_path, model_provider="qwen", qwen_api_key="sk-agent-loop-key")
    provider = QwenProvider(settings, transport=httpx2.MockTransport(handler))
    h = harness_factory(provider=provider, settings_overrides={"model_provider": "qwen", "qwen_api_key": "sk-agent-loop-key"},
                        search_results=[SearchResult(title="Robotics Hackathon 2026", url="https://robo.example.org/2026", snippet="x")])  # fmt: skip
    with h:
        h.login()
        conversation = h.post("/api/conversations", json={}).json()
        task_id = h.post(f"/api/conversations/{conversation['id']}/messages", json={"content": "Find a robotics hackathon."}).json()["task"]["id"]
        task = wait_for(lambda: (t := h.get(f"/api/tasks/{task_id}").json())["status"] in ("succeeded", "failed") and t)
    assert task["status"] == "succeeded", task
    assert "Robotics Hackathon" in task["result_text"] and task["searches"] == 1
    second = requests[1]["messages"]
    assistant = next(m for m in second if m["role"] == "assistant")
    tool = next(m for m in second if m["role"] == "tool")
    assert assistant["tool_calls"][0]["id"] == "call_1" and tool["tool_call_id"] == "call_1"
    assert "Robotics Hackathon 2026" in tool["content"]
    assert task["cost_usd"] == pytest.approx((3000 * 0.40 + 40 * 1.20 + 50 * 0.40 + 5 * 1.20) / 1_000_000, rel=1e-3)




def test_content_filter_rejection_removes_the_newest_page_and_reports_it(tmp_path):
    from home_agent.llm.qwen_provider import FILTERED_PAGE

    bodies = []

    def handler(request):
        body = json.loads(request.content)
        bodies.append(body)
        if any("BLOCKED-TEXT" in (m.get("content") or "") for m in body["messages"]):
            return httpx2.Response(400, json={"error": {"message": "Input data may contain inappropriate content.",
                                                        "code": "data_inspection_failed"}})  # fmt: skip
        return text_reply("answer without that page")

    provider = qwen(tmp_path, handler)
    history = provider.initial_messages([], "research")
    history += [{"role": "assistant", "content": "", "tool_calls": [
        {"id": "c1", "type": "function", "function": {"name": "web_search", "arguments": "{}"}},
        {"id": "c2", "type": "function", "function": {"name": "web_search", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "a normal page"},
        {"role": "tool", "tool_call_id": "c2", "content": "a page with BLOCKED-TEXT"}]  # fmt: skip
    turn = run(provider.complete(system="S", messages=history, tools=TOOLS, allow_tools=True))
    assert turn.text == "answer without that page" and len(bodies) == 2
    sent = {m.get("tool_call_id"): m["content"] for m in bodies[1]["messages"] if m["role"] == "tool"}
    assert sent == {"c1": "a normal page", "c2": FILTERED_PAGE}
    assert any("content filter" in n for n in turn.notes)
    assert history[-1]["content"] == "a page with BLOCKED-TEXT"  # the task's own record is untouched
    run(provider.complete(system="S", messages=history, tools=TOOLS, allow_tools=True))
    assert len(bodies) == 3  # later steps leave the page out straight away instead of being rejected again

    def always_blocked(request):
        return httpx2.Response(400, json={"error": {"message": "Input or output data may contain inappropriate content.",
                                                    "code": "data_inspection_failed"}})  # fmt: skip

    def output_blocked_mid_stream(request):
        body = "data: " + json.dumps({"error": {"message": "Output data may contain inappropriate content.",
                                                "code": "data_inspection_failed"}}) + "\n\n"  # fmt: skip
        return httpx2.Response(200, content=body.encode(), headers={"content-type": "text/event-stream"})

    midway = qwen(tmp_path, output_blocked_mid_stream)
    with pytest.raises(Exception, match="content filter rejected this request") as err:
        run(midway.complete(system="S", messages=history, tools=TOOLS, allow_tools=True))
    assert not getattr(err.value, "retryable", True)  # not retried as if it were a server error

    stubborn = qwen(tmp_path, always_blocked)
    with pytest.raises(Exception, match="content filter rejected this request") as err:
        run(stubborn.complete(system="S", messages=history, tools=TOOLS, allow_tools=True))
    assert not getattr(err.value, "retryable", True)
