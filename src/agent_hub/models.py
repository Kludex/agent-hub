from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

AgentState = Literal["starting", "idle", "running", "parked", "stopping", "stopped", "failed"]
RunState = Literal["queued", "running", "succeeded", "failed", "aborted"]
TERMINAL_RUN_STATES: frozenset[RunState] = frozenset({"succeeded", "failed", "aborted"})


def now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class AgentRecord:
    id: str
    runtime: str
    profile: str
    cwd: str
    access: str
    state: AgentState
    keep_alive: bool
    isolated: bool = False
    detached: bool = False
    depth: int = 0
    parent_agent_id: str | None = None
    root_session_id: str | None = None
    restoration: dict[str, Any] = field(default_factory=dict[str, Any])
    created_at: str = field(default_factory=now)
    updated_at: str = field(default_factory=now)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RunRecord:
    id: str
    agent_id: str
    state: RunState
    prompt: str
    created_at: str = field(default_factory=now)
    started_at: str | None = None
    settled_at: str | None = None
    result: str | None = None
    usage: dict[str, Any] = field(default_factory=dict[str, Any])
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class EventRecord:
    sequence: int
    timestamp: str
    type: str
    agent_id: str | None
    run_id: str | None
    data: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)
