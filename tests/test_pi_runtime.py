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

from agent_hub.app import create_app
from agent_hub.cli import bind_socket
from agent_hub.config import AgentProfile, HubConfig
from agent_hub.runtimes.pi import PiRuntime
from tests.conftest import rpc_request, serve_uvicorn


@dataclass
class PiHub:
    client: httpx2.AsyncClient

    async def rpc(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return await rpc_request(self.client, method, params)

    async def wait(self, run_id: str) -> dict[str, Any]:
        result = await self.rpc("run.wait", {"runId": run_id, "timeoutSeconds": 3})
        if "run" not in result:  # pragma: no cover - reports test infrastructure failure
            raise AssertionError(repr(result))
        return dict(result["run"])


@pytest.fixture
async def pi_hub(tmp_path: Path) -> AsyncIterator[PiHub]:
    executable = Path(__file__).parent / "fixtures" / "fake_pi.py"
    executable.chmod(0o700)
    socket_directory = Path("/tmp") / f"ah-pi-{uuid.uuid4().hex}"
    config = HubConfig(
        data_dir=tmp_path,
        socket_path=socket_directory / "hub.sock",
        profiles={
            "task": AgentProfile(name="task", allow_model_override=True),
            "sticky": AgentProfile(name="sticky", keep_alive=True, idle_timeout_seconds=60),
            "instructed": AgentProfile(name="instructed", instructions="Follow the profile instructions."),
        },
    )
    app = create_app(
        config,
        {
            "pi": PiRuntime(
                str(executable),
                shutdown_grace_seconds=0.01,
                process_shutdown_seconds=0.05,
                socket_path=config.socket_path,
                max_record_bytes=1024,
                max_stderr_bytes=5,
            )
        },
    )
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
        yield PiHub(client)
    finally:
        await client.aclose()
        server.should_exit = True
        await task_group.__aexit__(None, None, None)
        listener.close()
        config.socket_path.unlink(missing_ok=True)
        socket_directory.rmdir()


@pytest.mark.anyio
async def test_pi_runtime_streams_and_settles_on_agent_settled(pi_hub: PiHub, tmp_path: Path) -> None:
    prompt = "hello\u2028world"
    spawned = await pi_hub.rpc(
        "agent.spawn",
        {"prompt": prompt, "cwd": str(tmp_path), "model": "fixture-model", "access": "read-only"},
    )

    run = await pi_hub.wait(spawned["runId"])
    detail = await pi_hub.rpc("agent.get", {"agentId": spawned["agentId"]})
    events = detail["events"]

    assert run["state"] == "succeeded"
    assert run["result"] == f"result:{prompt}"
    assert run["usage"]["tokens"]["total"] == 6
    assert any(event["type"] == "run.output.delta" and "\u2028" in event["data"]["text"] for event in events)
    assert any(event["type"] == "run.tool.started" for event in events)
    assert any(event["type"] == "run.tool.updated" for event in events)
    assert any(event["type"] == "run.tool.finished" for event in events)
    assert any(event["type"] == "runtime.stderr" for event in events)


@pytest.mark.anyio
async def test_pi_runtime_applies_profile_instructions(pi_hub: PiHub, tmp_path: Path) -> None:
    spawned = await pi_hub.rpc(
        "agent.spawn",
        {"profile": "instructed", "prompt": "profile-instructions", "cwd": str(tmp_path)},
    )

    run = await pi_hub.wait(spawned["runId"])

    assert run["result"] == "result:profile-instructions:Follow the profile instructions."


@pytest.mark.anyio
@pytest.mark.parametrize(
    "prompt",
    [
        "malformed",
        "non-object",
        "oversized",
        "oversized-no-newline",
        "oversized-combined",
        "no-response",
        "incomplete",
        "crash",
    ],
)
async def test_pi_runtime_reports_protocol_and_process_failures(pi_hub: PiHub, tmp_path: Path, prompt: str) -> None:
    spawned = await pi_hub.rpc("agent.spawn", {"prompt": prompt, "cwd": str(tmp_path)})

    run = await pi_hub.wait(spawned["runId"])

    assert run["state"] == "failed"
    assert run["error"]


@pytest.mark.anyio
async def test_pi_runtime_aborts_and_restores_a_session(pi_hub: PiHub, tmp_path: Path) -> None:
    waiting = await pi_hub.rpc(
        "agent.spawn",
        {"profile": "sticky", "prompt": "stubborn", "cwd": str(tmp_path)},
    )
    for _ in range(100):
        detail = await pi_hub.rpc("agent.get", {"agentId": waiting["agentId"]})
        if detail["agent"]["state"] == "running":
            break
        await anyio.sleep(0.01)
    assert await pi_hub.rpc("agent.follow_up", {"agentId": waiting["agentId"], "message": "later"}) == {
        "accepted": True
    }
    rejected = await pi_hub.rpc("agent.steer", {"agentId": waiting["agentId"], "message": "reject"})
    assert rejected["error"]["code"] == -32011
    await pi_hub.rpc("agent.abort", {"agentId": waiting["agentId"]})
    assert (await pi_hub.wait(waiting["runId"]))["state"] == "aborted"
    assert await pi_hub.rpc("agent.stop", {"agentId": waiting["agentId"]}) == {"stopped": True}

    completed = await pi_hub.rpc(
        "agent.spawn",
        {"profile": "sticky", "prompt": "persist", "cwd": str(tmp_path)},
    )
    await pi_hub.wait(completed["runId"])
    assert await pi_hub.rpc("agent.park", {"agentId": completed["agentId"]}) == {"parked": True}
    assert await pi_hub.rpc("agent.revive", {"agentId": completed["agentId"]}) == {"revived": True}
    crashed = await pi_hub.rpc("agent.prompt", {"agentId": completed["agentId"], "prompt": "crash"})
    assert (await pi_hub.wait(crashed["runId"]))["state"] == "failed"
    detail = await pi_hub.rpc("agent.get", {"agentId": completed["agentId"]})
    assert detail["agent"]["state"] == "parked"
    assert await pi_hub.rpc("agent.revive", {"agentId": completed["agentId"]}) == {"revived": True}
    assert await pi_hub.rpc("agent.stop", {"agentId": completed["agentId"]}) == {"stopped": True}


@pytest.mark.anyio
@pytest.mark.parametrize("prompt", ["retry", "thinking", "crlf", "stderr-overflow"])
async def test_pi_runtime_waits_through_retries_and_streams_thinking(
    pi_hub: PiHub,
    tmp_path: Path,
    prompt: str,
) -> None:
    spawned = await pi_hub.rpc("agent.spawn", {"prompt": prompt, "cwd": str(tmp_path)})

    run = await pi_hub.wait(spawned["runId"])
    detail = await pi_hub.rpc("agent.get", {"agentId": spawned["agentId"]})

    assert run["state"] == "succeeded"
    if prompt == "thinking":
        assert any(event["type"] == "run.thinking.delta" for event in detail["events"])
