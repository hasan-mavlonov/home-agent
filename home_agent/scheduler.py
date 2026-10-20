"""Turns due schedules into queued tasks (one per slot, see services/schedules.py)."""

from __future__ import annotations

import asyncio
import logging

from .services import schedules as schedule_service
from .services.tasks import add_event

log = logging.getLogger(__name__)

TICK_SECONDS = 30


class Scheduler:
    def __init__(self, services):
        self.services = services
        self.db = services.db
        self._task: asyncio.Task | None = None

    def tick(self) -> list[int]:
        created = []
        for schedule in schedule_service.due_schedules(self.db):
            task_id = schedule_service.claim_due_slot(self.db, schedule)
            if task_id is not None:
                provider = self.services.runtime("model_provider")
                self.db.execute("UPDATE tasks SET provider = ? WHERE id = ?", (provider, task_id))
                add_event(self.db, task_id, "status", f"Queued by schedule '{schedule['name']}'")
                created.append(task_id)
        if created:
            self.services.worker.wake()
        return created

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="home-agent-scheduler")

    async def _run(self) -> None:
        while True:
            try:
                self.tick()
            except Exception:
                log.exception("scheduler tick failed")
            await asyncio.sleep(getattr(self.services.settings, "scheduler_tick_seconds", TICK_SECONDS))

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
