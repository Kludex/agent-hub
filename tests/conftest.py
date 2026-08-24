from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import anyio
import httpx2
import pytest
import uvicorn
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream
from pydantic import TypeAdapter

from agent_hub.app import create_app
from agent_hub.cli import bind_socket
from agent_hub.config import AgentProfile, HubConfig
from agent_hub.models import AgentRecord
from agent_hub.runtimes.base import RuntimeEvent, RuntimeFailure, RuntimeResult, StartAgentRequest, StartRunRequest

JSON_OBJECT_ADAPTER = TypeAdapter(dict[str, Any])


@dataclass
class FakeHandle:
    event_send: MemoryObjectSendStream[RuntimeEvent]
    event_receive: MemoryObjectReceiveStream[RuntimeEvent]
    blocked: anyio.Event = field(default_factory=anyio.Event)
    stopped: bool = False
    aborted: bool = False
    abort_error: bool = False
    event_error: str | None = None


class FakeRuntime:
    def __init__(self) -> None:
        self.handles: list[FakeHandle] = []
        self.start_requests: list[StartAgentRequest] = []
        self.running = 0
        self.maximum_running = 0
        self.fail_restore = False

    async def open(self) -> None:
        pass

    async def close(self) -> None:
        pass

    async def start(self, request: StartAgentRequest) -> object:
        self.start_requests.append(request)
        event_send, event_receive = anyio.create_memory_object_stream[RuntimeEvent](256)
        handle = FakeHandle(event_send, event_receive)
        self.handles.append(handle)
        return handle

    async def prompt(self, handle: object, request: StartRunRequest) -> RuntimeResult:
        fake = self._handle(handle)
        fake.aborted = False
        self.running += 1
        self.maximum_running = max(self.maximum_running, self.running)
        try:
            await fake.event_send.send(RuntimeEvent("run.output.delta", {"text": f"working:{request.prompt}"}))
            await fake.event_send.send(RuntimeEvent("run.tool.started", {"toolName": "read"}))
            if request.prompt == "nontext-output":
                await fake.event_send.send(RuntimeEvent("run.output.delta", {"text": 1}))
            if request.prompt == "block-abort-error":
                fake.abort_error = True
            if request.prompt.startswith("block"):
                await fake.blocked.wait()
            if request.prompt == "bug-run":
                raise TypeError("unexpected runtime bug")
            if request.prompt == "fail":
                raise RuntimeFailure("requested failure")
            if fake.aborted:
                raise RuntimeFailure("aborted")
            if request.prompt in {"event-failure", "event-bug"}:
                fake.event_error = request.prompt
                await fake.event_send.send(RuntimeEvent("run.output.delta", {"text": "event trigger"}))
            return RuntimeResult(
                text=f"result:{request.prompt}",
                usage={"input": 2, "output": 3, "totalTokens": 5, "cost": 0.01},
                restoration={"sessionFile": f"/tmp/{request.run_id}.jsonl"},
            )
        finally:
            self.running -= 1

    async def events(self, handle: object) -> AsyncIterator[RuntimeEvent]:
        fake = self._handle(handle)
        async with fake.event_receive:
            async for event in fake.event_receive:
                yield event
                if fake.event_error == "event-failure":
                    raise RuntimeFailure("event failure")
                if fake.event_error == "event-bug":
                    raise TypeError("event bug")

    async def steer(self, handle: object, message: str) -> None:
        if message == "bug":
            raise TypeError("fixture runtime bug")
        await self._handle(handle).event_send.send(RuntimeEvent("run.output.delta", {"text": f"steer:{message}"}))

    async def follow_up(self, handle: object, message: str) -> None:
        if message == "reject":
            raise RuntimeFailure("rejected")
        fake = self._handle(handle)
        if message == "finish-tool":
            await fake.event_send.send(RuntimeEvent("run.tool.finished", {"toolName": "read"}))
        await fake.event_send.send(RuntimeEvent("run.output.delta", {"text": f"follow:{message}"}))

    async def abort(self, handle: object) -> None:
        fake = self._handle(handle)
        if fake.abort_error:
            raise RuntimeFailure("abort failed")
        fake.aborted = True
        fake.blocked.set()

    async def stop(self, handle: object) -> None:
        fake = self._handle(handle)
        fake.stopped = True
        await fake.event_send.aclose()

    def is_resumable(self, agent: AgentRecord) -> bool:
        return "sessionFile" in agent.restoration

    async def restore(self, agent: AgentRecord, request: StartAgentRequest) -> object:
        if self.fail_restore:
            raise RuntimeFailure("restore failed")
        if "sessionFile" not in agent.restoration:  # pragma: no cover - fixture failure branch
            raise RuntimeFailure("not resumable")
        return await self.start(request)

    def release(self, index: int = -1) -> None:
        self.handles[index].blocked.set()

    @staticmethod
    def _handle(handle: object) -> FakeHandle:
        if not isinstance(handle, FakeHandle):  # pragma: no cover - fixture misuse guard
            raise TypeError("Expected FakeHandle")
        return handle


