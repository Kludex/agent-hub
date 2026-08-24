from __future__ import annotations

import json
from typing import Any

import pytest

from tests.conftest import RunningHub


async def wait_for_run(hub: RunningHub, run_id: str) -> dict[str, Any]:
    result = await hub.rpc("run.wait", {"runId": run_id, "timeoutSeconds": 3})
    assert "run" in result
    return dict(result["run"])


@pytest.mark.anyio
async def test_health_snapshot_and_unknown_method(hub: RunningHub) -> None:
    assert (await hub.client.get("/health")).json() == {"status": "ok"}
    assert await hub.rpc("hub.snapshot") == {"agents": [], "activeRuns": [], "latestSequence": 0}

    response = await hub.rpc("missing.method")

    assert response["error"]["code"] == -32601


@pytest.mark.anyio
async def test_spawn_wait_and_inspect_ephemeral_agent(hub: RunningHub, tmp_path: Any) -> None:
    spawned = await hub.rpc("agent.spawn", {"profile": "task", "prompt": "inspect", "cwd": str(tmp_path)})

    run = await wait_for_run(hub, spawned["runId"])
    detail = await hub.rpc("agent.get", {"agentId": spawned["agentId"]})

    assert run["state"] == "succeeded"
    assert run["result"] == "result:inspect"
    assert run["usage"]["totalTokens"] == 5
    assert detail["agent"]["state"] == "stopped"
    assert detail["runs"] == [run]
    assert hub.runtime.handles[0].stopped is True


@pytest.mark.anyio
async def test_completed_streaming_events_follow_the_retention_limit(hub: RunningHub, tmp_path: Any) -> None:
    hub.config.completed_event_retention = 0
    spawned = await hub.rpc("agent.spawn", {"prompt": "retained result", "cwd": str(tmp_path)})

    run = await wait_for_run(hub, spawned["runId"])
    detail = await hub.rpc("agent.get", {"agentId": spawned["agentId"]})

    assert run["result"] == "result:retained result"
    assert not any(event["type"] == "run.output.delta" for event in detail["events"])
    assert any(event["type"] == "run.state.changed" for event in detail["events"])


@pytest.mark.anyio
async def test_idempotent_spawn_returns_the_original_ids(hub: RunningHub, tmp_path: Any) -> None:
    params = {
        "profile": "task",
        "prompt": "once",
        "cwd": str(tmp_path),
        "idempotencyKey": "spawn-once",
    }

    first = await hub.rpc("agent.spawn", params)
    second = await hub.rpc("agent.spawn", params)
    agents = await hub.rpc("agent.list")

    assert second == first
    assert len(agents["agents"]) == 1
    await wait_for_run(hub, first["runId"])


@pytest.mark.anyio
async def test_idempotency_key_cannot_be_reused_for_another_method(hub: RunningHub, tmp_path: Any) -> None:
    spawned = await hub.rpc(
        "agent.spawn",
        {"profile": "task", "prompt": "once", "cwd": str(tmp_path), "idempotencyKey": "shared"},
    )
    await wait_for_run(hub, spawned["runId"])

    response = await hub.rpc("agent.stop", {"agentId": spawned["agentId"], "idempotencyKey": "shared"})

    assert response["error"]["code"] == -32602


@pytest.mark.anyio
async def test_rpc_rejects_invalid_records(hub: RunningHub) -> None:
    empty = await hub.client.post("/v1/rpc", content=b"")
    malformed = await hub.client.post("/v1/rpc", content=b"{nope}\n")
    invalid = await hub.client.post("/v1/rpc", content=b"[]\n")

    assert empty.json()["error"]["code"] == -32600
    assert malformed.json()["error"]["code"] == -32700
    assert invalid.json()["error"]["code"] == -32600


@pytest.mark.anyio
async def test_rpc_accepts_multiple_unicode_records(hub: RunningHub) -> None:
    body = b"\n".join(
        [
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "hub.snapshot"}).encode(),
            json.dumps(
                {"jsonrpc": "2.0", "id": "2\u2028", "method": "agent.list", "params": {"state": "stopped"}}
            ).encode(),
        ]
    )

    response = await hub.client.post("/v1/rpc", content=body + b"\n")
    records = [json.loads(line) for line in response.content.split(b"\n") if line]

    assert [record["id"] for record in records] == [1, "2\u2028"]
    assert records[1]["result"] == {"agents": []}


@pytest.mark.anyio
async def test_event_stream_rejects_invalid_cursors(hub: RunningHub) -> None:
    text = await hub.client.get("/v1/events?after=latest")
    negative = await hub.client.get("/v1/events?after=-1")

    assert text.status_code == 400
    assert negative.status_code == 400


@pytest.mark.anyio
async def test_rpc_enforces_the_configured_record_limit(hub: RunningHub) -> None:
    hub.config.max_record_bytes = 4

    response = await hub.client.post("/v1/rpc", content=b'{"jsonrpc":"2.0"}\n')

    assert response.json()["error"]["code"] == -32600


@pytest.mark.anyio
async def test_event_stream_replays_from_sequence(hub: RunningHub, tmp_path: Any) -> None:
    spawned = await hub.rpc("agent.spawn", {"prompt": "stream", "cwd": str(tmp_path)})
    await wait_for_run(hub, spawned["runId"])
    events: list[dict[str, Any]] = []

    async with hub.client.stream("GET", "/v1/events?after=0") as response:
        async for line in response.aiter_lines():
            event = json.loads(line)["params"]
            events.append(event)
            if event["type"] == "agent.state.changed" and event["data"]["state"] == "stopped":
                break

    sequences = [event["sequence"] for event in events]
    assert sequences == list(range(1, len(events) + 1))
    assert any(event["type"] == "run.output.delta" for event in events)
    assert any(event["type"] == "run.tool.started" for event in events)
