"""Local (or self-chosen) model endpoints: Ollama's native API and OpenAI-compatible servers.

Ollama is called through its native /api/chat endpoint because that is where the
context window (`num_ctx`) can be set per request; Ollama's OpenAI-compatible endpoint
ignores it and Ollama defaults to a 4k context on machines with < 24 GB of GPU memory,
which is far too small for an agent that reads web pages.
"""

from __future__ import annotations

import json
import re
import uuid

import httpx2

from ..config import Settings, is_local_endpoint
from .base import (
    LocalModeViolation,
    ModelConfigError,
    ModelError,
    ModelProvider,
    ModelTurn,
    ToolCall,
    ToolResult,
    ToolSpec,
    Usage,
    strip_think,
)
from .pricing import apply_cost, override_price

TOOL_CALL_TAG = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.S)


def _parse_arguments(value) -> tuple[dict | None, str | None]:
    if isinstance(value, dict):
        return value, None
    if value is None or value == "":
        return {}, None
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            return None, f"arguments were not valid JSON ({exc.msg})"
        if isinstance(parsed, dict):
            return parsed, None
        return None, "arguments were not a JSON object"
    return None, "arguments had an unexpected type"


def _calls_from_text(text: str, tool_names: set[str]) -> list[tuple[str, dict]]:
    """Recover tool calls that a local model wrote as text instead of structured calls."""
    found = []
    for match in TOOL_CALL_TAG.finditer(text or ""):
        try:
            data = json.loads(match.group(1))
        except json.JSONDecodeError:
            continue
        name = data.get("name")
        args = data.get("arguments", data.get("parameters", {}))
        if name in tool_names and isinstance(args, dict):
            found.append((name, args))
    if found:
        return found
    stripped = (text or "").strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        try:
            data = json.loads(stripped)
        except json.JSONDecodeError:
            return []
        name = data.get("name")
        args = data.get("arguments", data.get("parameters"))
        if name in tool_names and isinstance(args, dict):
            return [(name, args)]
    return []


def is_ollama_cloud_model(name: str) -> bool:
    tag = name.split(":", 1)[1] if ":" in name else ""
    return tag == "cloud" or tag.endswith("-cloud") or name.endswith("-cloud")


