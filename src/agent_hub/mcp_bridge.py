from __future__ import annotations

import contextlib
import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import anyio
from mcp.server.mcpserver import Context, MCPServer
from mcp.types import ToolAnnotations

from agent_hub.client import HubClient, HubClientError

if TYPE_CHECKING:
    MCPContext = Context[Any, Any]
else:
    MCPContext = Context

AgentState = Literal["starting", "idle", "running", "parked", "stopping", "stopped", "failed"]
AccessMode = Literal["read-only", "shared-write"]


class MCPBridge:
    def __init__(self, client: HubClient, cwd: Path) -> None:
        self.client = client
        self.cwd = cwd.resolve()
        self.root_session_id = f"mcp_{uuid.uuid4().hex}"

    async def task(
        self,
        agent: str,
        prompt: str,
        ctx: MCPContext,
        background: bool = False,
        model: str | None = None,
        access: AccessMode | None = None,
        isolated: bool = False,
        detached: bool = False,
        max_runtime_seconds: float | None = None,
    ) -> dict[str, Any]:
        """Delegate a task to an Agent Hub profile."""
        params: dict[str, Any] = {
            "profile": agent,
            "prompt": prompt,
            "cwd": str(self.cwd),
            "isolated": isolated,
            "detached": detached,
            "rootSessionId": self.root_session_id,
            "idempotencyKey": f"mcp:{self.root_session_id}:{ctx.request_id}:agent.spawn",
        }
        if model is not None:
            params["model"] = model
        if access is not None:
            params["access"] = access
        if max_runtime_seconds is not None:
            params["maxRuntimeSeconds"] = max_runtime_seconds
        spawned = await self.client.rpc("agent.spawn", params)
        agent_id = str(spawned["agentId"])
        run_id = str(spawned["runId"])
        if background:
            return {**spawned, "background": True}
        await ctx.report_progress(0, 1, f"Started Agent Hub run {run_id}")
        try:
            waited = await self.client.rpc("run.wait", {"runId": run_id})
        except anyio.get_cancelled_exc_class():
            with anyio.CancelScope(shield=True):
                with contextlib.suppress(HubClientError):
                    await self.client.rpc(
                        "agent.abort",
                        {
                            "agentId": agent_id,
                            "idempotencyKey": f"mcp:{self.root_session_id}:{ctx.request_id}:agent.abort",
                        },
                    )
            raise
        run = waited.get("run")
        if not isinstance(run, dict):
            raise HubClientError("Agent Hub returned an invalid run")
        await ctx.report_progress(1, 1, f"Agent Hub run {run_id} {run.get('state', 'completed')}")
        if run.get("state") != "succeeded":
            raise HubClientError(str(run.get("error") or f"Delegated run {run.get('state', 'failed')}"))
        return {
            "agentId": agent_id,
            "runId": run_id,
            "background": False,
            "state": run["state"],
            "result": run.get("result", ""),
            "usage": run.get("usage", {}),
        }

    async def agent_list(self, state: AgentState | None = None, parent_agent_id: str | None = None) -> dict[str, Any]:
        """List Agent Hub agents."""
        params: dict[str, Any] = {}
        if state is not None:
            params["state"] = state
        if parent_agent_id is not None:
            params["parentAgentId"] = parent_agent_id
        return await self.client.rpc("agent.list", params)

    async def agent_get(self, agent_id: str) -> dict[str, Any]:
        """Get an agent with its runs and events."""
        return await self.client.rpc("agent.get", {"agentId": agent_id})

    async def agent_stop(self, agent_id: str, ctx: MCPContext) -> dict[str, Any]:
        """Stop an agent and its attached descendants."""
        return await self.client.rpc(
            "agent.stop",
            {
                "agentId": agent_id,
                "idempotencyKey": f"mcp:{self.root_session_id}:{ctx.request_id}:agent.stop",
            },
        )

    async def agent_abort(self, agent_id: str, ctx: MCPContext) -> dict[str, Any]:
        """Abort an agent's active run."""
        return await self.client.rpc(
            "agent.abort",
            {
                "agentId": agent_id,
                "idempotencyKey": f"mcp:{self.root_session_id}:{ctx.request_id}:agent.abort",
            },
        )

    async def run_get(self, run_id: str) -> dict[str, Any]:
        """Get the current state and result of a run."""
        return await self.client.rpc("run.get", {"runId": run_id})

    async def run_wait(self, run_id: str, timeout_seconds: float | None = None) -> dict[str, Any]:
        """Wait for a run to reach a terminal state."""
        params: dict[str, Any] = {"runId": run_id}
        if timeout_seconds is not None:
            params["timeoutSeconds"] = timeout_seconds
        return await self.client.rpc("run.wait", params)


def create_mcp_server(socket_path: Path, cwd: Path | None = None) -> MCPServer[None]:
    client = HubClient(socket_path)
    bridge = MCPBridge(client, cwd or Path.cwd())

    @asynccontextmanager
    async def lifespan(_: MCPServer[None]) -> AsyncGenerator[None]:  # pragma: no cover - verified over stdio
        async with client:
            await client.health()
            yield

    server = MCPServer(
        "agent-hub",
        description="Delegate and manage local coding agents through Agent Hub.",
        instructions="Use task to delegate focused coding, exploration, or review work.",
        version="0.1.0",
        lifespan=lifespan,
    )
    server.add_tool(
        bridge.task,
        annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=True),
    )
    server.add_tool(
        bridge.agent_list,
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False),
    )
    server.add_tool(
        bridge.agent_get,
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False),
    )
    server.add_tool(
        bridge.agent_stop,
        annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=False),
    )
    server.add_tool(
        bridge.agent_abort,
        annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=False),
    )
    server.add_tool(
        bridge.run_get,
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False),
    )
    server.add_tool(
        bridge.run_wait,
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False),
    )
    return server


async def serve_mcp(
    socket_path: Path, cwd: Path | None = None
) -> None:  # pragma: no cover - CLI subprocess entry point
    await create_mcp_server(socket_path, cwd).run_stdio_async()
