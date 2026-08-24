from __future__ import annotations

from pathlib import Path
from typing import Any

import anyio
import pytest

from tests.conftest import RunningHub
from tests.test_api import wait_for_run


async def wait_for_agent_state(hub: RunningHub, agent_id: str, state: str) -> dict[str, Any]:
    for _ in range(100):
        result = await hub.rpc("agent.get", {"agentId": agent_id})
        if result["agent"]["state"] == state:
            return dict(result["agent"])
        await anyio.sleep(0.01)
    raise AssertionError(f"Agent {agent_id} did not reach {state}")  # pragma: no cover - test timeout guard


@pytest.mark.anyio
async def test_keep_alive_agent_accepts_multiple_runs_and_can_park(hub: RunningHub, tmp_path: Path) -> None:
    spawned = await hub.rpc("agent.spawn", {"profile": "sticky", "prompt": "first", "cwd": str(tmp_path)})
    await wait_for_run(hub, spawned["runId"])
    await wait_for_agent_state(hub, spawned["agentId"], "idle")

    second = await hub.rpc("agent.prompt", {"agentId": spawned["agentId"], "prompt": "second"})
    second_run = await wait_for_run(hub, second["runId"])
    parked = await hub.rpc("agent.park", {"agentId": spawned["agentId"]})
    revived = await hub.rpc("agent.revive", {"agentId": spawned["agentId"]})

    assert second_run["result"] == "result:second"
    assert parked == {"parked": True}
    assert revived == {"revived": True}
    await wait_for_agent_state(hub, spawned["agentId"], "idle")
    assert (await hub.rpc("agent.stop", {"agentId": spawned["agentId"]}))["stopped"] is True


@pytest.mark.anyio
async def test_revival_failure_marks_the_agent_failed(hub: RunningHub, tmp_path: Path) -> None:
    spawned = await hub.rpc("agent.spawn", {"profile": "sticky", "prompt": "park", "cwd": str(tmp_path)})
    await wait_for_run(hub, spawned["runId"])
    await hub.rpc("agent.park", {"agentId": spawned["agentId"]})
    hub.runtime.fail_restore = True

    response = await hub.rpc("agent.revive", {"agentId": spawned["agentId"]})

    assert response["error"]["code"] == -32006
    agent = await hub.rpc("agent.get", {"agentId": spawned["agentId"]})
    assert agent["agent"]["state"] == "failed"


@pytest.mark.anyio
async def test_keep_alive_agent_parks_after_its_idle_timeout(hub: RunningHub, tmp_path: Path) -> None:
    spawned = await hub.rpc("agent.spawn", {"profile": "brief", "prompt": "short", "cwd": str(tmp_path)})
    await wait_for_run(hub, spawned["runId"])

    agent = await wait_for_agent_state(hub, spawned["agentId"], "parked")

    assert agent["state"] == "parked"


@pytest.mark.anyio
async def test_steer_follow_up_and_abort_running_agent(hub: RunningHub, tmp_path: Path) -> None:
    spawned = await hub.rpc("agent.spawn", {"profile": "sticky", "prompt": "block-main", "cwd": str(tmp_path)})
    await wait_for_agent_state(hub, spawned["agentId"], "running")

    assert await hub.rpc("agent.steer", {"agentId": spawned["agentId"], "message": "focus"}) == {"accepted": True}
    assert await hub.rpc("agent.follow_up", {"agentId": spawned["agentId"], "message": "summarize"}) == {
        "accepted": True
    }
    rejected = await hub.rpc("agent.follow_up", {"agentId": spawned["agentId"], "message": "reject"})
    assert rejected["error"]["code"] == -32011
    aborted = await hub.rpc("agent.abort", {"agentId": spawned["agentId"]})
    run = await wait_for_run(hub, spawned["runId"])

    assert aborted["aborted"] is True
    assert run["state"] == "aborted"
    await wait_for_agent_state(hub, spawned["agentId"], "idle")
    resumed = await hub.rpc("agent.prompt", {"agentId": spawned["agentId"], "prompt": "resumed"})
    assert (await wait_for_run(hub, resumed["runId"]))["state"] == "succeeded"
    await hub.rpc("agent.stop", {"agentId": spawned["agentId"]})


