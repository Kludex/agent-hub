from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import anyio
import logfire
from anyio.streams.memory import MemoryObjectSendStream

from agent_hub.models import EventRecord
from agent_hub.persistence import Repository


class EventJournal:
    def __init__(self, repository: Repository, queue_size: int) -> None:
        self._repository = repository
        self._queue_size = queue_size
        self._subscribers: set[MemoryObjectSendStream[EventRecord]] = set()
        self._lock = anyio.Lock()

    async def emit(
        self,
        event_type: str,
        *,
        agent_id: str | None = None,
        run_id: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> EventRecord:
        async with self._lock:
            event = await self._repository.append_event(event_type, agent_id, run_id, data or {})
            stale: list[MemoryObjectSendStream[EventRecord]] = []
            for stream in self._subscribers:
                try:
                    stream.send_nowait(event)
                except anyio.WouldBlock:
                    stale.append(stream)
            for stream in stale:
                logfire.info(
                    "Disconnected an event subscriber at sequence {sequence} because its queue was full",
                    sequence=event.sequence,
                )
                self._subscribers.remove(stream)
                await stream.aclose()
            return event

    async def stream(self, after: int) -> AsyncIterator[EventRecord]:
        send, receive = anyio.create_memory_object_stream[EventRecord](self._queue_size)
        async with self._lock:
            backlog = await self._repository.events_after(after)
            self._subscribers.add(send)
        try:
            for event in backlog:
                yield event
            async with receive:
                async for event in receive:
                    yield event
        finally:
            self._subscribers.discard(send)
            await send.aclose()
