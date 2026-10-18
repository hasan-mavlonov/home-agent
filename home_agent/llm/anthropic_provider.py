"""Claude via the official Anthropic Python SDK (Messages API, streamed)."""

from __future__ import annotations

import logging

import anthropic

from ..config import Settings
from .base import ModelConfigError, ModelError, ModelProvider, ModelTurn, ToolCall, ToolResult, ToolSpec, Usage
from .pricing import anthropic_price, apply_cost, override_price

log = logging.getLogger(__name__)

THINKING_BINDING_BETA = "thinking-binding-controls-2026-08-01"
FALLBACK_BETA = "server-side-fallback-2026-07-01"
# Models that accept the server-side `fallbacks: "default"` refusal fallback.
FALLBACK_MODELS = ("claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5")
# Models with adaptive thinking and the effort control (older models take neither).
ADAPTIVE_MODELS = ("claude-fable-5", "claude-mythos-5", "claude-opus-5", "claude-opus-4-8", "claude-opus-4-7",
                   "claude-opus-4-6", "claude-sonnet-5", "claude-sonnet-4-6", "claude-haiku-5")  # fmt: skip
NO_KEY_MESSAGE = (
    "ANTHROPIC_API_KEY is not set. Create an API key at https://console.anthropic.com (API usage is billed "
    "separately; a Claude.ai or Claude Code subscription does not include API access), put it in .env, and "
    "restart Home Agent. Or switch MODEL_PROVIDER to a local model."
)