@pytest.mark.anyio
async def test_runtime_programming_errors_return_an_internal_rpc_error(hub: RunningHub, tmp_path: Path) -> None:
    spawned = await hub.rpc("agent.spawn", {"profile": "sticky", "prompt": "block-bug", "cwd": str(tmp_path)})
    await wait_for_agent_state(hub, spawned["agentId"], "running")

    response = await hub.rpc("agent.steer", {"agentId": spawned["agentId"], "message": "bug"})

    assert response["error"]["code"] == -32603
    await hub.rpc("agent.abort", {"agentId": spawned["agentId"]})
    await wait_for_run(hub, spawned["runId"])
    await hub.rpc("agent.stop", {"agentId": spawned["agentId"]})


@pytest.mark.anyio
async def test_abort_queued_run_releases_it_without_starting(hub: RunningHub, tmp_path: Path) -> None:
    first = await hub.rpc("agent.spawn", {"prompt": "block-one", "cwd": str(tmp_path)})
    second = await hub.rpc("agent.spawn", {"prompt": "block-two", "cwd": str(tmp_path), "access": "read-only"})
    third = await hub.rpc("agent.spawn", {"prompt": "queued", "cwd": str(tmp_path), "access": "read-only"})
    await wait_for_agent_state(hub, first["agentId"], "running")
    await wait_for_agent_state(hub, second["agentId"], "running")

    aborted = await hub.rpc("agent.abort", {"agentId": third["agentId"]})
    third_run = await wait_for_run(hub, third["runId"])
    hub.runtime.release(0)
    hub.runtime.release(1)

    assert aborted["aborted"] is True
    assert third_run["state"] == "aborted"
    await wait_for_run(hub, first["runId"])
    await wait_for_run(hub, second["runId"])


@pytest.mark.anyio
async def test_abort_runtime_errors_are_reported(hub: RunningHub, tmp_path: Path) -> None:
    spawned = await hub.rpc(
        "agent.spawn",
        {"profile": "sticky", "prompt": "block-abort-error", "cwd": str(tmp_path)},
    )
    await wait_for_agent_state(hub, spawned["agentId"], "running")

    response = await hub.rpc("agent.abort", {"agentId": spawned["agentId"]})

    assert response["error"]["code"] == -32011
    hub.runtime.handles[-1].abort_error = False
    await hub.rpc("agent.abort", {"agentId": spawned["agentId"]})
    await wait_for_run(hub, spawned["runId"])
    await hub.rpc("agent.stop", {"agentId": spawned["agentId"]})


@pytest.mark.anyio
async def test_run_wait_without_timeout_settles(hub: RunningHub, tmp_path: Path) -> None:
    spawned = await hub.rpc("agent.spawn", {"prompt": "block-wait", "cwd": str(tmp_path)})
    await wait_for_agent_state(hub, spawned["agentId"], "running")
    send, receive = anyio.create_memory_object_stream[dict[str, Any]](1)

    async def wait() -> None:
        await send.send(await hub.rpc("run.wait", {"runId": spawned["runId"]}))

    async with anyio.create_task_group() as task_group:
        task_group.start_soon(wait)
        await anyio.sleep(0.01)
        hub.runtime.release()
        result = await receive.receive()

    assert result["run"]["state"] == "succeeded"
    await send.aclose()
    await receive.aclose()


@pytest.mark.anyio
async def test_run_timeout_stops_the_runtime(hub: RunningHub, tmp_path: Path) -> None:
    spawned = await hub.rpc(
        "agent.spawn",
        {"prompt": "block-timeout", "cwd": str(tmp_path), "maxRuntimeSeconds": 0.01},
    )

    run = await wait_for_run(hub, spawned["runId"])

    assert run["state"] == "failed"
    assert run["error"] == "Run exceeded its maximum runtime"
    assert hub.runtime.handles[0].stopped is True


@pytest.mark.anyio
@pytest.mark.parametrize("prompt", ["event-failure", "event-bug"])
async def test_runtime_event_failures_are_normalized(hub: RunningHub, tmp_path: Path, prompt: str) -> None:
    spawned = await hub.rpc("agent.spawn", {"prompt": prompt, "cwd": str(tmp_path)})

    await wait_for_run(hub, spawned["runId"])
    detail = await hub.rpc("agent.get", {"agentId": spawned["agentId"]})

    assert any(event["type"] == "run.error" for event in detail["events"])


