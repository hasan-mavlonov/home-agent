"""Whole-process tests: the real `home-agent serve` process, real HTTP, real SIGKILL.

REAL: the server process (uvicorn, worker, scheduler, SQLite), HTTP between the test client
and the server and between the server and the model endpoint, script subprocesses, crashes
(SIGKILL) and graceful restarts (SIGTERM).
MOCKED: the model. It is a deterministic OpenAI-compatible endpoint served from this test
process, because no real model is reachable from the build machine. Script sandbox:
SCRIPT_SANDBOX=basic (no OS sandbox on this Linux machine).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import signal
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx2
import pytest
from conftest import PASSWORD

from home_agent.config import REPO_ROOT, load_settings
from home_agent.db import Database
from home_agent.security.auth import AuthService

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")

FIB_SLOW = """import csv, os, sys, time
print("started", flush=True)
time.sleep(float(sys.argv[2]) if len(sys.argv) > 2 else 0)
os.makedirs("outputs", exist_ok=True)
with open("outputs/runs.log", "a") as log:
    log.write("run\\n")
a, b, rows = 0, 1, []
for i in range(int(sys.argv[1])):
    rows.append((i, a))
    a, b = b, a + b
with open("outputs/fib.csv", "w", newline="") as f:
    csv.writer(f).writerows([("n", "fib"), *rows])
