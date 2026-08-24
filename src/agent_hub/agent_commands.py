from __future__ import annotations

from typing import TYPE_CHECKING, Any

from agent_hub.config import load_profiles
from agent_hub.models import AgentRecord, RunRecord
from agent_hub.protocol import RPCError
from agent_hub.runtimes.base import RuntimeFailure
from agent_hub.security import redact_text

if TYPE_CHECKING:
    from agent_hub.manager import AgentManager


async def spawn(manager: AgentManager, params: dict[str, Any]) -> dict[str, Any]:
    cwd = manager._working_directory(params.get("cwd"))
    profiles = load_profiles(manager.config, cwd)
    profile_name = params.get("profile", params.get("agent", "task"))
    if not isinstance(profile_name, str) or profile_name not in profiles:
        raise RPCError(-32001, "Agent profile not found", {"profile": profile_name})
    profile = manager._profile_with_timeout(profiles[profile_name], params.get("maxRuntimeSeconds"))
    runtime_name = params.get("runtime", profile.runtime)
    if not isinstance(runtime_name, str):
        raise RPCError(-32602, "runtime must be a string")
    if runtime_name not in manager.runtimes:
        raise RPCError(-32002, "Agent runtime not available", {"runtime": runtime_name})
    parent_id, root_session_id, depth = await manager._lineage(params)
    if depth > manager.config.recursion_limit:
        raise RPCError(-32003, "Delegation recursion limit exceeded")
    access = params.get("access", profile.access)
    if access not in {"read-only", "shared-write"}:
        raise RPCError(-32602, "Invalid access mode")
    if profile.access == "read-only" and access == "shared-write":
        raise RPCError(-32602, "Read-only profiles cannot be escalated to shared-write")
    isolated = params.get("isolated", False)
    if not isinstance(isolated, bool):
        raise RPCError(-32602, "isolated must be a boolean")
    detached = params.get("detached", False)
    if not isinstance(detached, bool):
        raise RPCError(-32602, "detached must be a boolean")
    agent_id = manager._id("agt")
    run_id = manager._id("run")
    agent = AgentRecord(
        id=agent_id,
        runtime=runtime_name,
        profile=profile_name,
        cwd=str(cwd),
        access=access,
        state="starting",
        keep_alive=profile.keep_alive,
        isolated=isolated,
        detached=detached,
        depth=depth,
        parent_agent_id=parent_id,
        root_session_id=root_session_id,
    )
    prompt = params.get("prompt")
    if not isinstance(prompt, str) or not prompt:
        raise RPCError(-32602, "prompt must be a non-empty string")
    run = RunRecord(id=run_id, agent_id=agent_id, state="queued", prompt=redact_text(prompt))
    manager._run_prompts[run_id] = prompt
    await manager.repository.create_agent(agent)
    await manager.repository.create_run(run)
    await manager.journal.emit("agent.created", agent_id=agent_id, data=agent.as_dict())
    await manager.journal.emit("run.created", agent_id=agent_id, run_id=run_id, data=run.as_dict())
    manager._start_run(run, profile, manager._model_override(profile, params.get("model")), isolated)
    return {"agentId": agent_id, "runId": run_id}


async def prompt(manager: AgentManager, params: dict[str, Any]) -> dict[str, Any]:
    agent = await manager._agent_param(params)
    if agent.state != "idle" or manager.live.get(agent.id) is None:
        raise RPCError(-32004, "Agent is not idle")
    prompt = params.get("prompt")
    if not isinstance(prompt, str) or not prompt:
        raise RPCError(-32602, "prompt must be a non-empty string")
    requested_isolation = params.get("isolated")
    if requested_isolation is not None:
        if not isinstance(requested_isolation, bool):
            raise RPCError(-32602, "isolated must be a boolean")
        if requested_isolation != agent.isolated:
            raise RPCError(-32602, "A live agent cannot change its isolation mode")
    run = RunRecord(id=manager._id("run"), agent_id=agent.id, state="queued", prompt=redact_text(prompt))
    manager._run_prompts[run.id] = prompt
    await manager.repository.create_run(run)
    await manager.journal.emit("run.created", agent_id=agent.id, run_id=run.id, data=run.as_dict())
    profile = manager._profile_with_timeout(manager._agent_profile(agent), params.get("maxRuntimeSeconds"))
    manager._start_run(
        run,
        profile,
        manager._model_override(profile, params.get("model")),
        agent.isolated,
        reuse=True,
    )
    return {"agentId": agent.id, "runId": run.id}


async def steer(manager: AgentManager, params: dict[str, Any]) -> dict[str, Any]:
    live = await manager._running_live(params)
    message = manager._message(params)
    try:
        await live.runtime.steer(live.handle, message)
    except RuntimeFailure as exc:
        raise RPCError(-32011, redact_text(str(exc))) from exc
    return {"accepted": True}


async def follow_up(manager: AgentManager, params: dict[str, Any]) -> dict[str, Any]:
    live = await manager._running_live(params)
    message = manager._message(params)
    try:
        await live.runtime.follow_up(live.handle, message)
    except RuntimeFailure as exc:
        raise RPCError(-32011, redact_text(str(exc))) from exc
    return {"accepted": True}


async def abort(manager: AgentManager, params: dict[str, Any]) -> dict[str, Any]:
    agent = await manager._agent_param(params)
    runs = await manager.repository.list_runs(agent.id, active_only=True)
    if not runs:
        return {"aborted": False}
    run = runs[-1]
    manager._aborted_runs.add(run.id)
    if run.state == "queued":
        task = manager._run_tasks.get(run.id)
        if task is not None:
            task.cancel()
    else:
        live = manager.live.get(agent.id)
        if live is not None:
            try:
                await live.runtime.abort(live.handle)
            except RuntimeFailure as exc:
                raise RPCError(-32011, redact_text(str(exc))) from exc
    return {"aborted": True, "runId": run.id}