@pytest.mark.anyio
async def test_unexpected_runtime_failures_are_normalized(hub: RunningHub, tmp_path: Path) -> None:
    spawned = await hub.rpc("agent.spawn", {"prompt": "bug-run", "cwd": str(tmp_path)})

    run = await wait_for_run(hub, spawned["runId"])

    assert run["state"] == "failed"
    assert "Unexpected runtime failure" in run["error"]


@pytest.mark.anyio
async def test_runtime_failure_marks_run_and_agent_failed(hub: RunningHub, tmp_path: Path) -> None:
    spawned = await hub.rpc("agent.spawn", {"prompt": "fail", "cwd": str(tmp_path)})

    run = await wait_for_run(hub, spawned["runId"])
    agent = await wait_for_agent_state(hub, spawned["agentId"], "failed")

    assert run["state"] == "failed"
    assert run["error"] == "requested failure"
    assert agent["state"] == "failed"


@pytest.mark.anyio
async def test_provider_credentials_are_redacted_from_persistence(
    hub: RunningHub,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AGENT_HUB_TEST_TOKEN", "supersecret")
    spawned = await hub.rpc("agent.spawn", {"prompt": "supersecret", "cwd": str(tmp_path)})

    run = await wait_for_run(hub, spawned["runId"])
    detail = await hub.rpc("agent.get", {"agentId": spawned["agentId"]})

    assert run["prompt"] == "[REDACTED]"
    assert run["result"] == "result:[REDACTED]"
    assert "supersecret" not in repr(detail)


@pytest.mark.anyio
async def test_output_is_bounded_centrally(hub: RunningHub, tmp_path: Path) -> None:
    hub.config.max_output_bytes = 5
    spawned = await hub.rpc("agent.spawn", {"prompt": "block-output", "cwd": str(tmp_path), "access": "read-only"})
    await wait_for_agent_state(hub, spawned["agentId"], "running")
    for _ in range(100):
        snapshot = await hub.rpc("hub.snapshot")
        agent = next(item for item in snapshot["agents"] if item["id"] == spawned["agentId"])
        if agent.get("currentTool") == "read":
            break
        await anyio.sleep(0.01)  # pragma: no cover - event timing depends on the host
    else:  # pragma: no cover - reports test infrastructure failure
        raise AssertionError("Snapshot did not expose the current tool")
    await hub.rpc("agent.follow_up", {"agentId": spawned["agentId"], "message": "finish-tool"})
    for _ in range(100):
        snapshot = await hub.rpc("hub.snapshot")
        agent = next(item for item in snapshot["agents"] if item["id"] == spawned["agentId"])
        if "currentTool" not in agent:
            break
        await anyio.sleep(0.01)  # pragma: no cover - event timing depends on the host
    await hub.rpc("agent.steer", {"agentId": spawned["agentId"], "message": "more"})
    hub.runtime.release()

    run = await wait_for_run(hub, spawned["runId"])
    detail = await hub.rpc("agent.get", {"agentId": spawned["agentId"]})
    output = [event for event in detail["events"] if event["type"] == "run.output.delta"]

    assert run["result"] == "resul"
    assert sum(len(event["data"]["text"].encode()) for event in output) <= 5
    assert output[0]["data"]["truncated"] is True


@pytest.mark.anyio
async def test_non_text_output_payload_is_preserved(hub: RunningHub, tmp_path: Path) -> None:
    spawned = await hub.rpc("agent.spawn", {"prompt": "nontext-output", "cwd": str(tmp_path)})
    await wait_for_run(hub, spawned["runId"])
    detail = await hub.rpc("agent.get", {"agentId": spawned["agentId"]})

    assert any(event["data"].get("text") == 1 for event in detail["events"])


@pytest.mark.anyio
async def test_validation_errors_are_structured(hub: RunningHub, tmp_path: Path) -> None:
    missing = await hub.rpc("agent.spawn", {"profile": "nope", "prompt": "x", "cwd": str(tmp_path)})
    access = await hub.rpc("agent.spawn", {"prompt": "x", "cwd": str(tmp_path), "access": "root"})
    cwd = await hub.rpc("agent.spawn", {"prompt": "x", "cwd": str(tmp_path / "missing")})
    absent = await hub.rpc("agent.get", {"agentId": "agt_missing"})
    timeout = await hub.rpc(
        "agent.spawn",
        {"prompt": "x", "cwd": str(tmp_path), "maxRuntimeSeconds": 0},
    )

    assert missing["error"]["code"] == -32001
    assert access["error"]["code"] == -32602
    assert cwd["error"]["code"] == -32602
    assert absent["error"]["code"] == -32008
    assert timeout["error"]["code"] == -32602


@pytest.mark.anyio
async def test_daemon_shutdown_closes_idle_runtimes(hub: RunningHub, tmp_path: Path) -> None:
    spawned = await hub.rpc("agent.spawn", {"profile": "sticky", "prompt": "idle-shutdown", "cwd": str(tmp_path)})

    await wait_for_run(hub, spawned["runId"])
    await wait_for_agent_state(hub, spawned["agentId"], "idle")


@pytest.mark.anyio
async def test_daemon_shutdown_cancels_active_runs(hub: RunningHub, tmp_path: Path) -> None:
    spawned = await hub.rpc(
        "agent.spawn",
        {"profile": "sticky", "prompt": "block-shutdown", "cwd": str(tmp_path)},
    )

    await wait_for_agent_state(hub, spawned["agentId"], "running")


@pytest.mark.anyio
async def test_command_parameters_are_validated(hub: RunningHub, tmp_path: Path) -> None:
    file_path = tmp_path / "file.txt"
    file_path.write_text("x", encoding="utf-8")
    invalid_calls: list[tuple[str, dict[str, Any]]] = [
        ("agent.spawn", {"prompt": "x", "cwd": str(tmp_path), "runtime": []}),
        ("agent.spawn", {"prompt": "x", "cwd": str(tmp_path), "model": []}),
        ("agent.spawn", {"profile": "sticky", "prompt": "x", "cwd": str(tmp_path), "model": "override"}),
        ("agent.spawn", {"prompt": "x", "cwd": str(tmp_path), "runtime": "pydantic-ai"}),
        ("agent.spawn", {"prompt": "x", "cwd": str(tmp_path), "isolated": "yes"}),
        ("agent.spawn", {"profile": "scout", "prompt": "x", "cwd": str(tmp_path), "access": "shared-write"}),
        ("agent.spawn", {"prompt": "x", "cwd": str(tmp_path), "detached": "yes"}),
        ("agent.spawn", {"prompt": "", "cwd": str(tmp_path)}),
        ("agent.spawn", {"prompt": "x", "cwd": str(file_path)}),
        ("agent.spawn", {"prompt": "x", "cwd": str(tmp_path), "parentAgentId": 1}),
        ("agent.spawn", {"prompt": "x", "cwd": str(tmp_path), "parentAgentId": "missing"}),
        ("agent.list", {"state": "gone"}),
        ("agent.list", {"parentAgentId": []}),
        ("agent.get", {}),
        ("run.get", {}),
        ("run.get", {"runId": "missing"}),
    ]

    for method, params in invalid_calls:
        response = await hub.rpc(method, params)
        assert "error" in response

    completed = await hub.rpc("agent.spawn", {"prompt": "done", "cwd": str(tmp_path)})
    await wait_for_run(hub, completed["runId"])
    assert "error" in await hub.rpc("agent.prompt", {"agentId": completed["agentId"], "prompt": "again"})
    assert "error" in await hub.rpc("agent.park", {"agentId": completed["agentId"]})
    assert "error" in await hub.rpc("agent.revive", {"agentId": completed["agentId"]})
    assert "error" in await hub.rpc("agent.patch", {"agentId": completed["agentId"]})
    assert "error" in await hub.rpc("agent.steer", {"agentId": completed["agentId"], "message": "x"})
    assert (await hub.rpc("agent.abort", {"agentId": completed["agentId"]}))["aborted"] is False

    active = await hub.rpc("agent.spawn", {"profile": "sticky", "prompt": "block-validation", "cwd": str(tmp_path)})
    await wait_for_agent_state(hub, active["agentId"], "running")
    assert "error" in await hub.rpc("agent.steer", {"agentId": active["agentId"], "message": ""})
    assert "error" in await hub.rpc("run.wait", {"runId": active["runId"], "timeoutSeconds": 0})
    timed_out = await hub.rpc("run.wait", {"runId": active["runId"], "timeoutSeconds": 0.01})
    assert timed_out["error"]["code"] == -32007
    await hub.rpc("agent.abort", {"agentId": active["agentId"]})
    await wait_for_run(hub, active["runId"])
    assert "error" in await hub.rpc("agent.prompt", {"agentId": active["agentId"], "prompt": ""})
    assert "error" in await hub.rpc(
        "agent.prompt",
        {"agentId": active["agentId"], "prompt": "x", "isolated": "yes"},
    )
    assert "error" in await hub.rpc(
        "agent.prompt",
        {"agentId": active["agentId"], "prompt": "x", "isolated": True},
    )
    await hub.rpc("agent.stop", {"agentId": active["agentId"]})


@pytest.mark.anyio
async def test_agent_reports_when_its_project_profile_was_removed(hub: RunningHub, tmp_path: Path) -> None:
    profile_directory = tmp_path / ".agent-hub" / "agents"
    profile_directory.mkdir(parents=True)
    profile = profile_directory / "temporary.toml"
    profile.write_text(
        'name = "temporary"\nkeep_alive = true\nidle_timeout_seconds = 60\n',
        encoding="utf-8",
    )
    hub.config.allow_project_profiles = True
    spawned = await hub.rpc("agent.spawn", {"profile": "temporary", "prompt": "run", "cwd": str(tmp_path)})
    await wait_for_run(hub, spawned["runId"])
    profile.unlink()

    response = await hub.rpc("agent.prompt", {"agentId": spawned["agentId"], "prompt": "again"})

    assert response["error"]["code"] == -32001
    await hub.rpc("agent.stop", {"agentId": spawned["agentId"]})


@pytest.mark.anyio
async def test_stopping_parent_cancels_only_attached_children(hub: RunningHub, tmp_path: Path) -> None:
    parent = await hub.rpc("agent.spawn", {"prompt": "parent", "cwd": str(tmp_path)})
    await wait_for_run(hub, parent["runId"])
    attached = await hub.rpc(
        "agent.spawn",
        {"prompt": "block-attached", "cwd": str(tmp_path), "parentAgentId": parent["agentId"]},
    )
    detached = await hub.rpc(
        "agent.spawn",
        {
            "prompt": "block-detached",
            "cwd": str(tmp_path),
            "parentAgentId": parent["agentId"],
            "detached": True,
            "access": "read-only",
        },
    )
    await wait_for_agent_state(hub, attached["agentId"], "running")
    await wait_for_agent_state(hub, detached["agentId"], "running")

    await hub.rpc("agent.stop", {"agentId": parent["agentId"]})

    assert (await wait_for_run(hub, attached["runId"]))["state"] == "aborted"
    assert (await hub.rpc("run.get", {"runId": detached["runId"]}))["run"]["state"] == "running"
    await hub.rpc("agent.abort", {"agentId": detached["agentId"]})
    await wait_for_run(hub, detached["runId"])


@pytest.mark.anyio
async def test_nested_delegation_requires_profile_opt_in(hub: RunningHub, tmp_path: Path) -> None:
    parent = await hub.rpc("agent.spawn", {"profile": "sticky", "prompt": "parent", "cwd": str(tmp_path)})
    await wait_for_run(hub, parent["runId"])

    child = await hub.rpc(
        "agent.spawn",
        {"prompt": "child", "cwd": str(tmp_path), "parentAgentId": parent["agentId"]},
    )

    assert child["error"]["code"] == -32012
    await hub.rpc("agent.stop", {"agentId": parent["agentId"]})


@pytest.mark.anyio
async def test_delegation_depth_is_enforced(hub: RunningHub, tmp_path: Path) -> None:
    parent: str | None = None
    for depth in range(4):
        params: dict[str, Any] = {"prompt": f"depth-{depth}", "cwd": str(tmp_path)}
        if parent is not None:
            params["parentAgentId"] = parent
        spawned = await hub.rpc("agent.spawn", params)
        assert "agentId" in spawned
        parent = spawned["agentId"]
        await wait_for_run(hub, spawned["runId"])

    rejected = await hub.rpc("agent.spawn", {"prompt": "too-deep", "cwd": str(tmp_path), "parentAgentId": parent})

    assert rejected["error"]["code"] == -32003
