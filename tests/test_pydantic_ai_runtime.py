from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import anyio
import httpx2
import pytest
import uvicorn
from pydantic_ai.models.test import TestModel
from pydantic_ai.toolsets import FunctionToolset

from agent_hub.app import create_app
from agent_hub.cli import bind_socket
from agent_hub.config import AgentProfile, HubConfig, UsageLimitSettings
from agent_hub.runtimes.pydantic_ai import PydanticAIRuntime
from tests.conftest import rpc_request, serve_uvicorn


async def echo(value: str) -> str:
    return f"echo:{value}"


@dataclass
class PydanticHub:
    client: httpx2.AsyncClient

    async def rpc(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return await rpc_request(self.client, method, params)

    async def wait(self, run_id: str) -> dict[str, Any]:
        result = await self.rpc("run.wait", {"runId": run_id, "timeoutSeconds": 3})
        return dict(result["run"])


@pytest.fixture
async def pydantic_hub(tmp_path: Path) -> AsyncIterator[PydanticHub]:
    socket_directory = Path("/tmp") / f"ah-pai-{uuid.uuid4().hex}"
    agent_spec = tmp_path / "agent-spec.yaml"
    agent_spec.write_text(
        "name: spec-agent\nmodel: plain-model\ninstructions: Answer from the AgentSpec.\nretries: 2\n",
        encoding="utf-8",
    )
    config = HubConfig(
        data_dir=tmp_path,
        socket_path=socket_directory / "hub.sock",
        profiles={
            "plain": AgentProfile(name="plain", runtime="pydantic-ai", model="plain-model"),
            "spec": AgentProfile(name="spec", runtime="pydantic-ai", agent_spec=agent_spec),
            "brief": AgentProfile(
                name="brief",
                runtime="pydantic-ai",
                model="plain-model",
                keep_alive=True,
                idle_timeout_seconds=0.01,
            ),
            "sticky": AgentProfile(
                name="sticky",
                runtime="pydantic-ai",
                model="plain-model",
                keep_alive=True,
                idle_timeout_seconds=60,
            ),
            "workspace-writer": AgentProfile(
                name="workspace-writer",
                runtime="pydantic-ai",
                model="write-model",
                tools=("write_file",),
            ),
            "tools": AgentProfile(
                name="tools",
                runtime="pydantic-ai",
                model="tool-model",
                tools=("echo",),
            ),
            "read-only-tools": AgentProfile(
                name="read-only-tools",
                runtime="pydantic-ai",
                model="tool-model",
                access="read-only",
                tools=("echo",),
            ),
            "mcp": AgentProfile(
                name="mcp",
                runtime="pydantic-ai",
                model="tool-model",
                mcp_servers=("fixture",),
            ),
            "limited": AgentProfile(
                name="limited",
                runtime="pydantic-ai",
                model="plain-model",
                usage_limits=UsageLimitSettings(total_tokens_limit=1),
            ),
        },
    )
    runtime = PydanticAIRuntime(
        tools={"echo": echo},
        models={
            "plain-model": TestModel(custom_output_text="Pydantic result"),
            "tool-model": TestModel(call_tools=["echo"]),
            "write-model": TestModel(call_tools=["write_file"]),
        },
        mcp_servers={"fixture": FunctionToolset([echo])},
    )
    app = create_app(config, {"pydantic-ai": runtime})
    if config.socket_path is None:  # pragma: no cover - Pydantic Settings guarantees the path
        raise AssertionError("HubConfig did not create a socket path")
    listener = bind_socket(config.socket_path)
    server = uvicorn.Server(uvicorn.Config(app, http="zttp", log_config=None, access_log=False, lifespan="on"))
    task_group = anyio.create_task_group()
    await task_group.__aenter__()
    task_group.start_soon(serve_uvicorn, server, listener)
    client = httpx2.AsyncClient(
        transport=httpx2.AsyncHTTPTransport(uds=str(config.socket_path)),
        base_url="http://agent-hub",
    )
    for _ in range(100):
        try:
            if (await client.get("/health")).status_code == 200:
                break
        except httpx2.ConnectError:  # pragma: no cover - startup timing depends on the host
            await anyio.sleep(0.01)
    else:  # pragma: no cover - reports test infrastructure failure
        raise RuntimeError("Agent Hub test server did not start")
    try:
        yield PydanticHub(client)
    finally:
        await client.aclose()
        server.should_exit = True
        await task_group.__aexit__(None, None, None)
        listener.close()
        config.socket_path.unlink(missing_ok=True)
        socket_directory.rmdir()


@pytest.mark.anyio
async def test_pydantic_ai_agent_spec_constructs_the_agent(pydantic_hub: PydanticHub, tmp_path: Path) -> None:
    spawned = await pydantic_hub.rpc(
        "agent.spawn",
        {"profile": "spec", "prompt": "hello", "cwd": str(tmp_path)},
    )

    run = await pydantic_hub.wait(spawned["runId"])

    assert run["state"] == "succeeded"
    assert run["result"] == "Pydantic result"


@pytest.mark.anyio
async def test_pydantic_ai_output_and_usage_are_normalized(pydantic_hub: PydanticHub, tmp_path: Path) -> None:
    spawned = await pydantic_hub.rpc(
        "agent.spawn",
        {"profile": "plain", "prompt": "hello", "cwd": str(tmp_path)},
    )

    run = await pydantic_hub.wait(spawned["runId"])
    detail = await pydantic_hub.rpc("agent.get", {"agentId": spawned["agentId"]})

    assert run["state"] == "succeeded"
    assert run["result"] == "Pydantic result"
    assert "totalTokens" in run["usage"]
    assert any(event["type"] == "run.output.delta" for event in detail["events"])
    assert any(event["type"] == "run.usage.updated" for event in detail["events"])


@pytest.mark.anyio
async def test_non_resumable_pydantic_agent_stops_after_idle_timeout(
    pydantic_hub: PydanticHub,
    tmp_path: Path,
) -> None:
    spawned = await pydantic_hub.rpc(
        "agent.spawn",
        {"profile": "brief", "prompt": "short", "cwd": str(tmp_path)},
    )
    await pydantic_hub.wait(spawned["runId"])
    for _ in range(100):
        detail = await pydantic_hub.rpc("agent.get", {"agentId": spawned["agentId"]})
        if detail["agent"]["state"] == "stopped":
            break
        await anyio.sleep(0.01)
    else:  # pragma: no cover - reports test infrastructure failure
        raise AssertionError("Pydantic AI agent did not stop")


@pytest.mark.anyio
async def test_pydantic_ai_conversation_continues_in_memory(pydantic_hub: PydanticHub, tmp_path: Path) -> None:
    first = await pydantic_hub.rpc(
        "agent.spawn",
        {"profile": "sticky", "prompt": "first", "cwd": str(tmp_path)},
    )
    assert (await pydantic_hub.wait(first["runId"]))["state"] == "succeeded"

    second = await pydantic_hub.rpc("agent.prompt", {"agentId": first["agentId"], "prompt": "second"})

    assert (await pydantic_hub.wait(second["runId"]))["state"] == "succeeded"
    detail = await pydantic_hub.rpc("agent.get", {"agentId": first["agentId"]})
    assert len(detail["runs"]) == 2
    await pydantic_hub.rpc("agent.stop", {"agentId": first["agentId"]})


@pytest.mark.anyio
async def test_pydantic_ai_conversations_cannot_be_parked_without_durable_restoration(
    pydantic_hub: PydanticHub,
    tmp_path: Path,
) -> None:
    spawned = await pydantic_hub.rpc(
        "agent.spawn",
        {"profile": "sticky", "prompt": "park", "cwd": str(tmp_path)},
    )
    await pydantic_hub.wait(spawned["runId"])
    parked = await pydantic_hub.rpc("agent.park", {"agentId": spawned["agentId"]})

    assert parked["error"]["code"] == -32006
    await pydantic_hub.rpc("agent.stop", {"agentId": spawned["agentId"]})


@pytest.mark.anyio
async def test_pydantic_ai_uses_manager_owned_workspace_tools(
    pydantic_hub: PydanticHub,
    tmp_path: Path,
) -> None:
    spawned = await pydantic_hub.rpc(
        "agent.spawn",
        {"profile": "workspace-writer", "prompt": "write", "cwd": str(tmp_path)},
    )

    run = await pydantic_hub.wait(spawned["runId"])

    assert run["state"] == "succeeded"
    assert (tmp_path / "a").read_text(encoding="utf-8") == "a"


@pytest.mark.anyio
async def test_pydantic_ai_tool_events_are_normalized(pydantic_hub: PydanticHub, tmp_path: Path) -> None:
    spawned = await pydantic_hub.rpc(
        "agent.spawn",
        {"profile": "tools", "prompt": "use the tool", "cwd": str(tmp_path)},
    )

    run = await pydantic_hub.wait(spawned["runId"])
    detail = await pydantic_hub.rpc("agent.get", {"agentId": spawned["agentId"]})
    event_types = [event["type"] for event in detail["events"]]

    assert run["state"] == "succeeded"
    assert "run.tool.started" in event_types
    assert "run.tool.finished" in event_types


@pytest.mark.anyio
async def test_pydantic_ai_enforces_read_only_tool_permissions(
    pydantic_hub: PydanticHub,
    tmp_path: Path,
) -> None:
    spawned = await pydantic_hub.rpc(
        "agent.spawn",
        {"profile": "read-only-tools", "prompt": "write", "cwd": str(tmp_path)},
    )

    run = await pydantic_hub.wait(spawned["runId"])

    assert run["state"] == "failed"
    assert "write-capable" in run["error"]


@pytest.mark.anyio
async def test_pydantic_ai_uses_configured_mcp_toolsets(pydantic_hub: PydanticHub, tmp_path: Path) -> None:
    spawned = await pydantic_hub.rpc(
        "agent.spawn",
        {"profile": "mcp", "prompt": "use MCP", "cwd": str(tmp_path)},
    )

    run = await pydantic_hub.wait(spawned["runId"])
    detail = await pydantic_hub.rpc("agent.get", {"agentId": spawned["agentId"]})

    assert run["state"] == "succeeded"
    assert any(event["type"] == "run.tool.started" for event in detail["events"])


@pytest.mark.anyio
async def test_pydantic_ai_usage_limits_fail_the_run(pydantic_hub: PydanticHub, tmp_path: Path) -> None:
    spawned = await pydantic_hub.rpc(
        "agent.spawn",
        {"profile": "limited", "prompt": "too many tokens", "cwd": str(tmp_path)},
    )

    run = await pydantic_hub.wait(spawned["runId"])

    assert run["state"] == "failed"
    assert "token" in run["error"].lower()
