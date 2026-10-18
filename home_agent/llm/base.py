"""Provider-neutral types for the agent loop.

Each provider keeps conversation history in its own wire format (so Claude's
thinking blocks, for example, are replayed exactly as received). The loop only sees
`ModelTurn`: visible text, tool calls, usage, and the native message to append.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

THINK_TAG = re.compile(r"<think>.*?(</think>|$)", re.S | re.I)


@dataclass
class ToolSpec:
    name: str
    description: str
    input_schema: dict


@dataclass
class ToolCall:
    id: str
    name: str
    input: dict | None
    parse_error: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "ToolCall":
        return cls(id=data["id"], name=data["name"], input=data.get("input"), parse_error=data.get("parse_error"))


@dataclass
class ToolResult:
    call_id: str
    name: str
    content: str
    is_error: bool = False


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float = 0.0
    cost_known: bool = True
    model: str = ""

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens + self.cache_read_tokens + self.cache_write_tokens


@dataclass
class ModelTurn:
    assistant_message: dict | None
    text: str
    tool_calls: list[ToolCall]
    stop_reason: str
    usage: Usage
    notes: list[str] = field(default_factory=list)
    refusal: str | None = None


class ModelError(Exception):
    def __init__(self, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


class ModelConfigError(ModelError):
    """Missing or invalid configuration; retrying will not help."""


class LocalModeViolation(ModelConfigError):
    """The configured endpoint would send prompts off this machine while local mode is required."""


def strip_think(text: str) -> str:
    return THINK_TAG.sub("", text or "").strip()


class ModelProvider:
    name: str = "base"
    model: str = ""
    is_local: bool = False

    def initial_messages(self, history: list[tuple[str, str]], user_text: str) -> list[dict]:
        raise NotImplementedError

    async def complete(self, *, system: str, messages: list[dict], tools: list[ToolSpec], allow_tools: bool) -> ModelTurn:
        raise NotImplementedError

    def tool_results_messages(self, results: list[ToolResult], note: str | None = None) -> list[dict]:
        raise NotImplementedError

    async def check(self) -> str:
        """Cheap connectivity/config check. Returns a short status line or raises ModelError."""
        raise NotImplementedError

    def describe(self) -> dict[str, Any]:
        return {"provider": self.name, "model": self.model, "local": self.is_local}

    async def aclose(self) -> None:
        return None
