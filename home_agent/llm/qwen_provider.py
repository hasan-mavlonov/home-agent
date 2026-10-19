"""Qwen through Alibaba Cloud Model Studio (DashScope), using its OpenAI-compatible API.

What this adds on top of the plain OpenAI-compatible adapter:

- Region. An API key works only in the region where it was created. With QWEN_REGION=auto
  the international (Singapore) endpoint is tried first and then mainland China (Beijing);
  the one that accepts the key is remembered for the rest of the process.
- Thinking. Requests are streamed, because Model Studio rejects non-streaming calls to a
  model that is thinking. Thinking is off unless QWEN_THINKING=true. Models that cannot turn
  it off get the request again without the switch. Reasoning text is discarded, never shown.
- Optional request fields that a model rejects (enable_thinking, parallel_tool_calls) are
  dropped and the request is sent again.
- Cost. Prices come from QWEN_PRICES so the per-task and daily caps apply. If a stream ends
  without a usage report, tokens are estimated rather than treated as free.
- Content filter. Model Studio rejects requests whose text its filter flags
  (data_inspection_failed). Web pages are the usual cause, so the newest page the model has
  not yet lost is replaced with a note and the request is sent again, up to
  FILTER_RETRIES times; the removal is reported in the task's activity log.
"""

from __future__ import annotations

import hashlib
import json

import httpx2

from ..config import Settings
from .base import ModelConfigError, ModelError, ModelTurn, ToolSpec, Usage
from .context import estimate_tokens
from .local_providers import OpenAICompatProvider, _error_text
from .pricing import QWEN_LONG_INPUT, QWEN_LONG_INPUT_FACTOR, apply_cost, override_price, qwen_price

REGION_URLS = {
    "intl": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
    "cn": "https://dashscope.aliyuncs.com/compatible-mode/v1",
}
REGION_NAMES = {"intl": "international (Singapore)", "cn": "mainland China (Beijing)"}
OPTIONAL_FIELDS = ("enable_thinking", "parallel_tool_calls")
FILTER_RETRIES = 3
FILTERED_PAGE = ("[Removed: Alibaba Cloud's content filter rejected requests containing this tool result, so its text is "
                 "not available. Use other sources.]")  # fmt: skip
_resolved_regions: dict[str, str] = {}  # key fingerprint -> region that accepted it


class _Unreachable(ModelError):
    """The endpoint could not be connected to (as opposed to answering with an error or timing out)."""


class _Filtered(ModelError):
    """Model Studio's content filter rejected the request."""


