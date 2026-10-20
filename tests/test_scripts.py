"""Running workspace scripts, and the agent loop's checks that work was actually done.

REAL: Python subprocesses, the sandbox bootstrap, the workspace, the HTTP API, the worker
and the agent loop. MOCKED: the model (scripted). The sandbox runs in SCRIPT_SANDBOX=basic
here (in-process guard), because this Linux build machine has neither macOS Seatbelt nor
bubblewrap; the Seatbelt and bubblewrap command lines are checked as text only.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
import time

import pytest
from conftest import make_settings
from helpers import ScriptedProvider, tool_json, wait_for

from home_agent.agent import sandbox
from home_agent.agent.runner import NO_SCRIPT_NOTICE, NO_TOOLS_NOTICE
from home_agent.agent.sandbox import ScriptRunner, resolve_mode, run_id_for
from home_agent.services.workspace import Workspace

FIB = """import csv, os, sys
os.makedirs("outputs", exist_ok=True)
n = int(sys.argv[1]) if len(sys.argv) > 1 else 10
a, b, rows = 0, 1, []
for i in range(n):
    rows.append((i, a))
    a, b = b, a + b
with open("outputs/fib.csv", "w", newline="") as f:
    csv.writer(f).writerows([("n", "fib"), *rows])
print(f"wrote {len(rows)} rows")
"""
FIB_CSV = "n,fib\r\n0,0\r\n1,1\r\n2,1\r\n3,2\r\n4,3\r\n5,5\r\n6,8\r\n7,13\r\n8,21\r\n9,34\r\n"


def runner(tmp_path, **overrides) -> ScriptRunner:
    settings = make_settings(tmp_path, script_sandbox="basic", **overrides)
    return ScriptRunner(settings, Workspace(settings.workspace_dir))


def run(coro):
    return asyncio.run(coro)


def test_script_run_reports_exit_status_output_and_the_file_it_wrote(tmp_path):
    r = runner(tmp_path)
    r.workspace.write_text("scripts/fib.py", FIB)
    result = run(r.run("scripts/fib.py", ["10"], 30, "t1-a"))
    assert (result.status, result.exit_code, result.stdout) == ("exited", 0, "wrote 10 rows\n")
    content = (r.workspace.root / "outputs/fib.csv").read_bytes()
    assert content.decode() == FIB_CSV
    assert result.files == [{"path": "outputs/fib.csv", "change": "created", "size": len(content),
                             "sha256": hashlib.sha256(content).hexdigest()}]  # fmt: skip
    record = json.loads((r.runs_dir / "t1-a" / "result.json").read_text())
    assert record["exit_code"] == 0 and json.loads((r.runs_dir / "t1-a" / "exit.json").read_text()) == {"exit_code": 0}


def test_failures_and_time_limit_are_reported_as_they_happened(tmp_path):
    r = runner(tmp_path)
    r.workspace.write_text("bad.py", "import sys\nprint('partial')\nsys.stderr.write('boom\\n')\nsys.exit(3)\n")
    result = run(r.run("bad.py", [], 30, "t1-b"))
    assert (result.status, result.exit_code, result.stdout, result.stderr) == ("exited", 3, "partial\n", "boom\n")
    r.workspace.write_text("raise.py", "raise ValueError('bad input')\n")
    result = run(r.run("raise.py", [], 30, "t1-c"))
    assert result.exit_code == 1 and "ValueError: bad input" in result.stderr
    r.workspace.write_text("slow.py", "import time\ntime.sleep(30)\nopen('late.txt', 'w').write('x')\n")
    started = time.monotonic()
    result = run(r.run("slow.py", [], 1, "t1-d"))
    assert result.status in ("timed_out", "killed") and result.exit_code is None
    assert time.monotonic() - started < 10 and not (r.workspace.root / "late.txt").exists()
    with pytest.raises(sandbox.ScriptError, match="Write it with write_file first"):
        run(r.run("missing.py", [], 5, "t1-e"))
    r.workspace.write_text("notes.txt", "x")
    with pytest.raises(sandbox.ScriptError, match="Only .py"):
        run(r.run("notes.txt", [], 5, "t1-f"))


def test_sandbox_refusals_and_clean_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("QWEN_API_KEY", "sk-should-never-reach-scripts")
    r = runner(tmp_path)
    private = tmp_path / "data" / "home_agent.sqlite3"
    private.parent.mkdir(parents=True, exist_ok=True)
    private.write_text("private")
    probe = f"""import os