class AnthropicProvider(ModelProvider):
    name = "anthropic"
    is_local = False

    def __init__(self, settings: Settings, client: anthropic.AsyncAnthropic | None = None):
        self.settings = settings
        self.model = settings.anthropic_model
        self.effort = settings.anthropic_effort
        self.max_tokens = settings.anthropic_max_tokens
        self.fallback = settings.anthropic_refusal_fallback and self.model.startswith(FALLBACK_MODELS)
        self.adaptive = self.model.startswith(ADAPTIVE_MODELS)
        self.binding = self.adaptive
        self.price_override = override_price(settings.model_input_price, settings.model_output_price)
        if client is not None:
            self.client = client
            return
        if settings.anthropic_api_key is None:
            raise ModelConfigError(NO_KEY_MESSAGE)
        http_client = None
        if settings.outbound_proxy:
            http_client = anthropic.DefaultAsyncHttpxClient(proxy=settings.outbound_proxy)
        self.client = anthropic.AsyncAnthropic(
            api_key=settings.anthropic_api_key.get_secret_value(),
            max_retries=3,
            timeout=settings.model_request_timeout,
            http_client=http_client,
        )

    # -- history --------------------------------------------------------------------
    def initial_messages(self, history: list[tuple[str, str]], user_text: str) -> list[dict]:
        messages = [{"role": role, "content": [{"type": "text", "text": text}]} for role, text in history]
        messages.append({"role": "user", "content": [{"type": "text", "text": user_text}]})
        return messages

    def tool_results_messages(self, results: list[ToolResult], note: str | None = None) -> list[dict]:
        content: list[dict] = [
            {"type": "tool_result", "tool_use_id": r.call_id, "content": r.content, "is_error": r.is_error}
            for r in results
        ]
        if note:
            content.append({"type": "text", "text": note})
        return [{"role": "user", "content": content}]

    # -- calls ----------------------------------------------------------------------
    async def complete(self, *, system: str, messages: list[dict], tools: list[ToolSpec], allow_tools: bool) -> ModelTurn:
        try:
            message = await self._stream(system, messages, tools, allow_tools)
        except anthropic.BadRequestError as exc:
            # An account or model that does not accept an optional beta field should not
            # break every task: drop the optional extras once and retry.
            text = _short(exc).lower()
            optional = ("fallback" in text and self.fallback) or ("block_binding" in text and self.binding)
            if not optional:
                raise self._translate(exc) from None
            self.fallback = self.fallback and "fallback" not in text
            self.binding = self.binding and "block_binding" not in text
            log.warning("Anthropic rejected an optional feature; retrying without it: %s", _short(exc))
            try:
                message = await self._stream(system, messages, tools, allow_tools)
            except anthropic.APIError as retry_exc:
                raise self._translate(retry_exc) from None
        except anthropic.APIError as exc:
            raise self._translate(exc) from None
        return self._to_turn(message)

    async def _stream(self, system: str, messages: list[dict], tools: list[ToolSpec], allow_tools: bool):
        betas = []
        params: dict = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": [{"type": "text", "text": system}],
            "messages": messages,
            "tools": [{"name": t.name, "description": t.description, "input_schema": t.input_schema} for t in tools],
            "tool_choice": {"type": "auto" if allow_tools else "none"},
            "cache_control": {"type": "ephemeral"},
        }
        if self.adaptive:
            # Thinking is always on for current models; its text is never shown to the user.
            params["output_config"] = {"effort": self.effort}
            params["thinking"] = {"type": "adaptive"}
            if self.binding:
                # drop_block degrades gracefully instead of failing if history ever stops matching
                params["thinking"]["block_binding"] = {"prefix_mismatch_behavior": "drop_block"}
                betas.append(THINKING_BINDING_BETA)
        if self.fallback:
            params["fallbacks"] = "default"
            betas.append(FALLBACK_BETA)
        if betas:
            params["betas"] = betas
        async with self.client.beta.messages.stream(**params) as stream:
            return await stream.get_final_message()

    def _translate(self, exc: Exception) -> ModelError:
        try:
            raise exc
        except anthropic.AuthenticationError:
            return ModelConfigError(
                "Anthropic rejected the API key (401). Check ANTHROPIC_API_KEY in .env. A Claude.ai or Claude Code "
                "subscription does not include API access."
            )
        except anthropic.PermissionDeniedError as exc:
            return ModelConfigError(
                f"Anthropic refused the request (403): {_short(exc)}. The API may not be available in your region, "
                "or the key lacks access to this model."
            )
        except anthropic.NotFoundError:
            return ModelConfigError(f"Model '{self.model}' was not found (404). Check ANTHROPIC_MODEL.")
        except anthropic.RateLimitError:
            return ModelError("Anthropic rate limit reached (429).", retryable=True)
        except anthropic.BadRequestError as exc:
            text = _short(exc)
            if "credit balance" in text.lower():
                return ModelConfigError("Your Anthropic account has no remaining credit. Add credit in the Console.")
            return ModelError(f"Anthropic rejected the request (400): {text}")
        except anthropic.APITimeoutError:
            return ModelError("The Anthropic API timed out.", retryable=True)
        except anthropic.APIConnectionError:
            return ModelError(
                "Could not reach the Anthropic API (network error). If api.anthropic.com is blocked on your network, "
                "set OUTBOUND_PROXY in .env.",
                retryable=True,
            )
        except anthropic.APIStatusError as exc:
            return ModelError(f"Anthropic API error ({exc.status_code}): {_short(exc)}",
                              retryable=exc.status_code >= 500 or exc.status_code == 529)  # fmt: skip
        except Exception as exc:  # pragma: no cover - defensive
            return ModelError(f"Anthropic request failed: {type(exc).__name__}")

    def _to_turn(self, message) -> ModelTurn:
        blocks = [_clean_block(b.to_dict()) for b in message.content]
        blocks = _echo_blocks(blocks)
        notes = []
        if getattr(message, "model", self.model) != self.model:
            notes.append(f"Served by fallback model {message.model}")
        for item in getattr(message, "input_transformations", None) or []:
            kind = getattr(item, "type", None) or (item.get("type") if isinstance(item, dict) else None)
            if kind == "thinking_dropped":
                notes.append("Some earlier reasoning blocks were dropped by the API")
                break
        usage = self._usage(message)
        if message.stop_reason == "refusal":
            details = getattr(message, "stop_details", None)
            category = getattr(details, "category", None) if details is not None else None
            explanation = getattr(details, "explanation", None) if details is not None else None
            reason = "The model declined this request"
            if category:
                reason += f" (category: {category})"
            if explanation:
                reason += f": {explanation}"
            return ModelTurn(assistant_message=None, text="", tool_calls=[], stop_reason="refusal", usage=usage,
                             notes=notes, refusal=reason)  # fmt: skip
        text = "\n\n".join(b["text"] for b in blocks if b.get("type") == "text" and b.get("text")).strip()
        calls = [
            ToolCall(id=b["id"], name=b["name"], input=b.get("input") if isinstance(b.get("input"), dict) else None,
                     parse_error=None if isinstance(b.get("input"), dict) else "tool input was not a JSON object")  # fmt: skip
            for b in blocks
            if b.get("type") == "tool_use"
        ]
        assistant = {"role": "assistant", "content": blocks} if blocks else None
        stop = message.stop_reason or "end_turn"
        if calls and stop != "max_tokens":
            stop = "tool_use"
        return ModelTurn(assistant_message=assistant, text=text, tool_calls=calls, stop_reason=stop, usage=usage, notes=notes)

    def _usage(self, message) -> Usage:
        total = Usage(model=getattr(message, "model", self.model))
        raw = message.usage
        iterations = [it for it in (getattr(raw, "iterations", None) or []) if getattr(it, "type", "") in ("message", "fallback_message")]
        entries = iterations or [raw]
        for entry in entries:
            model = getattr(entry, "model", None) or (self.model if iterations else total.model)
            part = Usage(
                input_tokens=getattr(entry, "input_tokens", 0) or 0,
                output_tokens=getattr(entry, "output_tokens", 0) or 0,
                cache_read_tokens=getattr(entry, "cache_read_input_tokens", 0) or 0,
                cache_write_tokens=getattr(entry, "cache_creation_input_tokens", 0) or 0,
            )
            if self.price_override:
                price, known = self.price_override, True
            else:
                price, known = anthropic_price(str(model))
            apply_cost(part, price)
            total.input_tokens += part.input_tokens
            total.output_tokens += part.output_tokens
            total.cache_read_tokens += part.cache_read_tokens
            total.cache_write_tokens += part.cache_write_tokens
            total.cost_usd += part.cost_usd
            total.cost_known = total.cost_known and known
        return total

    async def check(self) -> str:
        try:
            info = await self.client.models.retrieve(self.model)
        except anthropic.AuthenticationError:
            raise ModelConfigError("Anthropic rejected the API key (401).") from None
        except anthropic.NotFoundError:
            raise ModelConfigError(f"Model '{self.model}' was not found.") from None
        except anthropic.APIConnectionError:
            raise ModelError("Could not reach the Anthropic API (network error).", retryable=True) from None
        except anthropic.APIStatusError as exc:
            raise ModelError(f"Anthropic API error ({exc.status_code}): {_short(exc)}") from None
        return f"Connected; model {getattr(info, 'display_name', None) or self.model} is available"

    def describe(self) -> dict:
        return {"provider": self.name, "model": self.model, "local": False,
                "effort": self.effort if self.adaptive else None, "refusal_fallback": self.fallback}  # fmt: skip

    async def aclose(self) -> None:
        await self.client.close()


def _short(exc: Exception) -> str:
    message = getattr(exc, "message", None) or str(exc)
    return str(message)[:300]


def _clean_block(block: dict) -> dict:
    return {k: v for k, v in block.items() if v is not None}


def _echo_blocks(blocks: list[dict]) -> list[dict]:
    """Apply the documented echo rule after a server-side fallback.

    Before the last `fallback` marker, only text blocks are kept (thinking and tool_use
    from the declined attempt must not be replayed or executed); the marker itself is
    dropped; everything after it is kept as-is.
    """
    boundary = max((i for i, b in enumerate(blocks) if b.get("type") == "fallback"), default=-1)
    if boundary < 0:
        return blocks
    kept = [b for b in blocks[:boundary] if b.get("type") == "text"]
    kept.extend(blocks[boundary + 1 :])
    return kept