class OllamaProvider(ModelProvider):
    name = "ollama"

    def __init__(self, settings: Settings, transport: httpx2.AsyncBaseTransport | None = None):
        self.base_url = settings.ollama_base_url.rstrip("/")
        self.model = settings.ollama_model
        self.num_ctx = settings.ollama_num_ctx
        self.think = settings.ollama_think
        self.keep_alive = settings.ollama_keep_alive
        self.local_only = settings.local_only
        self.is_local = is_local_endpoint(self.base_url) and not is_ollama_cloud_model(self.model)
        self._remote_checked = False
        self.client = httpx2.AsyncClient(
            base_url=self.base_url,
            timeout=httpx2.Timeout(settings.model_request_timeout, connect=10.0),
            trust_env=False,  # never route a local model endpoint through a proxy
            transport=transport,
        )

    def initial_messages(self, history: list[tuple[str, str]], user_text: str) -> list[dict]:
        return [{"role": role, "content": text} for role, text in history] + [{"role": "user", "content": user_text}]

    def tool_results_messages(self, results: list[ToolResult], note: str | None = None) -> list[dict]:
        messages = [{"role": "tool", "tool_name": r.name, "content": r.content} for r in results]
        if note:
            messages.append({"role": "user", "content": note})
        return messages

    async def _ensure_local_model(self) -> None:
        """Ollama can proxy 'cloud' models; refuse them when local mode is required."""
        if self._remote_checked:
            return
        try:
            response = await self.client.post("/api/show", json={"model": self.model})
        except httpx2.HTTPError:
            return  # the chat call will report connectivity problems
        if response.status_code == 200:
            info = response.json()
            if info.get("remote_host") or info.get("remote_model"):
                self.is_local = False
                if self.local_only:
                    raise LocalModeViolation(
                        f"Ollama model '{self.model}' runs on a remote host ({info.get('remote_host') or 'Ollama cloud'}). "
                        "LOCAL_ONLY=true forbids sending prompts off this machine; choose a locally installed model."
                    )
        self._remote_checked = True

    async def complete(self, *, system: str, messages: list[dict], tools: list[ToolSpec], allow_tools: bool) -> ModelTurn:
        await self._ensure_local_model()
        body: dict = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, *messages],
            "stream": False,
            "options": {"num_ctx": self.num_ctx},
            "keep_alive": self.keep_alive,
        }
        if allow_tools and tools:
            body["tools"] = [
                {"type": "function", "function": {"name": t.name, "description": t.description, "parameters": t.input_schema}}
                for t in tools
            ]
        if self.think != "default":
            body["think"] = self.think == "on"
        try:
            response = await self.client.post("/api/chat", json=body)
        except httpx2.ConnectError:
            raise ModelError(
                f"Ollama is not reachable at {self.base_url}. Start the Ollama app (or `ollama serve`).", retryable=True
            ) from None
        except httpx2.TimeoutException:
            raise ModelError("The local model timed out.", retryable=True) from None
        except httpx2.HTTPError as exc:
            raise ModelError(f"Ollama request failed: {type(exc).__name__}", retryable=True) from None
        if response.status_code != 200:
            detail = _error_text(response)
            if response.status_code == 404:
                raise ModelConfigError(f"Ollama model '{self.model}' is not installed. Run: ollama pull {self.model}")
            if "does not support tools" in detail:
                raise ModelConfigError(f"Ollama model '{self.model}' does not support tool calling; pick a model with tools support.")
            raise ModelError(f"Ollama error ({response.status_code}): {detail}", retryable=response.status_code >= 500)
        data = response.json()
        message = data.get("message") or {}
        raw_content = message.get("content") or ""
        text = strip_think(raw_content)
        calls: list[ToolCall] = []
        for index, call in enumerate(message.get("tool_calls") or []):
            fn = call.get("function") or {}
            args, error = _parse_arguments(fn.get("arguments"))
            calls.append(ToolCall(id=call.get("id") or f"call_{uuid.uuid4().hex[:12]}_{index}", name=fn.get("name", ""),
                                  input=args, parse_error=error))  # fmt: skip
        assistant: dict = {"role": "assistant", "content": raw_content}
        if message.get("tool_calls"):
            assistant["tool_calls"] = message["tool_calls"]
        elif allow_tools:
            recovered = _calls_from_text(raw_content, {t.name for t in tools})
            if recovered:
                calls = [ToolCall(id=f"call_{uuid.uuid4().hex[:12]}_{i}", name=n, input=a) for i, (n, a) in enumerate(recovered)]
                assistant["tool_calls"] = [{"function": {"name": n, "arguments": a}} for n, a in recovered]
                text = ""
        usage = Usage(input_tokens=data.get("prompt_eval_count") or 0, output_tokens=data.get("eval_count") or 0,
                      model=self.model, cost_known=True)  # fmt: skip
        stop = "tool_use" if calls else ("max_tokens" if data.get("done_reason") == "length" else "end_turn")
        if not allow_tools:
            calls = []
            stop = "end_turn"
        return ModelTurn(assistant_message=assistant, text=text, tool_calls=calls, stop_reason=stop, usage=usage)

    async def check(self) -> str:
        await self._ensure_local_model()
        try:
            response = await self.client.get("/api/tags")
        except httpx2.HTTPError:
            raise ModelError(f"Ollama is not reachable at {self.base_url}.", retryable=True) from None
        if response.status_code != 200:
            raise ModelError(f"Ollama returned HTTP {response.status_code}")
        names = {m.get("name") for m in response.json().get("models", [])} | {
            m.get("model") for m in response.json().get("models", [])
        }
        if self.model not in names and f"{self.model}:latest" not in names:
            raise ModelConfigError(f"Ollama is running but model '{self.model}' is not installed. Run: ollama pull {self.model}")
        return f"Ollama is running; model {self.model} is installed (context {self.num_ctx} tokens)"

    def describe(self) -> dict:
        return {"provider": self.name, "model": self.model, "local": self.is_local, "endpoint": self.base_url,
                "num_ctx": self.num_ctx}  # fmt: skip

    async def aclose(self) -> None:
        await self.client.aclose()