results = {{}}
def attempt(name, fn):
    try:
        fn()
        results[name] = "allowed"
    except Exception as exc:
        results[name] = "blocked"
def net():
    import socket
    socket.socket()
def proc():
    import subprocess
    subprocess.run(["echo", "x"])
def system():
    os.system("echo x")
def native():
    import ctypes
def outside():
    open({str(tmp_path / 'outside.txt')!r}, "w").write("x")
def private():
    open({str(private)!r}).read()
def link():
    os.symlink("/etc/passwd", "link")
attempt("network", net); attempt("subprocess", proc); attempt("os.system", system); attempt("ctypes", native)
attempt("write_outside", outside); attempt("read_private", private); attempt("symlink", link)
open("report.json", "w").write(__import__("json").dumps({{"results": results, "env": sorted(os.environ)}}))
"""
    r.workspace.write_text("probe.py", probe)
    result = run(r.run("probe.py", [], 30, "t2-a"))
    assert result.ok, result.stderr
    report = json.loads((r.workspace.root / "report.json").read_text())
    assert set(report["results"].values()) == {"blocked"}, report["results"]
    assert report["env"] == ["HOME", "LANG", "PATH", "TMPDIR"]  # nothing from the server's environment
    assert not (tmp_path / "outside.txt").exists()
    assert {name: ok for name, ok, _ in run(r.self_test())} == {
        "Script runs": True, "Network blocked": True, "New processes blocked": True,
        "Writes outside the workspace blocked": True, "Reading Home Agent's private files blocked": True,
        "Writes inside the workspace allowed": True}  # fmt: skip


def test_sandbox_mode_selection_and_os_sandbox_command_lines(tmp_path, monkeypatch):
    """Seatbelt and bubblewrap cannot run on this machine; their command lines are checked as text."""
    assert resolve_mode("off")[0] == "off" and resolve_mode("basic")[0] == "basic"
    monkeypatch.setattr(sandbox.sys, "platform", "linux")
    monkeypatch.setattr(sandbox.shutil, "which", lambda name: None)
    mode, reason = resolve_mode("auto")
    assert mode == "off" and "bubblewrap" in reason  # never silently falls back to the weaker mode
    r = runner(tmp_path)
    run_dir = r.runs_dir / "x"
    r.mode = "seatbelt"
    cmd = r._command(r.workspace.root / "a.py", ["1"], run_dir, 30)
    assert cmd[:2] == ["/usr/bin/sandbox-exec", "-p"]
    profile = cmd[2]
    assert "(deny default)" in profile and "network" not in profile  # network stays denied by default
    assert '(allow file-write* (subpath (param "WS")) (subpath (param "RUN")))' in profile
    assert '(require-not (subpath (param "HOME_DIR")))' in profile and "process-fork" not in profile
    params = dict(cmd[i + 1].split("=", 1) for i, x in enumerate(cmd) if x == "-D")
    assert params["WS"] == os.path.realpath(r.workspace.root) and params["HOME_DIR"] == r.home
    assert cmd[cmd.index("-c") - 5:cmd.index("-c")] == ["-I", "-S", "-B", "-X", "utf8"]
    r.mode = "bwrap"
    cmd = r._command(r.workspace.root / "a.py", [], run_dir, 30)
    assert cmd[:4] == ["bwrap", "--die-with-parent", "--new-session", "--unshare-all"]
    assert cmd.index("--tmpfs") < cmd.index("--bind") and ["--ro-bind", "/", "/"] == cmd[4:7]


# ---------------------------------------------------------------------------------------------
# through the agent loop


def _ask(h, text):
    h.login()
    conversation = h.post("/api/conversations", json={}).json()
    task_id = h.post(f"/api/conversations/{conversation['id']}/messages", json={"content": text}).json()["task"]["id"]
    return wait_for(lambda: (t := h.get(f"/api/tasks/{task_id}").json())["status"] in ("succeeded", "failed", "cancelled") and t,
                    timeout=30)  # fmt: skip


def test_agent_writes_runs_and_returns_the_artifact(harness_factory):
    seen = {}

    def answer(messages):
        seen["run_result"] = tool_json(messages)[0]
        return ("text", "Ran scripts/fib.py (exit status 0). The CSV is outputs/fib.csv.")

    h = harness_factory(settings_overrides={"script_sandbox": "basic"}, provider=ScriptedProvider([
        lambda m: ("tools", [("write_file", {"path": "scripts/fib.py", "content": FIB})]),
        lambda m: ("tools", [("run_script", {"path": "scripts/fib.py", "args": ["10"]})]),
        answer,
    ]))  # fmt: skip
    with h:
        task = _ask(h, "Write and run a script that saves the first 10 Fibonacci numbers as CSV.")
        assert task["status"] == "succeeded" and task["tool_calls"] == 2
        assert 'exit_status="0"' in seen["run_result"] and "created outputs/fib.csv" in seen["run_result"]
        assert [r["exit_code"] for r in task["script_runs"]] == [0]
        artifacts = {a["path"]: a for a in task["artifacts"]}
        csv_hash = hashlib.sha256(FIB_CSV.encode()).hexdigest()
        assert artifacts["outputs/fib.csv"]["sha256"] == csv_hash and artifacts["outputs/fib.csv"]["unchanged"]
        assert "scripts/fib.py" in artifacts
        assert any(e["summary"] == "Running script scripts/fib.py 10" for e in task["events"])
        assert not any(e["type"] == "check" for e in task["events"])
        download = h.get("/api/files/download?path=outputs/fib.csv")
        assert download.status_code == 200 and download.content.decode() == FIB_CSV
        assert download.headers["content-disposition"] == 'attachment; filename="fib.csv"'
        assert download.headers["content-type"] == "application/octet-stream"
        assert h.get("/api/files/download?path=../etc/passwd").status_code == 400
        assert h.get("/api/integrations").json()["scripts"]["mode"] == "basic"
        checks = h.post("/api/integrations/test-scripts").json()
        assert checks["ok"], checks


def test_a_promise_without_action_gets_the_model_to_act(harness_factory):
    def act(messages):
        nudge = messages[-1]["content"]
        assert nudge.startswith("[Home Agent] You replied without calling any tool")
        return ("tools", [("write_file", {"path": "notes/plan.md", "content": "# Plan"})])

    h = harness_factory(provider=ScriptedProvider([
        lambda m: ("text", "Sure! I'll write the plan to notes/plan.md now."),
        act,
        lambda m: ("text", "Wrote notes/plan.md."),
    ]))  # fmt: skip
    with h:
        task = _ask(h, "Write a one-line plan to notes/plan.md")
        assert task["status"] == "succeeded" and task["tool_calls"] == 1 and task["result_text"] == "Wrote notes/plan.md."
        assert [e["summary"] for e in task["events"] if e["type"] == "check"] == [
            "The model answered without running any tool but described doing work; asked it to act"]
        assert h.services.workspace.read_text("notes/plan.md")[0] == "# Plan"


def test_claimed_work_without_tools_is_labelled_and_plain_answers_are_left_alone(harness_factory):
    claim = "I've created fib.py, ran it, and saved fib.csv: 0,1,1,2,3."
    h = harness_factory(provider=ScriptedProvider([lambda m: ("text", claim)]))
    with h:
        task = _ask(h, "Create and run a Fibonacci script.")
        assert task["status"] == "succeeded" and task["steps"] == 2 and task["tool_calls"] == 0
        assert task["result_text"] == claim + NO_TOOLS_NOTICE
        assert "no tools ran during this task" in task["result_html"]
    plain = harness_factory(provider=ScriptedProvider([lambda m: ("text", "Paris is the capital of France.")]))
    with plain:
        task = _ask(plain, "What is the capital of France?")
        assert task["steps"] == 1 and task["result_text"] == "Paris is the capital of France."
        assert not any(e["type"] == "check" for e in task["events"])


def test_a_repeated_promise_is_labelled_too(harness_factory):
    promise = "Sure, I'll write that script for you."
    h = harness_factory(provider=ScriptedProvider([lambda m: ("text", promise)]))
    with h:
        task = _ask(h, "Write a script")
        assert task["steps"] == 2 and task["result_text"] == promise + NO_TOOLS_NOTICE


def test_script_output_claimed_without_a_successful_run_is_labelled(harness_factory):
    h = harness_factory(settings_overrides={"script_sandbox": "basic"}, provider=ScriptedProvider([
        lambda m: ("tools", [("write_file", {"path": "s.py", "content": "print(1)"})]),
        lambda m: ("text", "I ran the script; it printed 1 with exit status 0."),
    ]))  # fmt: skip
    with h:
        task = _ask(h, "Write s.py and run it")
        assert task["result_text"].endswith(NO_SCRIPT_NOTICE)


def test_without_a_sandbox_scripts_are_not_offered_and_the_model_is_told(harness_factory):
    h = harness_factory(settings_overrides={"script_sandbox": "off"},
                        provider=ScriptedProvider([lambda m: ("text", "Running code is not available here.")]))  # fmt: skip
    with h:
        task = _ask(h, "Run a script for me")
        state = json.loads(h.db.scalar("SELECT checkpoint FROM tasks WHERE id = ?", (task["id"],)))
        assert "run_script" not in state["tools"] and "write_file" in state["tools"]
        assert "Running scripts or code is not available on this installation" in state["system"]


def test_interrupted_script_is_recovered_not_run_again(harness_factory, tmp_path):
    """Simulates a crash in the middle of a run: the script was started, the server died before
    recording the result. On resume the outcome comes from the run record."""
    h = harness_factory(settings_overrides={"script_sandbox": "basic"}, start_background=False,
                        provider=ScriptedProvider([lambda m: ("text", "Recovered: " + tool_json(m)[0][:60])]))  # fmt: skip
    with h:
        db, scripts = h.db, h.services.scripts
        scripts.workspace.write_text("append.py", "import time\ntime.sleep(1)\nopen('log.txt', 'a').write('ran\\n')\nprint('done')\n")
        task_id = db.insert("tasks", {"kind": "chat", "title": "t", "prompt": "run append.py", "status": "running",
                                      "provider": "scripted", "created_at": "2026-10-20T00:00:00Z", "updated_at": "2026-10-20T00:00:00Z"})  # fmt: skip
        call = {"id": "call_1", "name": "run_script", "input": {"path": "append.py"}, "parse_error": None}
        # what the crashed server left behind: the call in its checkpoint and ledger, and a running process
        run_dir = scripts.runs_dir / run_id_for(task_id, "call_1")

        # Started with Popen, which (unlike an asyncio subprocess transport) never kills its child on
        # cleanup: like a real crash, the script keeps running with nobody waiting for it.
        for sub in ("home", "tmp"):
            (run_dir / sub).mkdir(parents=True, exist_ok=True)
        (run_dir / "before.json").write_text("{}")
        (run_dir / "meta.json").write_text(json.dumps({"script": "append.py", "args": [], "timeout": 30,
                                                       "started_at": time.time(), "mode": "basic"}))  # fmt: skip
        with (run_dir / "stdout").open("wb") as out, (run_dir / "stderr").open("wb") as err:
            orphan = subprocess.Popen(scripts._command(scripts.workspace.root / "append.py", [], run_dir, 30),
                                      cwd=scripts.workspace.root, env=scripts._environment(run_dir), stdout=out,
                                      stderr=err, start_new_session=True)  # fmt: skip
        (run_dir / "pid").write_text(str(orphan.pid))
        provider = h.provider
        state = {"v": 1, "provider": provider.name, "model": provider.model, "system": "S", "tools": ["run_script"],
                 "messages": [{"role": "user", "content": "run append.py"},
                              {"role": "assistant", "content": "", "tool_calls": [{"id": "call_1", "name": "run_script"}]}],
                 "pending": [call], "steps": 1, "tool_calls": 1, "searches": 0, "fetches": 0, "input_tokens": 0,
                 "output_tokens": 0, "total_tokens": 0, "cost": 0.0, "cost_known": True, "active_seconds": 0.0,
                 "wrap_up": False, "wrap_up_turns": 0, "new_opportunities": [], "succeeded_tools": [], "action_nudges": 0}  # fmt: skip
        db.execute("UPDATE tasks SET checkpoint = ? WHERE id = ?", (json.dumps(state), task_id))
        db.execute("INSERT INTO tool_calls (task_id, call_id, name, input, status, started_at) VALUES (?, 'call_1', "
                   "'run_script', ?, 'started', '2026-10-20T00:00:00Z')", (task_id, json.dumps(call["input"])))  # fmt: skip
        from home_agent.agent.runner import AgentRunner

        status = asyncio.run(AgentRunner(h.services, task_id).run())
        assert status == "succeeded"
        assert (scripts.workspace.root / "log.txt").read_text() == "ran\n"  # ran exactly once
        assert orphan.wait(timeout=5) == 0
        ledger = db.one("SELECT * FROM tool_calls WHERE task_id = ? AND call_id = 'call_1'", (task_id,))
        assert ledger["status"] == "completed" and "was NOT run again" in ledger["output"] and 'exit_status="0"' in ledger["output"]
        events = [r["summary"] for r in db.all("SELECT summary FROM task_events WHERE task_id = ?", (task_id,))]
        assert any(s.startswith("Recovered after restart: Exit 0") for s in events)
        # a second resume of the same call reads the stored result, still without running it
        result = asyncio.run(scripts.recover(run_id_for(task_id, "call_1")))
        assert result.recovered and result.exit_code == 0
        assert (scripts.workspace.root / "log.txt").read_text() == "ran\n"


def test_cancelling_a_task_stops_its_script(harness_factory):
    slow = "import time\nprint('started', flush=True)\ntime.sleep(20)\nopen('too_late.txt', 'w').write('x')\n"
    h = harness_factory(settings_overrides={"script_sandbox": "basic"}, provider=ScriptedProvider([
        lambda m: ("tools", [("write_file", {"path": "slow.py", "content": slow})]),
        lambda m: ("tools", [("run_script", {"path": "slow.py", "timeout_seconds": 60})]),
        lambda m: ("text", "done"),
    ]))  # fmt: skip
    with h:
        h.login()
        conversation = h.post("/api/conversations", json={}).json()
        task_id = h.post(f"/api/conversations/{conversation['id']}/messages", json={"content": "run slow"}).json()["task"]["id"]
        runs = h.services.scripts.runs_dir
        pid_file = wait_for(lambda: next(iter(runs.glob("*/pid")), None), timeout=20)
        wait_for(lambda: b"started" in (pid_file.parent / "stdout").read_bytes(), timeout=10)
        pid = int(pid_file.read_text())
        assert h.post(f"/api/tasks/{task_id}/cancel").status_code == 200
        task = wait_for(lambda: (t := h.get(f"/api/tasks/{task_id}").json())["status"] == "cancelled" and t, timeout=15)
        assert task["status"] == "cancelled"
        wait_for(lambda: not _alive(pid), timeout=10)
        record = json.loads((pid_file.parent / "result.json").read_text())
        assert record["status"] == "interrupted"
        time.sleep(1)
        assert not (h.services.workspace.root / "too_late.txt").exists()


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:  # a zombie still answers kill(0); check its state where /proc exists
        with open(f"/proc/{pid}/stat") as handle:
            return handle.read().split(")")[-1].split()[0] != "Z"
    except OSError:
        return True
