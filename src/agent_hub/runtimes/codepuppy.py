from __future__ import annotations

import contextlib
import os
import shutil
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import anyio
from acp import PROTOCOL_VERSION, Agent, RequestError, spawn_agent_process, text_block
from acp.schema import Implementation
from anyio.abc import TaskGroup
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream
from pydantic import BaseModel, ValidationError

from agent_hub.models import AgentRecord
from agent_hub.runtimes.base import RuntimeEvent, RuntimeFailure, RuntimeResult, StartAgentRequest, StartRunRequest
from agent_hub.runtimes.codepuppy_client import CodePuppyClient

if TYPE_CHECKING:
    from asyncio.subprocess import Process

    from acp.core import ClientSideConnection


class _RequestErrorData(BaseModel):
    details: str | None = None


@dataclass
class CodePuppyHandle:
    agent_id: str
    connection: Agent
    process: Process
    process_context: AbstractAsyncContextManager[tuple[ClientSideConnection, Process]]
    client: CodePuppyClient
    session_id: str
    profile_instructions: str | None
    event_send: MemoryObjectSendStream[RuntimeEvent]
    event_receive: MemoryObjectReceiveStream[RuntimeEvent]
    stderr_done: anyio.Event
    instructions_sent: bool = False
    active: bool = False
    closed: bool = False


