from __future__ import annotations

from typing import TYPE_CHECKING, Any

import anyio

from agent_hub.models import TERMINAL_RUN_STATES
from agent_hub.protocol import RPCError

if TYPE_CHECKING:
    from agent_hub.manager import AgentManager


async def snapshot(manager: AgentManager, params: dict[str, Any]) -> dict[str, Any]:
    agents = await manager.repository.list_agents()
    runs = await manager.repository.list_runs(active_only=True)
    active_by_agent = {run.agent_id: run for run in runs}
    agent_values: list[dict[str, Any]] = []
    for agent in agents:
        value = agent.as_dict()
        if agent.id in active_by_agent:
            events = await manager.repository.events_for_agent(agent.id)
            active_tools: dict[str, Any] = {}
            for event in events:
                if not event.type.startswith("run.tool."):
                    continue
                tool_id = str(event.data.get("toolCallId", event.data.get("toolName", "tool")))
                if event.type == "run.tool.finished":
                    active_tools.pop(tool_id, None)
                else:
                    active_tools[tool_id] = event.data.get("toolName")
            if active_tools:
                value["current_tool"] = next(reversed(active_tools.values()))
        agent_values.append(value)
    return {
        "agents": agent_values,
        "activeRuns": [run.as_dict() for run in runs],
        "latestSequence": await manager.repository.latest_sequence(),
    }


async def list_agents(manager: AgentManager, params: dict[str, Any]) -> dict[str, Any]:
    state = params.get("state")
    if state is not None and state not in {
        "starting",
        "idle",
        "running",
        "parked",
        "stopping",
        "stopped",
        "failed",
    }:
        raise RPCError(-32602, "Invalid agent state")
    parent_id = params.get("parentAgentId")
    if parent_id is not None and not isinstance(parent_id, str):
        raise RPCError(-32602, "parentAgentId must be a string")
    agents = await manager.repository.list_agents(state, parent_id)
    return {"agents": [agent.as_dict() for agent in agents]}


async def get_agent(manager: AgentManager, params: dict[str, Any]) -> dict[str, Any]:
    agent = await manager._agent_param(params)
    runs = await manager.repository.list_runs(agent.id)
    events = await manager.repository.events_for_agent(agent.id)
    return {
        "agent": agent.as_dict(),
        "runs": [run.as_dict() for run in runs],
        "events": [event.as_dict() for event in events],
    }


async def get_run(manager: AgentManager, params: dict[str, Any]) -> dict[str, Any]:
    run = await manager._run_param(params)
    return {"run": run.as_dict()}


async def wait_run(manager: AgentManager, params: dict[str, Any]) -> dict[str, Any]:
    run = await manager._run_param(params)
    if run.state not in TERMINAL_RUN_STATES:
        waiter = manager._run_waiters.setdefault(run.id, anyio.Event())
        timeout = params.get("timeoutSeconds")
        if timeout is not None and (not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout <= 0):
            raise RPCError(-32602, "timeoutSeconds must be positive")
        try:
            if isinstance(timeout, (int, float)):
                with anyio.fail_after(timeout):
                    await waiter.wait()
            else:
                await waiter.wait()
        except TimeoutError as exc:
            raise RPCError(-32007, "Timed out waiting for run") from exc
        run = await manager._run_param(params)
    return {"run": run.as_dict()}
