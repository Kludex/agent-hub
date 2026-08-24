from __future__ import annotations

from pathlib import Path

import anyio
import pytest

from agent_hub.events import EventJournal
from agent_hub.models import EventRecord
from agent_hub.persistence import Repository


@pytest.mark.anyio
async def test_replays_events_and_disconnects_a_slow_subscriber(tmp_path: Path) -> None:
    repository = Repository(tmp_path / "events.sqlite3")
    await repository.open()
    journal = EventJournal(repository, queue_size=1)
    await journal.emit("before", data={"value": 1})

    replay = journal.stream(0)
    assert (await anext(replay)).type == "before"
    received_send, received = anyio.create_memory_object_stream[EventRecord](1)

    async def receive_first() -> None:
        await received_send.send(await anext(replay))

    async with anyio.create_task_group() as task_group:
        task_group.start_soon(receive_first)
        await journal.emit("first")
        first = await received.receive()
        assert first.type == "first"
    await journal.emit("second")
    await journal.emit("overflow")

    assert (await anext(replay)).type == "second"
    with pytest.raises(StopAsyncIteration):
        await anext(replay)
    await received_send.aclose()
    await received.aclose()
    await repository.close()
