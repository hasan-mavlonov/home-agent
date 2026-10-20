"""The bounded tool-calling loop for one task, checkpointed after every step.

Durability model:
* After each model response the assistant message and its pending tool calls are
  written to the task's checkpoint *before* any tool runs.
* Each tool call is recorded in the `tool_calls` ledger keyed by (task, call id); a
  completed call is never executed again, and side-effecting tools are idempotent on
  that key, so resuming after a crash cannot duplicate drafts, records or schedules.
* Limits (steps, tool calls, searches, page reads, active time, tokens, task and daily
  spend) are checked every step. Near a limit the model gets one tool-free turn to
  write its answer from what it has; at a hard limit the loop stops.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass

from ..db import dumps, loads, now_iso, utcnow
from ..llm.base import LocalModeViolation, ModelConfigError, ModelError, ModelTurn, ToolCall, ToolResult, ToolSpec
from ..services import profile as profile_service
from ..services.sources import list_task_sources
from ..services.tasks import add_event, get_task, notify, record_spend, spend_today
from ..util import normalize_url, scrub, truncate, urls_in
from .prompts import build_system_prompt, first_turn_text
from .tools import TOOLS_BY_NAME, ToolContext, ToolOutput, tools_for, validate_input

log = logging.getLogger(__name__)

TOOL_TIMEOUT = 150.0
READ_CONCURRENCY = 3
MAX_HISTORY_MESSAGES = 10
MAX_HISTORY_CHARS = 6000

# A reply that promises work ("I'll create the script...") or reports work as done ("I've saved
# report.csv") while no tool has run in the task. Matching only makes the loop ask the model,
# once, to act with its tools; plain answers to questions are left alone.
PROMISES_ACTION = re.compile(
    r"\b(I'?ll|I will|I am going to|I'm going to|let me|now I'll|I can now|one moment|please wait|stand by)\b"
    r"|我将|我会|让我|请稍等",
    re.I,
)
CLAIMS_ACTION = re.compile(
    r"\bI(?:'ve| have)? (?:just )?(?:created|written|wrote|saved|run|ran|executed|searched|fetched|downloaded|generated|"
    r"drafted|scheduled|checked|verified|tested|opened|read)\b"
    r"|\b(?:has|have) been (?:created|saved|written|executed|generated|run|downloaded)\b"
    r"|\bexit (?:code|status) (?:was |is |of )?\d"
    r"|已(?:经)?(?:创建|生成|运行|执行|保存|写入)",
    re.I,
)
CLAIMS_SCRIPT_RUN = re.compile(
    r"\b(?:I(?:'ve| have)? (?:run|ran|executed) (?:the|your|a|it)|exit (?:code|status)|the (?:script|program) "
    r"(?:printed|output|ran|produced))\b",
    re.I,
)
ACTION_NUDGE = (
    "[Home Agent] You replied without calling any tool, but your reply describes doing work. Nothing has been "
    "executed in this task yet. If the request needs action, do it now with your tools (for example write_file, then "
    "run_script) and report what actually happened. If no tool can do it, say so plainly. If it needs no action, "
    "repeat your answer without promising or claiming actions."
)
NO_TOOLS_NOTICE = ("\n\n> **Home Agent:** no tools ran during this task, so nothing described above was actually "
                   "executed, created or checked.")  # fmt: skip
NO_SCRIPT_NOTICE = ("\n\n> **Home Agent:** no script ran successfully during this task, so any script output or exit "
                    "status described above was not produced by this app.")  # fmt: skip


@dataclass
class Limits:
    max_steps: int
    max_tool_calls: int
    max_searches: int
    max_fetches: int
    max_seconds: float
    max_tokens: int
    task_max_cost: float
    daily_max_cost: float

    @classmethod
    def from_services(cls, services) -> "Limits":
        s = services.settings
        return cls(
            max_steps=s.task_max_steps,
            max_tool_calls=s.task_max_tool_calls,
            max_searches=s.task_max_searches,
            max_fetches=s.task_max_fetches,
            max_seconds=float(s.task_max_seconds),
            max_tokens=s.task_max_tokens,
            task_max_cost=float(services.runtime("task_max_cost_usd")),
            daily_max_cost=float(services.runtime("daily_max_cost_usd")),
        )


class TaskStopped(Exception):
    pass


def _summarize_call(name: str, args: dict | None) -> str:
    args = args or {}
    if name == "web_search":
        return f'Searching: "{truncate(str(args.get("query", "")), 120)}"'
    if name == "fetch_page":
        suffix = " (rendering JavaScript)" if args.get("render_javascript") else ""
        start = args.get("start_char") or 0
        more = f" from char {start}" if start else ""
        return f"Reading {truncate(str(args.get('url', '')), 160)}{more}{suffix}"
    if name == "save_opportunity":
        return f"Saving opportunity: {truncate(str(args.get('name', '')), 100)}"
    if name == "list_saved_opportunities":
        return "Checking saved opportunities"
    if name == "draft_email":
        return f"Drafting email: {truncate(str(args.get('subject', '')), 100)}"
    if name == "remember_preference":
        return "Saving a note about your preferences"
    if name == "propose_schedule":
        return f"Proposing schedule: {truncate(str(args.get('name', '')), 80)}"
    if name in ("read_file", "write_file"):
        return f"{'Reading' if name == 'read_file' else 'Writing'} file {truncate(str(args.get('path', '')), 100)}"
    if name == "list_files":
        return "Listing workspace files"
    if name == "run_script":
        extra = " " + " ".join(str(a) for a in args.get("args") or []) if args.get("args") else ""
        return f"Running script {truncate(str(args.get('path', '')) + extra, 120)}"
    return f"Calling {name}"


class AgentRunner:
    def __init__(self, services, task_id: int):
        self.services = services
        self.db = services.db
        self.task_id = task_id
        self.task: dict = get_task(self.db, task_id) or {}
        self.tz = services.runtime("timezone")
        self.limits = Limits.from_services(services)
        self.state: dict = {}
        self.provider = None
        self.shutting_down = False  # set by the worker when the server stops; the task resumes later

    # -- persistence ------------------------------------------------------------------
    def _checkpoint(self) -> None:
        st = self.state
        self.db.execute(
            "UPDATE tasks SET checkpoint = ?, steps = ?, tool_calls = ?, searches = ?, fetches = ?, input_tokens = ?, "
            "output_tokens = ?, cost_usd = ?, new_opportunities = ?, updated_at = ? WHERE id = ?",
            (dumps(st), st["steps"], st["tool_calls"], st["searches"], st["fetches"], st["input_tokens"],
             st["output_tokens"], st["cost"], len(st["new_opportunities"]), now_iso(), self.task_id),  # fmt: skip
        )

    def _event(self, type_: str, summary: str, data: dict | None = None) -> None:
        add_event(self.db, self.task_id, type_, summary, data)

    def _history(self) -> list[tuple[str, str]]:
        conversation_id = self.task.get("conversation_id")
        if not conversation_id:
            return []
        rows = self.db.all(
            "SELECT role, content FROM messages WHERE conversation_id = ? AND id < ? AND role IN ('user', 'assistant') "
            "ORDER BY id DESC LIMIT ?",
            (conversation_id, self.task.get("user_message_id") or 1 << 62, MAX_HISTORY_MESSAGES),
        )
        rows.reverse()
        history = [(r["role"], truncate(r["content"], MAX_HISTORY_CHARS)) for r in rows]
        while history and history[0][0] != "user":
            history.pop(0)
        return history

    def _new_state(self) -> dict:
        profile_text = profile_service.render_profile(profile_service.get_profile(self.db))
        memories_text = profile_service.render_memories(profile_service.list_memories(self.db))
        now = utcnow()
        scripts = self.services.scripts.describe() if getattr(self.services, "scripts", None) else None
        system = build_system_prompt(profile_text, memories_text, self.tz, now, scripts)
        first = first_turn_text(self.task["prompt"], self.tz, now, self.task["kind"])
        return {
            "v": 1,
            "provider": self.provider.name,
            "model": self.provider.model,
            "system": system,
            "tools": [t.name for t in tools_for(self.task["kind"], self.services)],
            "messages": self.provider.initial_messages(self._history(), first),
            "pending": [],
            "steps": 0,
            "tool_calls": 0,
            "searches": 0,
            "fetches": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "cost": 0.0,
            "cost_known": True,
            "active_seconds": 0.0,
            "wrap_up": False,
            "wrap_up_turns": 0,
            "new_opportunities": [],
            "succeeded_tools": [],
            "action_nudges": 0,
        }

    # -- main --------------------------------------------------------------------------
    async def run(self) -> str:
        """Run until the task finishes. Returns the final status."""
        task = self.task
        if not task:
            return "failed"
        try:
            self.provider = self.services.get_provider(task.get("provider") or self.services.runtime("model_provider"))
        except (ModelConfigError, LocalModeViolation) as exc:
            return self._finish("failed", error=str(exc))
        existing = loads(task.get("checkpoint"))
        if existing and existing.get("provider") == self.provider.name:
            self.state = existing
            self._event("status", f"Resumed after restart (step {self.state['steps']})")
        else:
            self.state = self._new_state()
            self._event("status", f"Started with {self.provider.name} / {self.provider.model}",
                        {"provider": self.provider.describe()})  # fmt: skip
            self._checkpoint()
        try:
            return await self._loop()
        except asyncio.CancelledError:
            if not self.shutting_down:
                self._finish("cancelled", detail="Cancelled")
            raise
        except TaskStopped as exc:
            return self._finish("cancelled", detail=str(exc))
        except (ModelConfigError, LocalModeViolation) as exc:
            return self._finish("failed", error=str(exc))
        except ModelError as exc:
            return self._finish("failed", error=f"Model error: {exc}")
        except Exception as exc:  # unexpected bug: record it rather than crash the worker
            log.exception("task %s crashed", self.task_id)
            return self._finish("failed", error=f"Internal error: {type(exc).__name__}: {truncate(str(exc), 300)}")

    def _check_cancel(self) -> None:
        flag = self.db.scalar("SELECT cancel_requested FROM tasks WHERE id = ?", (self.task_id,))
        if flag:
            raise TaskStopped("Cancelled")

    def _limit_reason(self, hard: bool) -> str | None:
        st, lim = self.state, self.limits
        per_step = (st["cost"] / st["steps"]) if st["steps"] else 0.05
        estimate = 0.0 if hard else per_step * 1.5
        if st["steps"] >= (lim.max_steps if hard else lim.max_steps - 1):
            return f"step limit ({lim.max_steps})"
        if not hard and st["tool_calls"] >= lim.max_tool_calls:
            return f"tool call limit ({lim.max_tool_calls})"
        seconds = lim.max_seconds if hard else lim.max_seconds * 0.8
        if st["active_seconds"] >= seconds:
            return f"time limit ({int(lim.max_seconds // 60)} min)"
        tokens = lim.max_tokens if hard else lim.max_tokens * 0.9
        if st["total_tokens"] >= tokens:
            return f"token limit ({lim.max_tokens:,})"
        if st["cost_known"] and st["cost"] + estimate >= lim.task_max_cost:
            return f"task budget (${lim.task_max_cost:.2f})"
        if spend_today(self.db, self.tz) + estimate >= lim.daily_max_cost:
            return f"daily budget (${lim.daily_max_cost:.2f})"
        return None

    async def _loop(self) -> str:
        st = self.state
        while True:
            self._check_cancel()
            started = time.monotonic()
            try:
                if st["pending"]:
                    await self._run_pending()
                    continue
                hard = self._limit_reason(hard=True)
                if hard:
                    return self._finish_partial(hard)
                soft = self._limit_reason(hard=False)
                if soft and not st["wrap_up"]:
                    # No tool results to attach the note to (e.g. first step); a plain note is fine.
                    st["wrap_up"] = True
                    st["messages"].extend(self.provider.tool_results_messages([], self._wrap_up_note(soft)))
                    self._event("status", f"Approaching the {soft}; asking for the final answer")
                    self._checkpoint()
                turn = await self._call_model(allow_tools=not st["wrap_up"])
                outcome = self._handle_turn(turn)
                if outcome is not None:
                    return outcome
            finally:
                st["active_seconds"] = st.get("active_seconds", 0.0) + (time.monotonic() - started)

    def _wrap_up_note(self, reason: str) -> str:
        return (
            f"[Home Agent] The {reason} is nearly reached. Do not call more tools. Write your final answer now "
            "from what you have gathered, clearly marking anything unverified or unchecked."
        )

    async def _call_model(self, allow_tools: bool) -> ModelTurn:
        st = self.state
        specs = [ToolSpec(n, TOOLS_BY_NAME[n].description, TOOLS_BY_NAME[n].schema) for n in st["tools"]]
        remaining = max(30.0, self.limits.max_seconds - st["active_seconds"]) + 60
        timeout = min(self.services.settings.model_request_timeout, remaining)
        delays = [5, 20]
        for attempt in range(len(delays) + 1):
            self._check_cancel()
            try:
                turn = await asyncio.wait_for(
                    self.provider.complete(system=st["system"], messages=st["messages"], tools=specs, allow_tools=allow_tools),
                    timeout=timeout,
                )
                break
            except asyncio.TimeoutError:
                error = ModelError(f"The model did not answer within {int(timeout)} s", retryable=True)
            except ModelError as exc:
                error = exc
            if not error.retryable or attempt == len(delays):
                raise error
            self._event("status", f"Model call failed ({truncate(str(error), 160)}); retrying in {delays[attempt]} s")
            await asyncio.sleep(delays[attempt])
        st["steps"] += 1
        usage = turn.usage
        st["input_tokens"] += usage.input_tokens + usage.cache_read_tokens + usage.cache_write_tokens
        st["output_tokens"] += usage.output_tokens
        st["total_tokens"] += usage.total_tokens
        st["cost"] += usage.cost_usd
        st["cost_known"] = st["cost_known"] and usage.cost_known
        record_spend(self.db, self.task_id, self.tz, self.provider.name, usage)
        for note in turn.notes:
            self._event("status", note)
        return turn

    def _handle_turn(self, turn: ModelTurn) -> str | None:
        st = self.state
        if turn.stop_reason == "refusal":
            self._checkpoint()
            return self._finish("failed", error=turn.refusal or "The model declined this request")
        if turn.assistant_message is not None:
            st["messages"].append(turn.assistant_message)
        if turn.tool_calls and not st["wrap_up"]:
            # Text that accompanies tool calls is not shown: local models often put their
            # private reasoning there. Progress is reported from actual tool activity only.
            st["pending"] = [c.to_dict() for c in turn.tool_calls]
            if turn.stop_reason == "max_tokens":
                st["truncated_calls"] = True
            self._checkpoint()
            return None
        if turn.tool_calls and st["wrap_up"]:
            # The model asked for tools after being told to stop (some local models do).
            st["wrap_up_turns"] += 1
            if st["wrap_up_turns"] > 1 or turn.assistant_message is None:
                return self._finish_partial("limit", text=turn.text)
            results = [ToolResult(c.id, c.name, "No more tool calls are allowed. Write the final answer now.", True)
                       for c in turn.tool_calls]  # fmt: skip
            st["messages"].extend(self.provider.tool_results_messages(results))
            self._checkpoint()
            return None
        text = turn.text.strip()
        executed = st["tool_calls"]
        if text and not st["wrap_up"] and executed == 0 and st.get("action_nudges", 0) == 0 and (
            PROMISES_ACTION.search(text) or CLAIMS_ACTION.search(text)
        ):
            st["action_nudges"] = 1
            st["messages"].extend(self.provider.tool_results_messages([], ACTION_NUDGE))
            self._event("check", "The model answered without running any tool but described doing work; asked it to act")
            self._checkpoint()
            return None
        if text and executed == 0 and (CLAIMS_ACTION.search(text) or PROMISES_ACTION.search(text)):
            text += NO_TOOLS_NOTICE
            self._event("check", "The answer describes actions, but no tool ran in this task")
        elif text and executed and CLAIMS_SCRIPT_RUN.search(text) and "run_script" not in st.get("succeeded_tools", []):
            text += NO_SCRIPT_NOTICE
            self._event("check", "The answer describes running a script, but no script ran successfully in this task")
        if not text:
            if not st["wrap_up"]:
                st["wrap_up"] = True
                st["messages"].extend(self.provider.tool_results_messages(
                    [], "[Home Agent] Your last reply contained no answer. Write the final answer for the user now."))  # fmt: skip
                self._checkpoint()
                return None
            return self._finish_partial("empty answer")
        if turn.stop_reason == "max_tokens":
            text += "\n\n*(The answer was cut off by the output length limit.)*"
        self._checkpoint()
        detail = "Finished" if not st["wrap_up"] else "Finished early because a limit was reached"
        return self._finish("succeeded", text=text, detail=detail)

    async def _run_pending(self) -> None:
        st = self.state
        calls = [ToolCall.from_dict(c) for c in st["pending"]]
        allowed = set(st["tools"])
        results: dict[str, ToolResult] = {}
        reads, writes = [], []
        for call in calls:
            tool = TOOLS_BY_NAME.get(call.name)
            if tool is None or call.name not in allowed:
                results[call.id] = ToolResult(call.id, call.name, f"Unknown tool '{call.name}'.", True)
            elif st.get("truncated_calls"):
                results[call.id] = ToolResult(call.id, call.name,
                                              "This tool input was cut off by the output length limit. Retry with "
                                              "shorter input.", True)  # fmt: skip
            elif st["tool_calls"] >= self.limits.max_tool_calls:
                results[call.id] = ToolResult(call.id, call.name, "Tool call limit reached for this task.", True)
            else:
                st["tool_calls"] += 1
                (writes if tool.side_effect else reads).append(call)
        st.pop("truncated_calls", None)
        semaphore = asyncio.Semaphore(READ_CONCURRENCY)

        async def run_read(call: ToolCall):
            async with semaphore:
                results[call.id] = await self._execute(call)

        await asyncio.gather(*(run_read(c) for c in reads))
        for call in writes:
            self._check_cancel()
            results[call.id] = await self._execute(call)
        ordered = [results[c.id] for c in calls]
        succeeded = st.setdefault("succeeded_tools", [])
        for result in ordered:
            if not result.is_error and result.name not in succeeded:
                succeeded.append(result.name)
        note = None
        soft = self._limit_reason(hard=False)
        if soft and not st["wrap_up"]:
            st["wrap_up"] = True
            note = self._wrap_up_note(soft)
            self._event("status", f"Approaching the {soft}; asking for the final answer")
        st["messages"].extend(self.provider.tool_results_messages(ordered, note))
        st["pending"] = []
        self._checkpoint()

    async def _execute(self, call: ToolCall) -> ToolResult:
        ledger = self.db.one("SELECT * FROM tool_calls WHERE task_id = ? AND call_id = ?", (self.task_id, call.id))
        if ledger and ledger["status"] == "completed":
            return ToolResult(call.id, call.name, ledger["output"] or "", bool(ledger["is_error"]))
        tool = TOOLS_BY_NAME[call.name]
        summary = _summarize_call(call.name, call.input)
        if ledger is not None and not tool.rerun_safe:
            recovered = await self._recover_interrupted(tool, call)
            if recovered is not None:
                return recovered
        if ledger is None:
            self.db.execute(
                "INSERT OR IGNORE INTO tool_calls (task_id, call_id, name, input, status, started_at) VALUES (?, ?, ?, ?, 'started', ?)",
                (self.task_id, call.id, call.name, dumps(call.input), now_iso()),
            )
        self._event("tool_call", summary, {"tool": call.name, "call_id": call.id})
        if call.parse_error:
            output = ToolOutput(f"Invalid tool input: {call.parse_error}", is_error=True, summary="Invalid input")
        else:
            args, error = validate_input(tool, call.input)
            if error:
                output = ToolOutput(error, is_error=True, summary="Invalid input")
            else:
                ctx = ToolContext(services=self.services, task=self.task, state=self.state, limits=self.limits, tz=self.tz)
                limit = TOOL_TIMEOUT
                if call.name == "run_script":  # the script has its own time limit; leave room for it
                    limit = max(TOOL_TIMEOUT, self.services.scripts.max_seconds + 30)
                try:
                    output = await asyncio.wait_for(tool.handler(ctx, args, call.id), timeout=limit)
                except asyncio.TimeoutError:
                    output = ToolOutput(f"The tool timed out after {int(limit)} s.", is_error=True, summary="Timed out")
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log.exception("tool %s failed", call.name)
                    output = ToolOutput(f"The tool failed: {type(exc).__name__}", is_error=True, summary="Tool error")
        content = scrub(output.content, self.services.settings.secret_values())
        self.db.execute(
            "UPDATE tool_calls SET status = 'completed', output = ?, is_error = ?, finished_at = ? WHERE task_id = ? AND call_id = ?",
            (content, 1 if output.is_error else 0, now_iso(), self.task_id, call.id),
        )
        self._event("tool_error" if output.is_error else "tool_result", output.summary or ("Error" if output.is_error else "Done"),
                    {"tool": call.name, "call_id": call.id, **(output.data or {})})  # fmt: skip
        return ToolResult(call.id, call.name, content, output.is_error)

    async def _recover_interrupted(self, tool, call: ToolCall) -> ToolResult | None:
        """A call that started before a crash or restart and is not safe to repeat: report what
        happened from its own records. None means it never started, so running it now is safe."""
        output: ToolOutput | None = None
        args, error = validate_input(tool, call.input)
        if not error and tool.recover is not None:
            ctx = ToolContext(services=self.services, task=self.task, state=self.state, limits=self.limits, tz=self.tz)
            output = await tool.recover(ctx, args, call.id)
            if output is None:
                return None
        if output is None:
            output = ToolOutput(f"The {call.name} call was interrupted by a restart and was not run again, because "
                                "repeating it could duplicate its effects.", is_error=True, summary="Interrupted; not repeated")  # fmt: skip
        content = scrub(output.content, self.services.settings.secret_values())
        self.db.execute(
            "UPDATE tool_calls SET status = 'completed', output = ?, is_error = ?, finished_at = ? WHERE task_id = ? AND call_id = ?",
            (content, 1 if output.is_error else 0, now_iso(), self.task_id, call.id),
        )
        self._event("tool_error" if output.is_error else "tool_result", output.summary or "Recovered",
                    {"tool": call.name, "call_id": call.id, **(output.data or {})})  # fmt: skip
        return ToolResult(call.id, call.name, content, output.is_error)

    # -- finishing ---------------------------------------------------------------------------
    def _finish_partial(self, reason: str, text: str | None = None) -> str:
        saved = self.state.get("new_opportunities") or []
        sources = [s for s in list_task_sources(self.db, self.task_id) if s["kind"] != "search_result" and s["ok"]]
        lines = [text.strip()] if text and text.strip() else []
        if not lines:
            lines.append(f"I stopped before finishing because the {reason} was reached.")
            if saved:
                lines.append(f"I saved {len(saved)} opportunit{'y' if len(saved) == 1 else 'ies'} so far - see the Opportunities tab.")
            if sources:
                lines.append("Pages I read:\n" + "\n".join(f"- [{s['title'] or s['url']}]({s['final_url'] or s['url']})" for s in sources[:15]))
            lines.append("You can retry the task, or raise the limits in .env / Settings.")
        return self._finish("succeeded" if text else "failed", text="\n\n".join(lines),
                            detail=f"Stopped early: {reason}", error=None if text else f"Stopped: {reason}")  # fmt: skip

    def _finish(self, status: str, *, text: str | None = None, error: str | None = None, detail: str | None = None) -> str:
        secrets = self.services.settings.secret_values()
        text = scrub(text, secrets) if text else text
        error = scrub(error, secrets) if error else error
        st = self.state or {}
        unverified = []
        if text:
            consulted = {normalize_url(s["final_url"] or s["url"]) for s in list_task_sources(self.db, self.task_id)}
            consulted |= {normalize_url(s["url"]) for s in list_task_sources(self.db, self.task_id)}
            for url in dict.fromkeys(urls_in(text)):
                if normalize_url(url) not in consulted:
                    unverified.append(url)
        updated = self.db.execute(
            "UPDATE tasks SET status = ?, status_detail = ?, result_text = ?, error = ?, finished_at = ?, updated_at = ?, "
            "lease_owner = NULL, lease_expires_at = NULL, new_opportunities = ? WHERE id = ? AND status NOT IN ('succeeded', 'failed', 'cancelled')",
            (status, detail or error, text, error, now_iso(), now_iso(), len(st.get("new_opportunities") or []), self.task_id),
        ).rowcount
        if not updated:
            return (get_task(self.db, self.task_id) or {}).get("status", status)
        if unverified:
            self._event("check", f"{len(unverified)} link(s) in the answer were not opened during this task",
                        {"urls": unverified[:20]})  # fmt: skip
        self._event("status", {"succeeded": "Finished", "failed": f"Failed: {truncate(error or detail or '', 300)}",
                               "cancelled": "Cancelled"}.get(status, status))  # fmt: skip
        conversation_id = self.task.get("conversation_id")
        if conversation_id:
            if status == "succeeded" and text:
                role, content = "assistant", text
            elif status == "cancelled":
                role, content = "notice", "Task cancelled."
            else:
                role, content = "notice", f"Task failed: {error or detail}"
            self.db.insert("messages", {"conversation_id": conversation_id, "role": role, "content": content,
                                        "task_id": self.task_id, "created_at": now_iso()})  # fmt: skip
            self.db.execute("UPDATE conversations SET updated_at = ? WHERE id = ?", (now_iso(), conversation_id))
        if status != "cancelled":
            new_count = len(st.get("new_opportunities") or [])
            title = self.task.get("title", "Task")
            if status == "succeeded":
                heading = f"Done: {title}"
                body = f"{new_count} new opportunit{'y' if new_count == 1 else 'ies'} found." if new_count else ""
            else:
                heading = f"Failed: {title}"
                body = truncate(error or detail or "", 300)
            kind = "opportunities" if new_count else ("task_failed" if status == "failed" else "task_done")
            notify(self.db, kind, heading, body, link=f"#/tasks/{self.task_id}", task_id=self.task_id)
        return status


def summarize_checkpoint(checkpoint: str | None) -> dict:
    data = loads(checkpoint, {}) or {}
    return {k: data.get(k) for k in ("provider", "model", "steps", "tool_calls", "searches", "fetches", "cost")}


__all__ = ["AgentRunner", "Limits", "summarize_checkpoint", "json"]