class QwenProvider(OpenAICompatProvider):
    name = "qwen"
    settings_hint = "QWEN_MODEL setting (model IDs are listed in the Model Studio console)"

    def __init__(self, settings: Settings, transport: httpx2.AsyncBaseTransport | None = None):
        if settings.qwen_api_key is None or not settings.qwen_api_key.get_secret_value().strip():
            raise ModelConfigError(
                "Set QWEN_API_KEY in .env to use Qwen. Create a key in the Alibaba Cloud Model Studio console "
                "(international: modelstudio.console.alibabacloud.com; mainland China: bailian.console.aliyun.com)."
            )
        key = settings.qwen_api_key.get_secret_value().strip()
        self.model = settings.qwen_model.strip() or "qwen-plus"
        self.max_tokens = settings.qwen_max_tokens
        self.context_tokens = settings.qwen_context_tokens
        self.thinking = settings.qwen_thinking
        self.is_local = False
        self._supports_tool_choice = True
        self._dropped: set[str] = set()
        self._filtered: set[str] = set()  # tool_call_ids whose result text the content filter rejected
        self._override = override_price(settings.model_input_price, settings.model_output_price)
        self._set_price(self.thinking)
        self._key_id = hashlib.sha256(key.encode()).hexdigest()[:16]
        if settings.qwen_base_url:
            self.regions: list[str] = []
            self.base_url = settings.qwen_base_url.rstrip("/")
        else:
            regions = ["intl", "cn"] if settings.qwen_region == "auto" else [settings.qwen_region]
            known = _resolved_regions.get(self._key_id)
            self.regions = [known] if known in regions else regions
            self.base_url = REGION_URLS[self.regions[0]]
        self._region_index = 0
        self.client = httpx2.AsyncClient(
            headers={"Authorization": f"Bearer {key}"},
            timeout=httpx2.Timeout(settings.model_request_timeout, connect=15.0),
            proxy=settings.outbound_proxy,
            trust_env=False,
            transport=transport,
        )

    # -- pricing -----------------------------------------------------------------------------
    def _set_price(self, thinking: bool) -> None:
        price, self.price_known = qwen_price(self.model, thinking)
        self.price = self._override or price

    def _charge(self, usage: Usage) -> None:
        price = self.price
        if usage.input_tokens > QWEN_LONG_INPUT:
            price = tuple(p * QWEN_LONG_INPUT_FACTOR for p in price)  # type: ignore[assignment]
        apply_cost(usage, price)

    # -- requests ----------------------------------------------------------------------------
    async def complete(self, *, system: str, messages: list[dict], tools: list[ToolSpec], allow_tools: bool) -> ModelTurn:
        notes = []
        for attempt in range(FILTER_RETRIES + 1):
            visible = [{**m, "content": FILTERED_PAGE} if m.get("role") == "tool" and m.get("tool_call_id") in self._filtered
                       else m for m in messages]  # fmt: skip
            try:
                turn = await super().complete(system=system, messages=visible, tools=tools, allow_tools=allow_tools)
            except _Filtered:
                newest = next((m for m in reversed(messages) if m.get("role") == "tool" and m.get("tool_call_id")
                               and m["tool_call_id"] not in self._filtered), None)  # fmt: skip
                if newest is None or attempt == FILTER_RETRIES:
                    raise ModelError(
                        "Alibaba Cloud's content filter rejected this request (data_inspection_failed), and removing "
                        "page text did not help. Rephrase the request, or use another model provider for it."
                    ) from None
                self._filtered.add(newest["tool_call_id"])
                notes.append("Alibaba Cloud's content filter rejected the text of a page the agent read; it was removed "
                             "from what the model sees")  # fmt: skip
                continue
            turn.notes.extend(notes)
            return turn
        raise AssertionError("unreachable")

    @property
    def region(self) -> str | None:
        return self.regions[self._region_index] if self.regions else None

    def _next_region(self) -> bool:
        if self._region_index + 1 < len(self.regions):
            self._region_index += 1
            self.base_url = REGION_URLS[self.regions[self._region_index]]
            return True
        return False

    async def _send(self, body: dict) -> tuple[int, dict | None, str]:
        body = {**body, "stream": True, "stream_options": {"include_usage": True}}
        optional = {"enable_thinking": self.thinking}
        if body.get("tools"):
            optional["parallel_tool_calls"] = True
        body.update({k: v for k, v in optional.items() if k not in self._dropped})
        status, data, error = 0, None, "no response"
        for _ in range(len(OPTIONAL_FIELDS) + len(self.regions) + 1):
            try:
                status, data, error = await self._stream(body)
            except _Unreachable:
                if self._next_region():
                    continue
                raise
            if status == 401 and self._next_region():
                continue
            if status != 200 and ("data_inspection_failed" in error or "inappropriate content" in error):
                raise _Filtered(error)  # also when the output was blocked mid-stream
            rejected = next((f for f in OPTIONAL_FIELDS if f in body and status == 400 and f in error), None)
            if rejected:
                self._dropped.add(rejected)
                body.pop(rejected)
                if rejected == "enable_thinking":
                    self._set_price(True)  # the model decides; assume it thinks
                continue
            if status == 200 and self.region:
                _resolved_regions[self._key_id] = self.region
            if status == 200 and data is not None and not data.get("usage"):
                prompt = estimate_tokens(json.dumps(body["messages"], ensure_ascii=False))
                message = data["choices"][0]["message"]
                reply = estimate_tokens((message.get("content") or "") + json.dumps(message.get("tool_calls") or []))
                data["usage"] = {"prompt_tokens": prompt, "completion_tokens": reply}
            return status, data, error
        return status, data, error

    async def _stream(self, body: dict) -> tuple[int, dict | None, str]:
        names = {t["function"]["name"] for t in body.get("tools") or []}
        try:
            async with self.client.stream("POST", f"{self.base_url}/chat/completions", json=body) as response:
                if response.status_code != 200:
                    await response.aread()
                    return response.status_code, None, _error_text(response)
                return await _collect(response, names)
        except httpx2.ConnectError:
            raise _Unreachable(f"The Qwen endpoint {self.base_url} is not reachable.", retryable=True) from None
        except httpx2.TimeoutException:
            raise ModelError("The Qwen endpoint timed out.", retryable=True) from None
        except httpx2.HTTPError as exc:
            raise ModelError(f"Qwen request failed: {type(exc).__name__}", retryable=True) from None

    def _auth_error(self, status: int, error: str = "") -> str:
        detail = f" ({error})" if error else ""
        if status == 403:
            return (f"Qwen refused the request (HTTP 403){detail}. Check that model '{self.model}' is available to your "
                    "Model Studio account and that the account is in good standing.")  # fmt: skip
        if not self.regions:
            return f"Qwen rejected QWEN_API_KEY at {self.base_url} (HTTP {status}){detail}."
        tried = " and ".join(REGION_NAMES[r] for r in self.regions[: self._region_index + 1])
        return (f"Qwen rejected QWEN_API_KEY on the {tried} endpoint (HTTP {status}){detail}. A key only works in the "
                "region where it was created; check the key in the Model Studio console.")  # fmt: skip

    async def check(self) -> str:
        status, _, error = await self._send(
            {"model": self.model, "messages": [{"role": "user", "content": "Reply with OK."}], "max_tokens": 16}
        )
        if status in (401, 403):
            raise ModelConfigError(self._auth_error(status, error))
        if status == 404 or "model_not_found" in error:
            raise ModelConfigError(f"Model Studio does not know model '{self.model}' ({error or 'HTTP 404'}). Check QWEN_MODEL.")
        if status != 200:
            raise ModelError(f"Qwen error ({status}): {error}", retryable=status == 429 or status >= 500)
        where = REGION_NAMES[self.region] if self.region else self.base_url
        price = "" if self.price_known or self._override else (
            "; its price is not in Home Agent's table, so cost caps assume the most expensive rate "
            "(set MODEL_INPUT_PRICE_PER_MTOK and MODEL_OUTPUT_PRICE_PER_MTOK)")  # fmt: skip
        return f"Qwen model {self.model} answered through the {where} endpoint{price}"

    def describe(self) -> dict:
        return {"provider": self.name, "model": self.model, "local": False, "endpoint": self.base_url,
                "thinking": self.thinking and "enable_thinking" not in self._dropped,
                "price_known": self.price_known or self._override is not None}  # fmt: skip


