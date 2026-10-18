"""Keep a task's conversation inside a small context window (local models).

When the estimated prompt is too large, older tool results (mostly page text) are replaced by
their first few hundred characters and a note, oldest first. The task's request and the newest
tool results are kept as long as possible. This only changes what is sent to the model: the full
page text stays in the task's records, so quote checks still use what was actually read.

Without this, Ollama silently drops the oldest messages, which can include the request itself.
"""

from __future__ import annotations

import json
import logging

log = logging.getLogger(__name__)

REMOVED_NOTE = "\n[... rest removed to fit the model's context window. Call the tool again if you need it.]"
KEEP_HEAD = 400  # characters kept from a shortened tool result (its URL and title)
KEEP_HISTORY = 300  # characters kept from an earlier conversation message
MIN_LATEST = 1500  # newest tool results are never cut below this


def estimate_tokens(text: str) -> int:
    """Deliberately high estimate: about 3.2 characters per token, one token per non-ASCII character."""
    wide = len(text) - len(text.encode("ascii", "ignore"))
    return int((len(text) - wide) / 3.2) + wide + 1


def message_tokens(message: dict) -> int:
    return estimate_tokens(json.dumps(message, ensure_ascii=False)) + 4


def _shorten(message: dict, keep: int, note: str) -> dict | None:
    content = message.get("content")
    if not isinstance(content, str) or len(content) <= keep + len(note):
        return None
    return {**message, "content": content[:keep].rstrip() + note}


def fit_messages(messages: list[dict], budget: int) -> list[dict]:
    """Return messages (a shortened copy if needed) whose estimated size is at most `budget` tokens.

    Order: older tool results, then earlier conversation turns, then the newest tool results
    (cut from the end). The task's request is never shortened.
    """
    sizes = [message_tokens(m) for m in messages]
    total = sum(sizes)
    if total <= budget:
        return messages
    out = list(messages)
    first_tool = next((i for i, m in enumerate(out) if m.get("role") == "tool"), len(out))
    request = max((i for i in range(first_tool) if out[i].get("role") == "user"), default=-1)
    last_assistant = max((i for i, m in enumerate(out) if m.get("role") == "assistant" and i > request), default=-1)

    def replace(i: int, new: dict | None) -> bool:
        nonlocal total
        if new is None:
            return False
        size = message_tokens(new)
        total += size - sizes[i]
        out[i], sizes[i] = new, size
        return True

    shortened = 0
    for i in range(request + 1, last_assistant):
        if total <= budget:
            break
        if out[i].get("role") == "tool":
            shortened += replace(i, _shorten(out[i], KEEP_HEAD, REMOVED_NOTE))
    for i in range(request):
        if total <= budget:
            break
        replace(i, _shorten(out[i], KEEP_HISTORY, " [...]"))
    latest = [i for i in range(max(last_assistant, request) + 1, len(out)) if out[i].get("role") == "tool"]
    while total > budget and latest:
        i = max(latest, key=lambda j: len(out[j].get("content") or ""))
        content = out[i].get("content") or ""
        keep = max(MIN_LATEST, len(content) // 2)
        if len(content) <= keep + len(REMOVED_NOTE):
            break
        replace(i, _shorten(out[i], keep, REMOVED_NOTE))
    if shortened:
        log.info("Shortened %d earlier tool results to fit the context window (~%d of %d tokens)", shortened, total, budget)
    if total > budget:
        log.warning("Prompt is still about %d tokens for a budget of %d; the model server may truncate it", total, budget)
    return out
