from __future__ import annotations

import json
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import anyio
import httpx2
import pytest
from mcp import ClientSession, StdioServerParameters, stdio_client
from mcp.server.mcpserver import Context
from mcp.types import CallToolResult

from agent_hub.client import HubClient, HubClientError
from agent_hub.mcp_bridge import MCPBridge, create_mcp_server
from tests.conftest import RunningHub


def output(result: CallToolResult) -> dict[str, Any]:
    assert result.is_error is False
    structured_content: object = result.structured_content
    assert isinstance(structured_content, dict)
    return cast(dict[str, Any], structured_content)


@pytest.mark.anyio
async def test_stdio_bridge_delegates_and_manages_agents(hub: RunningHub, tmp_path: Path) -> None:
    if hub.config.socket_path is None:  # pragma: no cover - Pydantic Settings configures the socket
        raise AssertionError("HubConfig did not create a socket path")
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "agent_hub.cli", "mcp", "--socket", str(hub.config.socket_path)],
        cwd=tmp_path,
    )
    progress_updates: list[tuple[float, float | None, str | None]] = []

    async def receive_progress(progress: float, total: float | None, message: str | None) -> None:
        progress_updates.append((progress, total, message))

    async with stdio_client(parameters) as streams:
        async with ClientSession(*streams) as session:
            initialized = await session.initialize()
            tools = await session.list_tools()
            delegated = await session.call_tool(
                "task",
                {"agent": "task", "prompt": "inspect"},
                progress_callback=receive_progress,
            )
            assert isinstance(delegated, CallToolResult)
            delegated_output = output(delegated)
            agent_id = str(delegated_output["agentId"])
            run_id = str(delegated_output["runId"])

            listed = await session.call_tool("agent_list", {})
            inspected = await session.call_tool("agent_get", {"agent_id": agent_id})
            run = await session.call_tool("run_get", {"run_id": run_id})
            waited = await session.call_tool("run_wait", {"run_id": run_id, "timeout_seconds": 1})
            stopped = await session.call_tool("agent_stop", {"agent_id": agent_id})
            background = await session.call_tool(
                "task",
                {"agent": "task", "prompt": "block", "background": True, "access": "read-only"},
            )
            assert isinstance(background, CallToolResult)
            background_output = output(background)
            aborted = await session.call_tool("agent_abort", {"agent_id": background_output["agentId"]})
            failed = await session.call_tool("task", {"agent": "task", "prompt": "fail"})

    assert initialized.server_info.name == "agent-hub"
    assert {tool.name for tool in tools.tools} == {
        "task",
        "agent_list",
        "agent_get",
        "agent_stop",
        "agent_abort",
        "run_get",
        "run_wait",
    }
    task_tool = next(tool for tool in tools.tools if tool.name == "task")
    assert "cwd" not in task_tool.input_schema["properties"]
    assert delegated_output == {
        "agentId": agent_id,
        "runId": run_id,
        "background": False,
        "state": "succeeded",
        "result": "result:inspect",
        "usage": {"input": 2, "output": 3, "totalTokens": 5, "cost": 0.01},
    }
    assert progress_updates[0][0] == 0
    assert progress_updates[-1][0] == 1
    assert len(output(listed)["agents"]) == 1
    assert output(inspected)["agent"]["id"] == agent_id
    assert output(run)["run"]["id"] == run_id
    assert output(waited)["run"]["state"] == "succeeded"
    assert output(stopped) == {"stopped": True}
    assert output(aborted)["aborted"] is True
    assert failed.is_error is True
    assert hub.runtime.start_requests[0].cwd == tmp_path


@dataclass
class FakeContext:
    request_id: str = "request"
    progress: list[float] = field(default_factory=list[float])

    async def report_progress(self, progress: float, total: float | None, message: str | None) -> None:
        self.progress.append(progress)