async def rpc_request(
    client: httpx2.AsyncClient,
    method: str,
    params: dict[str, Any] | None = None,
    request_id: str = "test",
) -> dict[str, Any]:
    response = await client.post(
        "/v1/rpc",
        content=json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}}) + "\n",
        headers={"content-type": "application/x-ndjson"},
    )
    record = JSON_OBJECT_ADAPTER.validate_python(response.json())
    if "error" in record:
        return record
    return JSON_OBJECT_ADAPTER.validate_python(record["result"])


@dataclass
class RunningHub:
    client: httpx2.AsyncClient
    runtime: FakeRuntime
    config: HubConfig

    async def rpc(self, method: str, params: dict[str, Any] | None = None, request_id: str = "test") -> dict[str, Any]:
        return await rpc_request(self.client, method, params, request_id)


@pytest.fixture
async def hub(tmp_path: Path) -> AsyncIterator[RunningHub]:
    runtime = FakeRuntime()
    socket_directory = Path("/tmp") / f"ah-{uuid.uuid4().hex}"
    config = HubConfig(
        data_dir=tmp_path,
        socket_path=socket_directory / "hub.sock",
        global_concurrency=2,
        profiles={
            "task": AgentProfile(name="task", allow_model_override=True, allow_delegation=True),
            "scout": AgentProfile(name="scout", access="read-only"),
            "sticky": AgentProfile(name="sticky", keep_alive=True, idle_timeout_seconds=60),
            "brief": AgentProfile(name="brief", keep_alive=True, idle_timeout_seconds=0.01),
        },
    )
    app = create_app(config, {"pi": runtime})
    if config.socket_path is None:  # pragma: no cover - Pydantic Settings guarantees the path
        raise AssertionError("HubConfig did not create a socket path")
    listener = bind_socket(config.socket_path)
    server = uvicorn.Server(uvicorn.Config(app, http="zttp", log_config=None, access_log=False, lifespan="on"))
    task_group = anyio.create_task_group()
    await task_group.__aenter__()
    task_group.start_soon(serve_uvicorn, server, listener)
    transport = httpx2.AsyncHTTPTransport(uds=str(config.socket_path))
    client = httpx2.AsyncClient(transport=transport, base_url="http://agent-hub")
    for _ in range(100):
        try:
            response = await client.get("/health")
        except httpx2.ConnectError:  # pragma: no cover - startup timing depends on the host
            await anyio.sleep(0.01)
            continue
        if response.status_code == 200:
            break
    else:  # pragma: no cover - reports test infrastructure failure
        raise RuntimeError("Agent Hub test server did not start")
    try:
        yield RunningHub(client, runtime, config)
    finally:
        await client.aclose()
        server.should_exit = True
        await task_group.__aexit__(None, None, None)
        listener.close()
        config.socket_path.unlink(missing_ok=True)
        socket_directory.rmdir()


async def serve_uvicorn(server: uvicorn.Server, listener: Any) -> None:
    await server.serve(sockets=[listener])


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