print(f"wrote {len(rows)} rows")
"""
SLOW = "import time\nprint('started', flush=True)\ntime.sleep(30)\nopen('too_late.txt', 'w').write('x')\n"


# ---------------------------------------------------------------------------------------------
# a deterministic stand-in for the model (OpenAI-compatible chat completions)


class MockModel:
    def __init__(self):
        self.requests: list[dict] = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/v1"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()

    def _handler(self):
        model = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["content-length"])))
                model.requests.append(body)
                payload = json.dumps(model.respond(body["messages"])).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        return Handler

    @staticmethod
    def respond(messages: list[dict]) -> dict:
        task = next(m["content"] for m in messages if m["role"] == "user")
        results = [m["content"] for m in messages if m["role"] == "tool"]
        if "E2E-FIB" in task:
            delay = re.search(r"delay=(\d+)", task).group(1)
            plan = [("write_file", {"path": "scripts/fib.py", "content": FIB_SLOW}),
                    ("run_script", {"path": "scripts/fib.py", "args": ["10", delay], "timeout_seconds": 30})]  # fmt: skip
        elif "E2E-CANCEL" in task:
            plan = [("write_file", {"path": "scripts/slow.py", "content": SLOW}),
                    ("run_script", {"path": "scripts/slow.py", "timeout_seconds": 60})]  # fmt: skip
        elif "E2E-HEARTBEAT" in task:
            plan = [("write_file", {"path": "notes/heartbeat.md", "content": "heartbeat\n"})]
        else:
            return _text("I only handle the test tasks.")
        if len(results) < len(plan):
            name, args = plan[len(results)]
            return _call(f"call_{len(results)}", name, args)
        status = re.search(r'exit_status="(\w+)"', results[-1])
        return _text(f"Done. exit status {status.group(1)}." if status else f"Done: {results[-1][:80]}")


def _call(call_id, name, args):
    return {"choices": [{"finish_reason": "tool_calls", "message": {"role": "assistant", "content": None, "tool_calls": [
        {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]}}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 10}}  # fmt: skip


def _text(text):
    return {"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": text}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 10}}  # fmt: skip


# ---------------------------------------------------------------------------------------------
# the server process


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class ServerProcess:
    def __init__(self, tmp_path: Path, model_url: str):
        self.port = _free_port()
        self.data = tmp_path / "data"
        self.env_file = tmp_path / ".env"
        self.env_file.write_text("\n".join([
            "HOME_AGENT_HOST=127.0.0.1", f"HOME_AGENT_PORT={self.port}", f"HOME_AGENT_DATA_DIR={self.data}",
            "HOME_AGENT_SECRET_KEY=" + "e2e-secret-" + "x" * 40, "HOME_AGENT_TIMEZONE=UTC", "HOME_AGENT_PREVENT_SLEEP=false",
            "HOME_AGENT_SCHEDULER_TICK_SECONDS=1", "MODEL_PROVIDER=openai_compatible", f"OPENAI_COMPAT_BASE_URL={model_url}",
            "OPENAI_COMPAT_MODEL=e2e-mock", "SEARCH_PROVIDER=none", "BROWSER_ENABLED=false", "SCRIPT_SANDBOX=basic", ""]))  # fmt: skip
        self.env_file.chmod(0o600)
        settings = load_settings(self.env_file)
        db = Database(settings.db_path)
        db.migrate()
        AuthService(db).set_password(PASSWORD)
        db.close()
        self.log = tmp_path / "server.log"
        self.proc: subprocess.Popen | None = None
        self.base = f"http://127.0.0.1:{self.port}"

    def start(self):
        env = {**os.environ, "HOME_AGENT_ENV_FILE": str(self.env_file)}
        with self.log.open("ab") as log:
            self.proc = subprocess.Popen([sys.executable, "-m", "home_agent", "serve"], cwd=REPO_ROOT, env=env,
                                         stdout=log, stderr=subprocess.STDOUT)  # fmt: skip
        deadline = time.time() + 30
        while time.time() < deadline:
            assert self.proc.poll() is None, self.log.read_text()[-2000:]
            try:
                if httpx2.get(self.base + "/api/health", timeout=1).status_code == 200:
                    return
            except httpx2.HTTPError:
                pass
            time.sleep(0.2)
        raise AssertionError("server did not start:\n" + self.log.read_text()[-2000:])

    def crash(self):
        """SIGKILL: no shutdown code runs, exactly like a power cut or an OOM kill."""
        self.proc.send_signal(signal.SIGKILL)
        self.proc.wait(10)

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
            self.proc.wait(30)

    @contextmanager
    def client(self):
        """A fresh connection and a fresh login session."""
        with httpx2.Client(base_url=self.base, timeout=10) as client:
            csrf = client.post("/api/auth/login", json={"password": PASSWORD}).json()["csrf_token"]
            client.headers["X-CSRF-Token"] = csrf
            yield client

    def db(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.data / "home_agent.sqlite3")
        conn.row_factory = sqlite3.Row
        return conn


@pytest.fixture
def model():
    mock = MockModel()
    yield mock
    mock.close()


@pytest.fixture
def server(tmp_path, model):
    proc = ServerProcess(tmp_path, model.url)
    yield proc
    proc.stop()


def _post_task(server: ServerProcess, text: str) -> int:
    with server.client() as client:  # the client disconnects as soon as the task is accepted
        conversation = client.post("/api/conversations", json={}).json()
        return client.post(f"/api/conversations/{conversation['id']}/messages", json={"content": text}).json()["task"]["id"]


def _wait_task(server: ServerProcess, task_id: int, statuses=("succeeded", "failed", "cancelled"), timeout=60) -> dict:
    with server.client() as client:  # a new connection and a new login: nothing carries over from the first client
        deadline = time.time() + timeout
        while time.time() < deadline:
            task = client.get(f"/api/tasks/{task_id}").json()
            if task["status"] in statuses:
                return task
            time.sleep(0.3)
    raise AssertionError(f"task {task_id} did not finish; last status {task['status']}")


def _wait(predicate, timeout=30, interval=0.1):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    raise AssertionError("condition not met in time")


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:
        with open(f"/proc/{pid}/stat") as handle:
            return handle.read().split(")")[-1].split()[0] != "Z"
    except OSError:
        return True


def _expected_csv() -> bytes:
    a, b, rows = 0, 1, ["n,fib"]
    for i in range(10):
        rows.append(f"{i},{a}")
        a, b = b, a + b
    return ("\r\n".join(rows) + "\r\n").encode()


# ---------------------------------------------------------------------------------------------


def test_task_continues_after_the_client_disconnects(server, model):
    server.start()
    task_id = _post_task(server, "E2E-FIB delay=2: write and run the Fibonacci script.")
    task = _wait_task(server, task_id)
    assert task["status"] == "succeeded" and task["result_text"] == "Done. exit status 0."
    workspace = server.data / "workspace"
    assert (workspace / "outputs/fib.csv").read_bytes() == _expected_csv()
    assert [r["exit_code"] for r in task["script_runs"]] == [0]
    csv_artifact = next(a for a in task["artifacts"] if a["path"] == "outputs/fib.csv")
    assert csv_artifact["sha256"] == hashlib.sha256(_expected_csv()).hexdigest() and csv_artifact["unchanged"]
    with server.client() as client:
        assert client.get("/api/files/download?path=outputs/fib.csv").content == _expected_csv()


def test_crash_mid_script_resumes_without_running_it_twice(server, model):
    server.start()
    task_id = _post_task(server, "E2E-FIB delay=4: write and run the Fibonacci script.")
    runs = server.data / "runs"
    stdout = _wait(lambda: next((p / "stdout" for p in runs.glob("t*") if (p / "stdout").exists()
                                 and b"started" in (p / "stdout").read_bytes()), None))  # fmt: skip
    script_pid = int((stdout.parent / "pid").read_text())
    server.crash()  # the server dies while the script sleeps
    assert _alive(script_pid)  # the script itself keeps running and finishes on its own
    server.start()
    task = _wait_task(server, task_id)
    assert task["status"] == "succeeded" and task["result_text"] == "Done. exit status 0."
    workspace = server.data / "workspace"
    assert (workspace / "outputs/runs.log").read_text() == "run\n"  # the side effect happened exactly once
    assert (workspace / "outputs/fib.csv").read_bytes() == _expected_csv()
    summaries = [e["summary"] for e in task["events"]]
    assert "Interrupted by a restart; queued to resume" in summaries
    assert any(s.startswith("Resumed after restart") for s in summaries)
    assert any(s.startswith("Recovered after restart: Exit 0") for s in summaries)
    assert task["script_runs"] == [{"script": "scripts/fib.py", "status": "exited", "exit_code": 0,
                                    "duration": task["script_runs"][0]["duration"], "recovered": True,
                                    "run_id": stdout.parent.name}]  # fmt: skip
    with server.db() as conn:
        rows = conn.execute("SELECT status FROM tool_calls WHERE task_id = ? AND name = 'run_script'", (task_id,)).fetchall()
    assert [r["status"] for r in rows] == ["completed"]
    assert len(list(runs.glob("t*"))) == 1  # one run record: the script was started once
    final_request = model.requests[-1]["messages"]
    assert 'recovered_after_restart="true"' in final_request[-1]["content"]


def test_cancel_stops_the_running_script(server, model):
    server.start()
    task_id = _post_task(server, "E2E-CANCEL: run the slow script.")
    runs = server.data / "runs"
    stdout = _wait(lambda: next((p / "stdout" for p in runs.glob("t*") if (p / "stdout").exists()
                                 and b"started" in (p / "stdout").read_bytes()), None))  # fmt: skip
    script_pid = int((stdout.parent / "pid").read_text())
    with server.client() as client:
        assert client.post(f"/api/tasks/{task_id}/cancel").status_code == 200
    task = _wait_task(server, task_id)
    assert task["status"] == "cancelled"
    _wait(lambda: not _alive(script_pid), timeout=10)
    assert json.loads((stdout.parent / "result.json").read_text())["status"] == "interrupted"
    time.sleep(1)
    assert not (server.data / "workspace" / "too_late.txt").exists()
    server.stop()
    server.start()  # after a restart the cancelled task stays cancelled and nothing runs again
    time.sleep(2)
    assert _wait_task(server, task_id)["status"] == "cancelled"
    assert len(list(runs.glob("t*"))) == 1


def test_schedule_is_persisted_across_a_restart_and_runs_when_due(server, model):
    server.start()
    now = datetime.now(UTC)
    due = (now + timedelta(minutes=1)).replace(second=0, microsecond=0)
    with server.client() as client:
        created = client.post("/api/schedules", json={"name": "E2E heartbeat", "prompt": "E2E-HEARTBEAT: write the note.",
                                                      "recurrence": {"freq": "daily", "time": due.strftime("%H:%M")},
                                                      "timezone": "UTC"}).json()  # fmt: skip
    assert created["status"] == "active" and created["next_run_at"] == due.strftime("%Y-%m-%dT%H:%M:%SZ")
    server.stop()  # the schedule lives in the database, not in the process
    server.start()
    with server.client() as client:
        listed = {s["id"]: s for s in client.get("/api/schedules").json()["items"]}
        assert listed[created["id"]]["next_run_at"] == created["next_run_at"]
        assert not [t for t in client.get("/api/tasks").json()["items"] if t["kind"] == "scheduled"]  # not run early
        scheduled = _wait(lambda: [t for t in client.get("/api/tasks").json()["items"] if t["kind"] == "scheduled"],
                          timeout=(due - datetime.now(UTC)).total_seconds() + 30, interval=0.5)  # fmt: skip
    assert datetime.now(UTC) >= due  # it ran only once the time had come
    task = _wait_task(server, scheduled[0]["id"])
    assert task["status"] == "succeeded" and task["scheduled_for"] == created["next_run_at"]
    assert (server.data / "workspace" / "notes/heartbeat.md").read_text() == "heartbeat\n"
    with server.client() as client:
        after = {s["id"]: s for s in client.get("/api/schedules").json()["items"]}[created["id"]]
        assert after["next_run_at"] == (due + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        assert len([t for t in client.get("/api/tasks").json()["items"] if t["kind"] == "scheduled"]) == 1