class OpenAICompatProvider(ModelProvider):
    """Any server that implements OpenAI-style /chat/completions with tool calls.

    Local examples: LM Studio (http://127.0.0.1:1234/v1), llama.cpp `llama-server --jinja`
    (http://127.0.0.1:8080/v1), mlx-lm / vLLM servers. Can also point at a hosted service;
    then it is treated as a cloud provider.
    """

    name = "openai_compatible"

    def __init__(self, settings: Settings, transport: httpx2.AsyncBaseTransport | None = None):
        if not settings.openai_base_url or not settings.openai_model:
            raise ModelConfigError("Set OPENAI_COMPAT_BASE_URL and OPENAI_COMPAT_MODEL in .env to use this provider.")
        self.base_url = settings.openai_base_url.rstrip("/")
        self.model = settings.openai_model
        self.max_tokens = settings.openai_max_tokens
        self.is_local = is_local_endpoint(self.base_url)
        self.price = override_price(settings.model_input_price, settings.model_output_price)
        self._supports_tool_choice = True
        headers = {}
        if settings.openai_api_key:
            headers["Authorization"] = f"Bearer {settings.openai_api_key.get_secret_value()}"
        self.client = httpx2.AsyncClient(
            base_url=self.base_url,
            headers=headers,
            timeout=httpx2.Timeout(settings.model_request_timeout, connect=10.0),
            proxy=None if self.is_local else settings.outbound_proxy,
            trust_env=False,
            transport=transport,
        )

    def initial_messages(self, history: list[tuple[str, str]], user_text: str) -> list[dict]:
        return [{"role": role, "content": text} for role, text in history] + [{"role": "user", "content": user_text}]

    def tool_results_messages(self, results: list[ToolResult], note: str | None = None) -> list[dict]:
        messages = [{"role": "tool", "tool_call_id": r.call_id, "content": r.content} for r in results]
        if note:
            messages.append({"role": "user", "content": note})
        return messages

    async def complete(self, *, system: str, messages: list[dict], tools: list[ToolSpec], allow_tools: bool) -> ModelTurn:
        body: dict = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, *messages],
            "max_tokens": self.max_tokens,
            "stream": False,
        }
        if tools:
            body["tools"] = [
                {"type": "function", "function": {"name": t.name, "description": t.description, "parameters": t.input_schema}}
                for t in tools
            ]
            if self._supports_tool_choice:
                body["tool_choice"] = "auto" if allow_tools else "none"
            elif not allow_tools:
                body.pop("tools")
        response = await self._post(body)
        if response.status_code == 400 and "tool_choice" in response.text and "tool_choice" in body:
            self._supports_tool_choice = False
            body.pop("tool_choice")
            if not allow_tools:
                body.pop("tools", None)
            response = await self._post(body)
        if response.status_code in (401, 403):
            raise ModelConfigError(f"The model endpoint rejected the API key (HTTP {response.status_code}).")
        if response.status_code == 404:
            raise ModelConfigError(f"The model endpoint returned 404 for model '{self.model}'. Check OPENAI_COMPAT_* settings.")
        if response.status_code != 200:
            raise ModelError(f"Model endpoint error ({response.status_code}): {_error_text(response)}",
                             retryable=response.status_code == 429 or response.status_code >= 500)  # fmt: skip
        data = response.json()
        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        raw_content = message.get("content") or ""
        text = strip_think(raw_content)
        calls: list[ToolCall] = []
        for index, call in enumerate(message.get("tool_calls") or []):
            fn = call.get("function") or {}
            args, error = _parse_arguments(fn.get("arguments"))
            calls.append(ToolCall(id=call.get("id") or f"call_{uuid.uuid4().hex[:12]}_{index}", name=fn.get("name", ""),
                                  input=args, parse_error=error))  # fmt: skip
        assistant: dict = {"role": "assistant", "content": raw_content}
        if message.get("tool_calls"):
            assistant["tool_calls"] = [
                {**c, "id": calls[i].id, "type": c.get("type", "function")} for i, c in enumerate(message["tool_calls"])
            ]
        elif allow_tools:
            recovered = _calls_from_text(raw_content, {t.name for t in tools})
            if recovered:
                calls = [ToolCall(id=f"call_{uuid.uuid4().hex[:12]}_{i}", name=n, input=a) for i, (n, a) in enumerate(recovered)]
                assistant["tool_calls"] = [
                    {"id": c.id, "type": "function", "function": {"name": c.name, "arguments": json.dumps(c.input)}}
                    for c in calls
                ]
                text = ""
        raw_usage = data.get("usage") or {}
        usage = Usage(input_tokens=raw_usage.get("prompt_tokens") or 0, output_tokens=raw_usage.get("completion_tokens") or 0,
                      model=self.model)  # fmt: skip
        if self.price:
            apply_cost(usage, self.price)
        elif not self.is_local:
            usage.cost_known = False
        finish = choice.get("finish_reason")
        stop = "tool_use" if calls else ("max_tokens" if finish == "length" else "end_turn")
        if not allow_tools:
            calls = []
            stop = "end_turn"
        return ModelTurn(assistant_message=assistant, text=text, tool_calls=calls, stop_reason=stop, usage=usage)

    async def _post(self, body: dict) -> httpx2.Response:
        try:
            return await self.client.post("/chat/completions", json=body)
        except httpx2.ConnectError:
            raise ModelError(f"The model endpoint {self.base_url} is not reachable.", retryable=True) from None
        except httpx2.TimeoutException:
            raise ModelError("The model endpoint timed out.", retryable=True) from None
        except httpx2.HTTPError as exc:
            raise ModelError(f"Model request failed: {type(exc).__name__}", retryable=True) from None

    async def check(self) -> str:
        try:
            response = await self.client.get("/models")
        except httpx2.HTTPError:
            raise ModelError(f"The model endpoint {self.base_url} is not reachable.", retryable=True) from None
        if response.status_code in (401, 403):
            raise ModelConfigError("The model endpoint rejected the API key.")
        if response.status_code != 200:
            return f"Endpoint answered HTTP {response.status_code} on /models (model listing unsupported?)"
        return f"Endpoint reachable at {self.base_url}"

    def describe(self) -> dict:
        return {"provider": self.name, "model": self.model, "local": self.is_local, "endpoint": self.base_url}

    async def aclose(self) -> None:
        await self.client.aclose()


def _error_text(response: httpx2.Response) -> str:
    try:
        data = response.json()
        if isinstance(data, dict):
            error = data.get("error")
            if isinstance(error, dict):
                return str(error.get("message") or error)[:300]
            if error:
                return str(error)[:300]
    except ValueError:
        pass
    return response.text[:300]
