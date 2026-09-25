from __future__ import annotations

import json
import os
import sys
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import anyio
import httpx2
import pytest
import uvicorn
from starlette.types import Receive, Scope, Send

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


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("limit", "record_bytes", "ending", "succeeds"),
    [
        (None, 1_071_439, b"\n", True),
        (None, 2 * 1024 * 1024, b"\n", True),
        (None, 2 * 1024 * 1024 + 1, b"\n", False),
        (None, 2 * 1024 * 1024 + 1, b"", False),
        (3 * 1024 * 1024, 2 * 1024 * 1024 + 1, b"\n", True),
        (1024 * 1024, 1024 * 1024, b"\n", True),
        (1024 * 1024, 1024 * 1024 + 1, b"\n", False),
    ],
)
async def test_pi_aggregate_transport_limit_is_independent_of_api_and_output_limits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    limit: int | None,
    record_bytes: int,
    ending: bytes,
    succeeds: bool,
) -> None:
    config = HubConfig(data_dir=tmp_path, shutdown_grace_seconds=0.01, process_shutdown_seconds=0.05)
    assert config.pi_max_record_bytes == 2 * 1024 * 1024
    if limit is not None:
        config.pi_max_record_bytes = limit
    text = "€\u2028\u2029" * 6000
    messages: list[dict[str, Any]] = [
        {"role": "toolResult", "toolName": "read", "content": [{"type": "text", "text": ""}]} for _ in range(87)
    ]
    messages.extend({"role": "assistant", "content": []} for _ in range(13))
    messages.extend(
        [
            {"role": "user", "content": "synthetic aggregate"},
            {"role": "assistant", "content": [{"type": "text", "text": text}]},
        ]
    )
    aggregate = {"type": "agent_end", "messages": messages, "willRetry": False}
    padding = record_bytes - len(json.dumps(aggregate, ensure_ascii=False).encode())
    size, remainder = divmod(padding, 87)
    for index, message in enumerate(messages[:87]):
        message["content"][0]["text"] = "x" * (size + (index < remainder))
    raw = json.dumps(aggregate, ensure_ascii=False).encode()
    assert len(raw) == record_bytes
    delta = {"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "delta": text}}
    payload = json.dumps(delta, ensure_ascii=False).encode() + b"\n" + raw + ending
    if ending:
        payload += b'{"type":"agent_settled"}\n'
    (tmp_path / "events.jsonl").write_bytes(payload)
    (tmp_path / "result.txt").write_text(text, encoding="utf-8")
    executable = tmp_path / "pi"
    executable.write_text(
        f"#!{sys.executable}\n"
        "import json, sys\n"
        "from pathlib import Path\n"
        "for raw in sys.stdin.buffer:\n"
        "    command = json.loads(raw)\n"
        "    data = {'text': Path('result.txt').read_text()} if command['type'] == 'get_last_assistant_text' else {}\n"
        "    print(json.dumps({'id': command['id'], 'type': 'response', 'success': True, 'data': data}), flush=True)\n"
        "    if command['type'] == 'prompt':\n"
        "        sys.stdout.buffer.write(Path('events.jsonl').read_bytes())\n"
        "        sys.stdout.buffer.flush()\n"
        "    elif command['type'] == 'abort':\n"
        '        print(\'{"type":"agent_settled"}\', flush=True)\n',
        encoding="utf-8",
    )
    executable.chmod(0o700)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    app = create_app(config)
    async with app.router.lifespan_context(app) as state:

        async def app_with_state(scope: Scope, receive: Receive, send: Send) -> None:
            await app({**scope, "state": state}, receive, send)

        async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app_with_state), base_url="http://agent-hub"
        ) as client:
            hub = PiHub(client)
            spawned = await hub.rpc("agent.spawn", {"prompt": "aggregate", "cwd": str(tmp_path)})
            run = await hub.wait(spawned["runId"])
            if succeeds:
                assert run["state"] == "succeeded"
                assert config.max_output_bytes == 50 * 1024
                assert run["result"] == text.encode()[: 50 * 1024].decode(errors="ignore")
                detail = await hub.rpc("agent.get", {"agentId": spawned["agentId"]})
                deltas = [event["data"] for event in detail["events"] if event["type"] == "run.output.delta"]
                assert sum(len(delta["text"].encode()) for delta in deltas) <= 50 * 1024
                assert deltas[0]["truncated"] is True
            else:
                assert run["state"] == "failed"
                assert run["error"] == "Pi JSONL record exceeds the configured limit"
            assert config.max_record_bytes == 1024 * 1024
            rejected = await client.post("/v1/rpc", content=b" " * (1024 * 1024 + 1) + b"\n")
            assert rejected.json()["error"]["message"] == "JSONL record exceeds the configured limit"