class CodePuppyRuntime:
    def __init__(
        self,
        executable: str = "code-puppy",
        *,
        socket_path: Path | None = None,
        process_shutdown_seconds: float = 5,
        max_record_bytes: int = 1024 * 1024,
        max_output_bytes: int = 50 * 1024,
    ) -> None:
        self._executable = executable
        self._socket_path = socket_path
        self._process_shutdown_seconds = process_shutdown_seconds
        self._max_record_bytes = max_record_bytes
        self._max_output_bytes = max_output_bytes
        self._task_group: TaskGroup | None = None

    async def open(self) -> None:
        if self._task_group is not None:
            raise RuntimeError("CodePuppy runtime is already open")
        self._task_group = anyio.create_task_group()
        await self._task_group.__aenter__()

    async def close(self) -> None:
        if self._task_group is not None:
            await self._task_group.__aexit__(None, None, None)
            self._task_group = None

    async def start(self, request: StartAgentRequest) -> object:
        return await self._start(request)

    async def prompt(self, handle: object, request: StartRunRequest) -> RuntimeResult:
        puppy = self._handle(handle)
        puppy.client.begin_prompt()
        prompt = request.prompt
        if puppy.profile_instructions is not None and not puppy.instructions_sent:
            prompt = f"{puppy.profile_instructions}\n\n{prompt}"
            puppy.instructions_sent = True
        puppy.active = True
        try:
            response = await puppy.connection.prompt(puppy.session_id, [text_block(prompt)])
        except (ConnectionError, RequestError, ValidationError) as exc:
            raise RuntimeFailure(self._error("CodePuppy prompt failed", exc)) from exc
        finally:
            puppy.active = False
        if response.stop_reason == "cancelled":
            raise RuntimeFailure("CodePuppy run was aborted")
        usage: dict[str, Any] = {}
        if response.usage is not None:
            usage = response.usage.model_dump(mode="json", by_alias=True, exclude_none=True)
            await puppy.event_send.send(RuntimeEvent("run.usage.updated", usage))
        return RuntimeResult(
            text="".join(puppy.client.output),
            usage=usage,
            restoration={"sessionId": puppy.session_id},
        )

    async def events(self, handle: object) -> AsyncIterator[RuntimeEvent]:
        puppy = self._handle(handle)
        async with puppy.event_receive:
            async for event in puppy.event_receive:
                yield event

    async def steer(self, handle: object, message: str) -> None:
        self._handle(handle)
        raise RuntimeFailure("CodePuppy ACP does not support steering an active prompt")

    async def follow_up(self, handle: object, message: str) -> None:
        self._handle(handle)
        raise RuntimeFailure("CodePuppy ACP does not support queuing a follow-up during an active prompt")

    async def abort(self, handle: object) -> None:
        puppy = self._handle(handle)
        if puppy.active:
            with contextlib.suppress(ConnectionError, RequestError):
                await puppy.connection.cancel(puppy.session_id)

    async def stop(self, handle: object) -> None:
        puppy = self._handle(handle)
        if puppy.closed:
            return
        puppy.closed = True
        await self.abort(puppy)
        await puppy.client.close()
        await puppy.process_context.__aexit__(None, None, None)
        await puppy.stderr_done.wait()
        await puppy.event_send.aclose()
        await puppy.event_receive.aclose()

    def is_resumable(self, agent: AgentRecord) -> bool:
        return isinstance(agent.restoration.get("sessionId"), str)

    async def restore(self, agent: AgentRecord, request: StartAgentRequest) -> object:
        session_id = agent.restoration.get("sessionId")
        if not isinstance(session_id, str):
            raise RuntimeFailure("CodePuppy agent has no ACP session to restore")
        return await self._start(request, session_id)

    async def _start(self, request: StartAgentRequest, session_id: str | None = None) -> CodePuppyHandle:
        if shutil.which(self._executable) is None:
            raise RuntimeFailure(f"CodePuppy executable not found: {self._executable}")
        if self._task_group is None:
            raise RuntimeError("CodePuppy runtime is not open")
        request.session_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        request.session_directory.chmod(0o700)
        event_send, event_receive = anyio.create_memory_object_stream[RuntimeEvent](256)
        client = CodePuppyClient(
            request.cwd,
            request.access or request.profile.access,
            event_send,
            self._task_group,
            self._max_output_bytes,
        )
        process_context = spawn_agent_process(
            client,
            self._executable,
            "--acp",
            cwd=request.cwd,
            env=self._environment(request.agent_id),
            transport_kwargs={
                "limit": self._max_record_bytes,
                "shutdown_timeout": self._process_shutdown_seconds,
            },
        )
        try:
            connection, process = await process_context.__aenter__()
        except OSError as exc:
            await event_send.aclose()
            await event_receive.aclose()
            raise RuntimeFailure(f"Could not start CodePuppy: {exc}") from exc
        stderr_done = anyio.Event()
        handle = CodePuppyHandle(
            request.agent_id,
            connection,
            process,
            process_context,
            client,
            session_id or "",
            request.profile.instructions,
            event_send,
            event_receive,
            stderr_done,
            instructions_sent=session_id is not None,
        )
        self._task_group.start_soon(self._read_stderr, handle)
        try:
            initialized = await connection.initialize(
                PROTOCOL_VERSION,
                client.capabilities,
                Implementation(name="agent-hub", title="Agent Hub", version="0.1.0"),
            )
            if session_id is None:
                session = await connection.new_session(cwd=str(request.cwd))
                handle.session_id = session.session_id
            else:
                if initialized.agent_capabilities is None or initialized.agent_capabilities.load_session is not True:
                    raise RuntimeFailure("CodePuppy does not support ACP session restoration")
                await connection.load_session(session_id=session_id, cwd=str(request.cwd))
            model = request.model or request.profile.model
            if model is not None:
                await connection.set_config_option("model", handle.session_id, model)
            client.enable_events()
        except (ConnectionError, RequestError, RuntimeFailure, ValidationError) as exc:
            await process_context.__aexit__(type(exc), exc, exc.__traceback__)
            await stderr_done.wait()
            await event_send.aclose()
            await event_receive.aclose()
            raise RuntimeFailure(self._error("CodePuppy startup failed", exc)) from exc
        return handle

    async def _read_stderr(self, handle: CodePuppyHandle) -> None:
        stream = handle.process.stderr
        read = 0
        try:
            if stream is None:  # pragma: no cover - ACP subprocesses always use a stderr pipe
                return
            while chunk := await stream.read(4096):
                remaining = self._max_output_bytes - read
                if remaining <= 0:
                    continue
                bounded = chunk[:remaining]
                read += len(bounded)
                with contextlib.suppress(anyio.BrokenResourceError, anyio.ClosedResourceError):
                    await handle.event_send.send(
                        RuntimeEvent("runtime.stderr", {"text": bounded.decode("utf-8", errors="replace")})
                    )
        finally:
            handle.stderr_done.set()

    def _environment(self, agent_id: str) -> dict[str, str]:
        allowed = {"HOME", "PATH", "SHELL", "TMPDIR", "USER", "LANG", "LC_ALL", "TERM"}
        environment = {key: value for key, value in os.environ.items() if key in allowed or key.endswith("_API_KEY")}
        environment["AGENT_HUB_PARENT_AGENT_ID"] = agent_id
        if self._socket_path is not None:
            environment["AGENT_HUB_SOCKET"] = str(self._socket_path)
        return environment

    @staticmethod
    def _error(prefix: str, error: RequestError | ConnectionError | RuntimeFailure | ValidationError) -> str:
        detail = None
        if isinstance(error, RequestError):
            try:
                detail = _RequestErrorData.model_validate(error.data).details
            except ValidationError:
                pass
        return f"{prefix}: {detail or error}"

    @staticmethod
    def _handle(handle: object) -> CodePuppyHandle:
        if not isinstance(handle, CodePuppyHandle):
            raise TypeError("Expected a CodePuppy runtime handle")
        return handle
