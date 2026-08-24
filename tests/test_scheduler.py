from __future__ import annotations

from pathlib import Path

import anyio
import pytest

from agent_hub.scheduler import Scheduler, ScheduleRequest
from tests.conftest import RunningHub
from tests.test_api import wait_for_run
from tests.test_lifecycle import wait_for_agent_state


@pytest.mark.anyio
async def test_releasing_an_inactive_schedule_is_safe() -> None:
    scheduler = Scheduler(1)

    await scheduler.release(ScheduleRequest("missing", "/repo", "shared-write"))


@pytest.mark.anyio
async def test_shared_writers_are_serialized_per_workspace(hub: RunningHub, tmp_path: Path) -> None:
    first = await hub.rpc("agent.spawn", {"prompt": "block-first", "cwd": str(tmp_path)})
    second = await hub.rpc("agent.spawn", {"prompt": "block-second", "cwd": str(tmp_path)})
    await wait_for_agent_state(hub, first["agentId"], "running")
    await anyio.sleep(0.05)

    second_agent = await hub.rpc("agent.get", {"agentId": second["agentId"]})
    assert second_agent["runs"][0]["state"] == "queued"
    assert hub.runtime.maximum_running == 1

    hub.runtime.release(0)
    await wait_for_run(hub, first["runId"])
    await wait_for_agent_state(hub, second["agentId"], "running")
    hub.runtime.release(1)
    await wait_for_run(hub, second["runId"])


@pytest.mark.anyio
async def test_read_only_runs_use_global_concurrency(hub: RunningHub, tmp_path: Path) -> None:
    runs = [
        await hub.rpc(
            "agent.spawn",
            {"prompt": f"block-{index}", "cwd": str(tmp_path), "access": "read-only"},
        )
        for index in range(3)
    ]
    await wait_for_agent_state(hub, runs[0]["agentId"], "running")
    await wait_for_agent_state(hub, runs[1]["agentId"], "running")
    await anyio.sleep(0.05)

    third = await hub.rpc("run.get", {"runId": runs[2]["runId"]})
    assert third["run"]["state"] == "queued"
    assert hub.runtime.maximum_running == 2

    hub.runtime.release(0)
    await wait_for_run(hub, runs[0]["runId"])
    await wait_for_agent_state(hub, runs[2]["agentId"], "running")
    for handle in hub.runtime.handles:
        handle.blocked.set()
    await wait_for_run(hub, runs[1]["runId"])
    await wait_for_run(hub, runs[2]["runId"])


@pytest.mark.anyio
async def test_blocked_writer_does_not_block_another_workspace(hub: RunningHub, tmp_path: Path) -> None:
    other = tmp_path / "other"
    other.mkdir()
    first = await hub.rpc("agent.spawn", {"prompt": "block-first", "cwd": str(tmp_path)})
    second = await hub.rpc("agent.spawn", {"prompt": "block-second", "cwd": str(tmp_path)})
    third = await hub.rpc("agent.spawn", {"prompt": "block-third", "cwd": str(other)})
    await wait_for_agent_state(hub, first["agentId"], "running")
    await wait_for_agent_state(hub, third["agentId"], "running")

    assert (await hub.rpc("run.get", {"runId": second["runId"]}))["run"]["state"] == "queued"
    for handle in hub.runtime.handles:
        handle.blocked.set()
    await wait_for_run(hub, first["runId"])
    await wait_for_run(hub, third["runId"])
    await wait_for_agent_state(hub, second["agentId"], "running")
    hub.runtime.handles[-1].blocked.set()
    await wait_for_run(hub, second["runId"])
