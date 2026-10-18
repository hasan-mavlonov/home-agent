"""Durable background task queue backed by the `tasks` table.

* Tasks are claimed atomically (queued -> running) with a lease that is renewed while
  the task runs. Closing the browser has no effect on running tasks.
* On startup, tasks left `running` by a previous process are re-queued and resume
  from their last checkpoint (see agent/runner.py). A task that keeps crashing the
  process is failed after MAX_ATTEMPTS instead of looping forever.
* Only one server process may use a data directory (file lock in app.py), so two
  processes can never run the same queue.
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import uuid

from .agent.runner import AgentRunner
from .db import in_seconds, now_iso
from .services.tasks import add_event, notify

log = logging.getLogger(__name__)

LEASE_SECONDS = 120
HEARTBEAT_SECONDS = 30
MAX_ATTEMPTS = 5


class Worker:
    def __init__(self, services, concurrency: int):
        self.services = services
        self.db = services.db
        self.concurrency = max(1, concurrency)
        self.owner = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
        self.running: dict[int, tuple[asyncio.Task, AgentRunner]] = {}
        self._wakeup = asyncio.Event()
        self._stopping = False
        self._loop_task: asyncio.Task | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    def recover(self) -> int:
        """Re-queue tasks interrupted by a crash or restart."""
        rows = self.db.all("SELECT id, attempts, cancel_requested FROM tasks WHERE status = 'running'")
        for row in rows:
            if row["cancel_requested"]:
                self.db.execute(
                    "UPDATE tasks SET status = 'cancelled', status_detail = 'Cancelled', finished_at = ?, updated_at = ? WHERE id = ?",
                    (now_iso(), now_iso(), row["id"]),
                )
            elif row["attempts"] >= MAX_ATTEMPTS:
                self.db.execute(
                    "UPDATE tasks SET status = 'failed', error = ?, status_detail = ?, finished_at = ?, updated_at = ? WHERE id = ?",
                    ("Stopped after repeated interruptions", "Stopped after repeated interruptions", now_iso(), now_iso(), row["id"]),
                )
                add_event(self.db, row["id"], "status", "Failed: interrupted too many times")
                notify(self.db, "task_failed", "A task was stopped after repeated interruptions", link=f"#/tasks/{row['id']}",
                       task_id=row["id"])  # fmt: skip
            else:
                self.db.execute(
                    "UPDATE tasks SET status = 'queued', lease_owner = NULL, lease_expires_at = NULL, updated_at = ? WHERE id = ?",
                    (now_iso(), row["id"]),
                )
                add_event(self.db, row["id"], "status", "Interrupted by a restart; queued to resume")
        return len(rows)

    def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._loop_task = asyncio.create_task(self._run(), name="home-agent-worker")

    def _in_loop(self, callback) -> None:
        """Run `callback` on the worker's event loop (API handlers call in from other threads)."""
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            current = asyncio.get_running_loop()
        except RuntimeError:
            current = None
        if current is loop:
            callback()
        else:
            loop.call_soon_threadsafe(callback)

    def wake(self) -> None:
        self._in_loop(self._wakeup.set)

    async def stop(self) -> None:
        self._stopping = True
        self._wakeup.set()
        for task, runner in list(self.running.values()):
            runner.shutting_down = True
            task.cancel()
        if self.running:
            await asyncio.gather(*(t for t, _ in self.running.values()), return_exceptions=True)
        # Leave interrupted tasks 'running' with an expiring lease; recover() resumes them next start.
        if self._loop_task:
            self._loop_task.cancel()
            await asyncio.gather(self._loop_task, return_exceptions=True)

    def cancel(self, task_id: int) -> bool:
        entry = self.running.get(task_id)
        if entry is None:
            return False
        self._in_loop(entry[0].cancel)
        return True

    def _claim(self) -> int | None:
        with self.db.tx():
            row = self.db.one("SELECT id FROM tasks WHERE status = 'queued' AND cancel_requested = 0 ORDER BY id LIMIT 1")
            if row is None:
                return None
            updated = self.db.execute(
                "UPDATE tasks SET status = 'running', lease_owner = ?, lease_expires_at = ?, attempts = attempts + 1, "
                "started_at = COALESCE(started_at, ?), updated_at = ? WHERE id = ? AND status = 'queued'",
                (self.owner, in_seconds(LEASE_SECONDS), now_iso(), now_iso(), row["id"]),
            ).rowcount
            return row["id"] if updated == 1 else None

    async def _run(self) -> None:
        last_heartbeat = 0.0
        loop = asyncio.get_running_loop()
        while not self._stopping:
            try:
                while len(self.running) < self.concurrency:
                    task_id = self._claim()
                    if task_id is None:
                        break
                    self._launch(task_id)
                now = loop.time()
                if now - last_heartbeat >= HEARTBEAT_SECONDS:
                    last_heartbeat = now
                    for task_id in list(self.running):
                        self.db.execute(
                            "UPDATE tasks SET lease_expires_at = ? WHERE id = ? AND lease_owner = ?",
                            (in_seconds(LEASE_SECONDS), task_id, self.owner),
                        )
            except Exception:
                log.exception("worker loop error")
            self._wakeup.clear()
            try:
                await asyncio.wait_for(self._wakeup.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                pass

    def _launch(self, task_id: int) -> None:
        runner = AgentRunner(self.services, task_id)

        async def run() -> None:
            try:
                await runner.run()
            except asyncio.CancelledError:
                pass
            except Exception:
                log.exception("task %s failed unexpectedly", task_id)
            finally:
                self.running.pop(task_id, None)
                self._wakeup.set()

        task = asyncio.create_task(run(), name=f"home-agent-task-{task_id}")
        self.running[task_id] = (task, runner)
