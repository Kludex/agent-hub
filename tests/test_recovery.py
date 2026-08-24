from __future__ import annotations

import uuid
from pathlib import Path

import anyio
import httpx2
import pytest
import uvicorn

from agent_hub.app import create_app
from agent_hub.cli import bind_socket
from agent_hub.config import HubConfig
from agent_hub.models import AgentRecord, RunRecord
from agent_hub.persistence import Repository
from tests.conftest import FakeRuntime, rpc_request, serve_uvicorn


@pytest.mark.anyio
async def test_recovers_lost_runs_and_resumable_agents_after_restart(tmp_path: Path) -> None:
    socket_directory = Path("/tmp") / f"ah-recovery-{uuid.uuid4().hex}"
    config = HubConfig(data_dir=tmp_path, socket_path=socket_directory / "hub.sock")
    if config.database_path is None or config.socket_path is None:  # pragma: no cover - settings guarantee paths
        raise AssertionError("HubConfig did not configure persistence paths")
    repository = Repository(config.database_path)
    await repository.open()
    session_file = tmp_path / "session.jsonl"
    session_file.touch()
    resumable = AgentRecord(
        id="agt_resumable",
        runtime="pi",
        profile="task",
        cwd=str(tmp_path),
        access="shared-write",
        state="running",
        keep_alive=True,
        restoration={"sessionFile": str(session_file)},
    )
    lost = AgentRecord(
        id="agt_lost",
        runtime="pi",
        profile="task",
        cwd=str(tmp_path),
        access="shared-write",
        state="starting",
        keep_alive=False,
    )
    await repository.create_agent(resumable)
    await repository.create_agent(lost)
    await repository.create_run(RunRecord(id="run_lost", agent_id=resumable.id, state="running", prompt="work"))
    await repository.close()

    listener = bind_socket(config.socket_path)
    server = uvicorn.Server(
        uvicorn.Config(
            create_app(config, {"pi": FakeRuntime()}),
            http="zttp",
            log_config=None,
            access_log=False,
            lifespan="on",
        )
    )
    task_group = anyio.create_task_group()
    await task_group.__aenter__()
    task_group.start_soon(serve_uvicorn, server, listener)
    client = httpx2.AsyncClient(
        transport=httpx2.AsyncHTTPTransport(uds=str(config.socket_path)),
        base_url="http://agent-hub",
    )
    try:
        for _ in range(100):
            try:
                if (await client.get("/health")).status_code == 200:
                    break
            except httpx2.ConnectError:  # pragma: no cover - startup timing depends on the host
                await anyio.sleep(0.01)
        else:  # pragma: no cover - reports test infrastructure failure
            raise AssertionError("Recovered Hub did not start")

        snapshot = await rpc_request(client, "hub.snapshot")
        run = await rpc_request(client, "run.get", {"runId": "run_lost"})
    finally:
        await client.aclose()
        server.should_exit = True
        await task_group.__aexit__(None, None, None)
        listener.close()
        config.socket_path.unlink(missing_ok=True)
        socket_directory.rmdir()

    states = {agent["id"]: agent["state"] for agent in snapshot["agents"]}
    assert states == {"agt_resumable": "parked", "agt_lost": "failed"}
    assert snapshot["activeRuns"] == []
    assert run["run"]["state"] == "failed"
    assert run["run"]["error"] == "Agent Hub restarted"
