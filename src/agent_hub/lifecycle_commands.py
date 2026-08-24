from __future__ import annotations

from typing import TYPE_CHECKING, Any

from agent_hub.agent_commands import abort
from agent_hub.isolation import IsolationFailure
from agent_hub.protocol import RPCError
from agent_hub.registry import LiveAgent
from agent_hub.runtimes.base import RuntimeFailure
from agent_hub.scheduler import ScheduleRequest

if TYPE_CHECKING:
    from agent_hub.manager import AgentManager


async def stop(manager: AgentManager, params: dict[str, Any]) -> dict[str, Any]:
    agent = await manager._agent_param(params)
    children = await manager.repository.list_agents(parent_agent_id=agent.id)
    for child in children:
        if not child.detached and child.state != "stopped":
            await stop(manager, {"agentId": child.id})
    await abort(manager, {"agentId": agent.id})
    live = manager.live.remove(agent.id)
    await manager._set_agent_state(agent.id, "stopping")
    if live is not None:
        await manager._close_live(live)
    await manager._set_agent_state(agent.id, "stopped")
    return {"stopped": True}


async def park(manager: AgentManager, params: dict[str, Any]) -> dict[str, Any]:
    agent = await manager._agent_param(params)
    if agent.state != "idle":
        raise RPCError(-32004, "Only an idle agent can be parked")
    live_runtime = manager.live.get(agent.id)
    if live_runtime is not None and not live_runtime.runtime.is_resumable(agent):
        raise RPCError(-32006, "Agent runtime does not support parking")
    live = manager.live.remove(agent.id)
    if live is None:  # pragma: no cover - idle state requires a registered live runtime
        raise RPCError(-32005, "Agent runtime is unavailable")
    await manager._close_live(live)
    await manager._set_agent_state(agent.id, "parked")
    return {"parked": True}


async def revive(manager: AgentManager, params: dict[str, Any]) -> dict[str, Any]:
    agent = await manager._agent_param(params)
    if agent.state != "parked":
        raise RPCError(-32004, "Only a parked agent can be revived")
    profile = manager._agent_profile(agent)
    runtime = manager.runtimes[agent.runtime]
    request = manager._start_request(agent, profile, None)
    try:
        handle = await runtime.restore(agent, request)
    except RuntimeFailure as exc:
        await manager._set_agent_state(agent.id, "failed")
        raise RPCError(-32006, str(exc)) from exc
    event_task = manager._background(manager._pump_events, agent.id, runtime, handle)
    live = LiveAgent(runtime, handle, event_task)
    manager.live.add(agent.id, live)
    await manager._set_agent_state(agent.id, "idle")
    manager._schedule_idle(agent.id, profile, live)
    return {"revived": True}


async def patch(manager: AgentManager, params: dict[str, Any]) -> dict[str, Any]:
    agent = await manager._isolated_agent(params)
    try:
        patch, truncated = await manager.isolation.inspect(agent, manager.config.max_output_bytes)
    except (IsolationFailure, OSError) as exc:
        raise RPCError(-32010, str(exc)) from exc
    return {"patch": patch, "truncated": truncated}


async def apply(manager: AgentManager, params: dict[str, Any]) -> dict[str, Any]:
    agent = await manager._isolated_agent(params)
    if await manager.repository.list_runs(agent.id, active_only=True):
        raise RPCError(-32004, "Cannot apply changes while the agent is running")
    schedule = ScheduleRequest(f"apply:{agent.id}", agent.cwd, "shared-write", False)
    try:
        async with manager.scheduler.slot(schedule):
            await manager.isolation.apply(agent)
    except (IsolationFailure, OSError) as exc:
        raise RPCError(-32010, str(exc)) from exc
    return {"applied": True}


async def discard(manager: AgentManager, params: dict[str, Any]) -> dict[str, Any]:
    agent = await manager._isolated_agent(params)
    if manager.live.get(agent.id) is not None or await manager.repository.list_runs(agent.id, active_only=True):
        raise RPCError(-32004, "Stop the agent before discarding its worktree")
    try:
        await manager.isolation.discard(agent)
    except (IsolationFailure, OSError) as exc:
        raise RPCError(-32010, str(exc)) from exc
    return {"discarded": True}
