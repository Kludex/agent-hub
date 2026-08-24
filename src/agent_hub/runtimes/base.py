from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from agent_hub.config import AgentProfile
from agent_hub.models import AgentRecord


class RuntimeFailure(RuntimeError):
    """A runtime failed to start, communicate, or complete a run."""


@dataclass(frozen=True)
class StartAgentRequest:
    agent_id: str
    profile: AgentProfile
    cwd: Path
    session_directory: Path
    model: str | None = None
    access: str | None = None


@dataclass(frozen=True)
class StartRunRequest:
    run_id: str
    prompt: str


@dataclass(frozen=True)
class RuntimeEvent:
    type: str
    data: dict[str, Any] = field(default_factory=dict[str, Any])


@dataclass(frozen=True)
class RuntimeResult:
    text: str
    usage: dict[str, Any] = field(default_factory=dict[str, Any])
    restoration: dict[str, Any] = field(default_factory=dict[str, Any])


class AgentRuntime(Protocol):
    async def open(self) -> None: ...

    async def close(self) -> None: ...

    async def start(self, request: StartAgentRequest) -> object: ...

    async def prompt(self, handle: object, request: StartRunRequest) -> RuntimeResult: ...

    def events(self, handle: object) -> AsyncIterator[RuntimeEvent]: ...

    async def steer(self, handle: object, message: str) -> None: ...

    async def follow_up(self, handle: object, message: str) -> None: ...

    async def abort(self, handle: object) -> None: ...

    async def stop(self, handle: object) -> None: ...

    def is_resumable(self, agent: AgentRecord) -> bool: ...

    async def restore(self, agent: AgentRecord, request: StartAgentRequest) -> object: ...
