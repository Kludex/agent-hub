from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

import anyio


@dataclass(frozen=True)
class ScheduleRequest:
    run_id: str
    workspace: str
    access: str
    isolated: bool = False

    @property
    def locks_workspace(self) -> bool:
        return self.access == "shared-write" and not self.isolated


class Scheduler:
    def __init__(self, global_limit: int) -> None:
        self._global_limit = global_limit
        self._condition = anyio.Condition()
        self._waiting: list[ScheduleRequest] = []
        self._active: set[ScheduleRequest] = set()
        self._writers: set[str] = set()

    @asynccontextmanager
    async def slot(self, request: ScheduleRequest) -> AsyncIterator[None]:
        await self.acquire(request)
        try:
            yield
        finally:
            await self.release(request)

    async def acquire(self, request: ScheduleRequest) -> None:
        async with self._condition:
            self._waiting.append(request)
            try:
                await self._condition.wait_for(lambda: self._can_start(request))
            except anyio.get_cancelled_exc_class():
                self._waiting.remove(request)
                self._condition.notify_all()
                raise
            self._waiting.remove(request)
            self._active.add(request)
            if request.locks_workspace:
                self._writers.add(request.workspace)
            self._condition.notify_all()

    async def release(self, request: ScheduleRequest) -> None:
        async with self._condition:
            if request not in self._active:
                return
            self._active.remove(request)
            if request.locks_workspace:
                self._writers.remove(request.workspace)
            self._condition.notify_all()

    def _can_start(self, request: ScheduleRequest) -> bool:
        if len(self._active) >= self._global_limit or not self._resources_available(request):
            return False
        for waiting in self._waiting:
            if self._resources_available(waiting):
                return waiting == request
        return False  # pragma: no cover - the current request is always present in the waiting queue

    def _resources_available(self, request: ScheduleRequest) -> bool:
        return not request.locks_workspace or request.workspace not in self._writers