async def _collect(response: httpx2.Response, tool_names: set[str]) -> tuple[int, dict | None, str]:
    """Turn a streamed chat completion into the shape of a non-streamed one."""
    content: list[str] = []
    calls: dict[int, dict] = {}
    finish = None
    usage: dict = {}
    async for line in response.aiter_lines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if payload == "[DONE]":
            break
        try:
            chunk = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if chunk.get("error"):
            error = chunk["error"]
            if isinstance(error, dict):
                code = error.get("code") or error.get("type") or ""
                return 502, None, f"{error.get('message') or error} [{code}]"[:300]
            return 502, None, str(error)[:300]
        if chunk.get("usage"):
            usage = chunk["usage"]
        for choice in chunk.get("choices") or []:
            delta = choice.get("delta") or {}
            if delta.get("content"):
                content.append(delta["content"])  # delta["reasoning_content"] (thinking) is dropped on purpose
            for part in delta.get("tool_calls") or []:
                slot = calls.setdefault(part.get("index", len(calls)), {"id": None, "names": [], "args": []})
                slot["id"] = part.get("id") or slot["id"]
                fn = part.get("function") or {}
                if fn.get("name"):
                    slot["names"].append(fn["name"])
                if fn.get("arguments"):
                    slot["args"].append(fn["arguments"])
            if choice.get("finish_reason"):
                finish = choice["finish_reason"]
    tool_calls = []
    for index in sorted(calls):
        slot = calls[index]
        names = slot["names"] or [""]
        # some servers split the name across chunks, others repeat it whole in each chunk
        name = next((n for n in ("".join(names), names[0], names[-1]) if n in tool_names), "".join(names))
        arguments = "".join(slot["args"])
        if slot["args"] and not _is_json(arguments) and _is_json(slot["args"][-1]):
            arguments = slot["args"][-1]  # cumulative chunks: the last one is complete
        tool_calls.append({"id": slot["id"], "type": "function", "function": {"name": name, "arguments": arguments}})
    message: dict = {"role": "assistant", "content": "".join(content) or None}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return 200, {"choices": [{"finish_reason": finish, "message": message}], "usage": usage}, ""


def _is_json(text: str) -> bool:
    try:
        json.loads(text)
        return True
    except json.JSONDecodeError:
        return False