@pytest.mark.anyio
async def test_mcp_bridge_command_boundaries(hub: RunningHub, tmp_path: Path) -> None:
    if hub.config.socket_path is None:  # pragma: no cover - Pydantic Settings configures the socket
        raise AssertionError("HubConfig did not create a socket path")
    server = create_mcp_server(hub.config.socket_path, tmp_path)
    fake_context = FakeContext()
    context = cast(Context[Any, Any], fake_context)
    async with HubClient(hub.config.socket_path) as client:
        bridge = MCPBridge(client, tmp_path)
        completed = await bridge.task(
            "task",
            "direct",
            context,
            model="test-model",
            access="read-only",
            detached=True,
            max_runtime_seconds=2,
        )
        agent_id = str(completed["agentId"])
        run_id = str(completed["runId"])
        assert await bridge.agent_list("stopped", agent_id) == {"agents": []}
        assert (await bridge.agent_get(agent_id))["agent"]["id"] == agent_id
        assert (await bridge.run_get(run_id))["run"]["state"] == "succeeded"
        assert (await bridge.run_wait(run_id))["run"]["state"] == "succeeded"
        fake_context.request_id = "stop"
        assert await bridge.agent_stop(agent_id, context) == {"stopped": True}

        fake_context.request_id = "background"
        background = await bridge.task("task", "block", context, background=True)
        fake_context.request_id = "abort"
        assert await bridge.agent_abort(str(background["agentId"]), context) == {
            "aborted": True,
            "runId": background["runId"],
        }
        assert (await bridge.run_wait(str(background["runId"]), 2))["run"]["state"] == "aborted"

        fake_context.request_id = "fail"
        with pytest.raises(HubClientError, match="requested failure"):
            await bridge.task("task", "fail", context)

        fake_context.request_id = "cancel"
        with anyio.move_on_after(0.05) as cancellation:
            await bridge.task("task", "block", context)
        assert cancellation.cancelled_caught

    assert server.name == "agent-hub"
    assert fake_context.progress == [0, 1, 0, 1, 0]
    assert hub.runtime.start_requests[0].model == "test-model"


@pytest.mark.anyio
async def test_hub_client_validates_daemon_responses(tmp_path: Path) -> None:
    async def verify(
        response: Callable[[httpx2.Request], httpx2.Response],
        call: Callable[[HubClient], Awaitable[object]],
        message: str,
    ) -> None:
        client = HubClient(tmp_path / "hub.sock", httpx2.MockTransport(response))
        with pytest.raises((HubClientError, RuntimeError), match=message):
            async with client:
                await call(client)

    unopened = HubClient(tmp_path / "hub.sock")
    with pytest.raises(RuntimeError, match="not open"):
        await unopened.rpc("hub.snapshot")

    await verify(
        lambda _request: httpx2.Response(200, json={"status": "nope"}),
        lambda client: client.health(),
        "invalid health",
    )
    await verify(
        lambda _request: httpx2.Response(500),
        lambda client: client.rpc("hub.snapshot"),
        "unavailable",
    )
    await verify(
        lambda _request: httpx2.Response(200, content=b"not-json\n"),
        lambda client: client.rpc("hub.snapshot"),
        "invalid JSON-RPC response",
    )
    await verify(
        lambda _request: httpx2.Response(200, json={"jsonrpc": "2.0", "id": "different", "result": {}}),
        lambda client: client.rpc("hub.snapshot"),
        "no matching",
    )

    def rpc_error(request: httpx2.Request) -> httpx2.Response:
        request_id = json.loads(request.content)["id"]
        return httpx2.Response(
            200,
            json={"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": "missing"}},
        )

    await verify(rpc_error, lambda client: client.rpc("missing"), r"missing \(-32601\)")

    def invalid_result(request: httpx2.Request) -> httpx2.Response:
        request_id = json.loads(request.content)["id"]
        return httpx2.Response(200, json={"jsonrpc": "2.0", "id": request_id, "result": []})

    await verify(invalid_result, lambda client: client.rpc("hub.snapshot"), "invalid JSON-RPC result")

    def invalid_run(request: httpx2.Request) -> httpx2.Response:
        payload = json.loads(request.content)
        result: dict[str, Any] = (
            {"agentId": "agt_invalid", "runId": "run_invalid"} if payload["method"] == "agent.spawn" else {"run": []}
        )
        return httpx2.Response(200, json={"jsonrpc": "2.0", "id": payload["id"], "result": result})

    client = HubClient(tmp_path / "hub.sock", httpx2.MockTransport(invalid_run))
    context = cast(Context[Any, Any], FakeContext())
    async with client:
        with pytest.raises(HubClientError, match="invalid run"):
            await MCPBridge(client, tmp_path).task("task", "invalid", context)
