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
from agent_hub.models import AgentRecord
from agent_hub.runtimes.base import RuntimeFailure, StartAgentRequest
from agent_hub.runtimes.codepuppy import CodePuppyRuntime
from tests.conftest import rpc_request, serve_uvicorn


@dataclass
class CodePuppyHub:
    client: httpx2.AsyncClient

    async def rpc(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return await rpc_request(self.client, method, params)

    async def wait(self, run_id: str) -> dict[str, Any]:
        result = await self.rpc("run.wait", {"runId": run_id, "timeoutSeconds": 3})
        if "run" not in result:  # pragma: no cover - reports test infrastructure failure
            raise AssertionError(repr(result))
        return dict(result["run"])


@pytest.fixture
async def codepuppy_hub(tmp_path: Path) -> AsyncIterator[CodePuppyHub]:
    executable = Path(__file__).parent / "fixtures" / "fake_codepuppy.py"
    executable.chmod(0o700)
    socket_directory = Path("/tmp") / f"ah-cp-{uuid.uuid4().hex}"
    config = HubConfig(
        data_dir=tmp_path / "data",
        socket_path=socket_directory / "hub.sock",
        profiles={
            "task": AgentProfile(
                name="task",
                runtime="codepuppy",
                model="fixture-model",
                instructions="Follow the fixture instructions.",
                allow_model_override=True,
            ),
            "read": AgentProfile(name="read", runtime="codepuppy", access="read-only"),
            "sticky": AgentProfile(
                name="sticky",
                runtime="codepuppy",
                keep_alive=True,
                idle_timeout_seconds=60,
            ),
        },
    )
    app = create_app(
        config,
        {
            "codepuppy": CodePuppyRuntime(
                str(executable),
                socket_path=config.socket_path,
                process_shutdown_seconds=0.05,
                max_record_bytes=1024,
                max_output_bytes=1024,
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
        yield CodePuppyHub(client)
    finally:
        await client.aclose()
        server.should_exit = True
        await task_group.__aexit__(None, None, None)
        listener.close()
        config.socket_path.unlink(missing_ok=True)
        socket_directory.rmdir()


@pytest.mark.anyio
async def test_codepuppy_runtime_streams_tools_usage_and_workspace_io(
    codepuppy_hub: CodePuppyHub, tmp_path: Path
) -> None:
    spawned = await codepuppy_hub.rpc(
        "agent.spawn",
        {"profile": "task", "prompt": "tools", "cwd": str(tmp_path), "model": "override-model"},
    )

    run = await codepuppy_hub.wait(spawned["runId"])
    detail = await codepuppy_hub.rpc("agent.get", {"agentId": spawned["agentId"]})

    assert run["state"] == "succeeded"
    assert run["result"] == "result:allowed:written:terminal"
    assert run["usage"]["totalTokens"] == 6
    assert (tmp_path / "codepuppy.txt").read_text(encoding="utf-8") == "written"
    assert (tmp_path / ".fixture-codepuppy-model").read_text(encoding="utf-8") == "override-model"
    event_types = {event["type"] for event in detail["events"]}
    assert {
        "run.output.delta",
        "run.thinking.delta",
        "run.tool.started",
        "run.tool.finished",
        "run.usage.updated",
        "runtime.stderr",
    } <= event_types


@pytest.mark.anyio
async def test_codepuppy_runtime_uses_isolated_worktrees(codepuppy_hub: CodePuppyHub, tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    for arguments in (
        ("init", "--quiet"),
        ("config", "user.email", "agent-hub@example.com"),
        ("config", "user.name", "Agent Hub"),
    ):
        await anyio.run_process(["git", "-C", str(repository), *arguments])
    (repository / "tracked.txt").write_text("before\n", encoding="utf-8")
    await anyio.run_process(["git", "-C", str(repository), "add", "tracked.txt"])
    await anyio.run_process(["git", "-C", str(repository), "commit", "--quiet", "-m", "Initial commit"])

    spawned = await codepuppy_hub.rpc(
        "agent.spawn",
        {"profile": "task", "prompt": "tools", "cwd": str(repository), "isolated": True},
    )
    assert (await codepuppy_hub.wait(spawned["runId"]))["state"] == "succeeded"

    assert not (repository / "codepuppy.txt").exists()
    patch = await codepuppy_hub.rpc("agent.patch", {"agentId": spawned["agentId"]})
    assert "codepuppy.txt" in patch["patch"]
    assert await codepuppy_hub.rpc("agent.discard", {"agentId": spawned["agentId"]}) == {"discarded": True}


@pytest.mark.anyio
async def test_codepuppy_runtime_enforces_read_only_and_workspace_boundaries(
    codepuppy_hub: CodePuppyHub, tmp_path: Path
) -> None:
    prompts = {
        "permissions": "result:denied",
        "outside": "result:outside-blocked",
        "force-write": "result:write-blocked",
        "force-terminal": "result:terminal-blocked",
    }
    for prompt, expected in prompts.items():
        spawned = await codepuppy_hub.rpc(
            "agent.spawn",
            {"profile": "read", "prompt": prompt, "cwd": str(tmp_path)},
        )
        assert (await codepuppy_hub.wait(spawned["runId"]))["result"] == expected
    no_options = await codepuppy_hub.rpc(
        "agent.spawn", {"profile": "task", "prompt": "no-options", "cwd": str(tmp_path)}
    )
    assert (await codepuppy_hub.wait(no_options["runId"]))["result"] == "result:denied"
    assert not (tmp_path / "codepuppy.txt").exists()


@pytest.mark.anyio
async def test_codepuppy_runtime_aborts_and_restores_sessions(codepuppy_hub: CodePuppyHub, tmp_path: Path) -> None:
    blocked = await codepuppy_hub.rpc(
        "agent.spawn",
        {"profile": "sticky", "prompt": "block", "cwd": str(tmp_path)},
    )
    for _ in range(100):
        detail = await codepuppy_hub.rpc("agent.get", {"agentId": blocked["agentId"]})
        if detail["agent"]["state"] == "running":
            break
        await anyio.sleep(0.01)
    assert (await codepuppy_hub.rpc("agent.steer", {"agentId": blocked["agentId"], "message": "now"}))["error"][
        "code"
    ] == -32011
    assert (await codepuppy_hub.rpc("agent.follow_up", {"agentId": blocked["agentId"], "message": "later"}))["error"][
        "code"
    ] == -32011
    assert await codepuppy_hub.rpc("agent.abort", {"agentId": blocked["agentId"]}) == {
        "aborted": True,
        "runId": blocked["runId"],
    }
    assert (await codepuppy_hub.wait(blocked["runId"]))["state"] == "aborted"

    completed = await codepuppy_hub.rpc(
        "agent.spawn",
        {"profile": "sticky", "prompt": "persist", "cwd": str(tmp_path)},
    )
    assert (await codepuppy_hub.wait(completed["runId"]))["state"] == "succeeded"
    assert await codepuppy_hub.rpc("agent.park", {"agentId": completed["agentId"]}) == {"parked": True}
    assert await codepuppy_hub.rpc("agent.revive", {"agentId": completed["agentId"]}) == {"revived": True}
    continued = await codepuppy_hub.rpc("agent.prompt", {"agentId": completed["agentId"], "prompt": "continued"})
    assert (await codepuppy_hub.wait(continued["runId"]))["result"] == "result:continued"
    assert await codepuppy_hub.rpc("agent.stop", {"agentId": completed["agentId"]}) == {"stopped": True}


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("prompt", "expected"),
    [
        ("stderr-overflow", "stderr-overflow"),
        ("oversized-tool", "oversized-tool"),
        ("terminal-invalid-cwd", "cwd-blocked"),
        ("terminal-outside", "terminal-outside-blocked"),
        ("terminal-kill", "5678"),
        ("terminal-leak", "terminal-created"),
        ("unknown-terminal", "terminal-missing"),
    ],
)
async def test_codepuppy_runtime_handles_client_protocol_boundaries(
    codepuppy_hub: CodePuppyHub, tmp_path: Path, prompt: str, expected: str
) -> None:
    spawned = await codepuppy_hub.rpc("agent.spawn", {"prompt": prompt, "cwd": str(tmp_path)})

    run = await codepuppy_hub.wait(spawned["runId"])

    assert run["state"] == "succeeded"
    assert expected in run["result"]


@pytest.mark.anyio
@pytest.mark.parametrize("prompt", ["crash", "malformed"])
async def test_codepuppy_runtime_reports_process_and_protocol_failures(
    codepuppy_hub: CodePuppyHub, tmp_path: Path, prompt: str
) -> None:
    spawned = await codepuppy_hub.rpc("agent.spawn", {"prompt": prompt, "cwd": str(tmp_path)})

    run = await codepuppy_hub.wait(spawned["runId"])

    assert run["state"] == "failed"
    assert run["error"]


@pytest.mark.anyio
async def test_codepuppy_runtime_reports_startup_and_restoration_capability_failures(
    codepuppy_hub: CodePuppyHub, tmp_path: Path
) -> None:
    invalid = await codepuppy_hub.rpc(
        "agent.spawn",
        {"profile": "task", "prompt": "unused", "cwd": str(tmp_path), "model": "invalid-model"},
    )
    assert (await codepuppy_hub.wait(invalid["runId"]))["state"] == "failed"

    completed = await codepuppy_hub.rpc("agent.spawn", {"profile": "sticky", "prompt": "persist", "cwd": str(tmp_path)})
    assert (await codepuppy_hub.wait(completed["runId"]))["state"] == "succeeded"
    assert await codepuppy_hub.rpc("agent.park", {"agentId": completed["agentId"]}) == {"parked": True}
    (tmp_path / ".fixture-no-restoration").touch()
    revived = await codepuppy_hub.rpc("agent.revive", {"agentId": completed["agentId"]})
    assert revived["error"]["code"] == -32006
    assert "does not support ACP session restoration" in revived["error"]["message"]


@pytest.mark.anyio
async def test_codepuppy_runtime_validates_lifecycle_boundaries(tmp_path: Path) -> None:
    executable = Path(__file__).parent / "fixtures" / "fake_codepuppy.py"
    runtime = CodePuppyRuntime(str(executable), process_shutdown_seconds=0.05)
    profile = AgentProfile(name="puppy", runtime="codepuppy")
    request = StartAgentRequest("agent", profile, tmp_path, tmp_path / "session")

    with pytest.raises(RuntimeError, match="not open"):
        await runtime.start(request)
    await runtime.open()
    with pytest.raises(RuntimeError, match="already open"):
        await runtime.open()
    handle = await runtime.start(request)
    await runtime.stop(handle)
    await runtime.stop(handle)
    with pytest.raises(TypeError, match="CodePuppy"):
        await runtime.stop(object())
    agent = AgentRecord("agent", "codepuppy", "puppy", str(tmp_path), "shared-write", "parked", True)
    assert runtime.is_resumable(agent) is False
    with pytest.raises(RuntimeFailure, match="no ACP session"):
        await runtime.restore(agent, request)
    await runtime.close()

    missing = CodePuppyRuntime("missing-codepuppy-executable")
    await missing.open()
    with pytest.raises(RuntimeFailure, match="executable not found"):
        await missing.start(request)
    await missing.close()

    invalid = tmp_path / "invalid-codepuppy"
    invalid.write_text("not an executable format", encoding="utf-8")
    invalid.chmod(0o700)
    broken = CodePuppyRuntime(str(invalid))
    await broken.open()
    with pytest.raises(RuntimeFailure, match="Could not start CodePuppy"):
        await broken.start(request)
    await broken.close()
