from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import anyio
from anyio.abc import TaskGroup

from agent_hub.runtimes.base import AgentRuntime


@dataclass
class BackgroundTask:
    scope: anyio.CancelScope
    done: anyio.Event

    def cancel(self) -> None:
        self.scope.cancel()

    async def wait(self) -> None:
        await self.done.wait()


@dataclass
class LiveAgent:
    runtime: AgentRuntime
    handle: object
    event_task: BackgroundTask
    current_run_id: str | None = None
    idle_task: BackgroundTask | None = None


class RuntimeRegistry:
    def __init__(self) -> None:
        self._agents: dict[str, LiveAgent] = {}

    def add(self, agent_id: str, agent: LiveAgent) -> None:
        if agent_id in self._agents:  # pragma: no cover - manager state transitions prevent duplicate handles
            raise RuntimeError(f"Agent {agent_id} already has a live runtime")
        self._agents[agent_id] = agent

    def get(self, agent_id: str) -> LiveAgent | None:
        return self._agents.get(agent_id)

    def remove(self, agent_id: str) -> LiveAgent | None:
        return self._agents.pop(agent_id, None)

    def values(self) -> list[tuple[str, LiveAgent]]:
        return list(self._agents.items())


def start_background(
    task_group: TaskGroup,
    function: Callable[..., Awaitable[None]],
    *args: object,
) -> BackgroundTask:
    scope = anyio.CancelScope()
    done = anyio.Event()

    async def run() -> None:
        try:
            with scope:
                await function(*args)
        finally:
            done.set()

    task_group.start_soon(run)
    return BackgroundTask(scope, done)
