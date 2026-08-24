from __future__ import annotations

import contextlib
import json
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import anyio
import logfire
from anyio.abc import Process, TaskGroup
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream
from pydantic import ValidationError

from agent_hub.json_data import JSONValue, parse_json
from agent_hub.models import AgentRecord
from agent_hub.runtimes.base import RuntimeEvent, RuntimeFailure, RuntimeResult, StartAgentRequest, StartRunRequest


@dataclass
class CommandWaiter:
    ready: anyio.Event = field(default_factory=anyio.Event)
    value: dict[str, JSONValue] | None = None
    error: RuntimeFailure | None = None


@dataclass
class PiHandle:
    agent_id: str
    process: Process
    event_send: MemoryObjectSendStream[RuntimeEvent]
    event_receive: MemoryObjectReceiveStream[RuntimeEvent]
    responses: dict[str, CommandWaiter] = field(default_factory=dict[str, CommandWaiter])
    settled: anyio.Event = field(default_factory=anyio.Event)
    write_lock: anyio.Lock = field(default_factory=anyio.Lock)
    protocol_error: str | None = None
    command_number: int = 0
    stderr_bytes: int = 0
    readers_remaining: int = 2
    readers_done: anyio.Event = field(default_factory=anyio.Event)
    readers_finished: bool = False


class PiRuntime:
    def __init__(
        self,
        executable: str = "pi",
        *,
        shutdown_grace_seconds: float = 2,
        process_shutdown_seconds: float = 5,
        socket_path: Path | None = None,
        max_record_bytes: int = 1024 * 1024,
        max_stderr_bytes: int = 64 * 1024,
    ) -> None:
        self._executable = executable
        self._shutdown_grace_seconds = shutdown_grace_seconds
        self._process_shutdown_seconds = process_shutdown_seconds
        self._socket_path = socket_path
        self._max_record_bytes = max_record_bytes
        self._max_stderr_bytes = max_stderr_bytes
        self._task_group: TaskGroup | None = None

    async def open(self) -> None:
        if self._task_group is not None:
            raise RuntimeError("Pi runtime is already open")
        self._task_group = anyio.create_task_group()
        await self._task_group.__aenter__()

    async def close(self) -> None:
        if self._task_group is not None:
            await self._task_group.__aexit__(None, None, None)
            self._task_group = None

    async def start(self, request: StartAgentRequest) -> object:
        request.session_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        request.session_directory.chmod(0o700)
        arguments = ["--mode", "rpc", "--session-dir", str(request.session_directory)]
        if request.profile.instructions is not None:
            arguments.extend(["--append-system-prompt", request.profile.instructions])
        model = request.model or request.profile.model
        if model is not None:
            arguments.extend(["--model", model])
        if (request.access or request.profile.access) == "read-only":
            arguments.extend(["--tools", "read,grep,find,ls"])
        try:
            process = await anyio.open_process(
                [self._executable, *arguments],
                cwd=request.cwd,
                env=self._environment(request.agent_id),
            )
        except OSError as exc:
            raise RuntimeFailure(f"Could not start Pi: {exc}") from exc
        if self._task_group is None:
            process.kill()
            await process.wait()
            raise RuntimeError("Pi runtime is not open")
        event_send, event_receive = anyio.create_memory_object_stream[RuntimeEvent](256)
        handle = PiHandle(request.agent_id, process, event_send, event_receive)
        self._task_group.start_soon(self._reader, handle, self._read_stdout)
        self._task_group.start_soon(self._reader, handle, self._read_stderr)
        return handle

    async def prompt(self, handle: object, request: StartRunRequest) -> RuntimeResult:
        pi = self._handle(handle)
        pi.settled = anyio.Event()
        await self._command(pi, "prompt", message=request.prompt)
        winner: list[str] = []

        async def wait_until_settled(task_group: TaskGroup) -> None:
            await pi.settled.wait()
            winner.append("settled")
            task_group.cancel_scope.cancel()

        async def wait_until_exited(task_group: TaskGroup) -> None:
            await pi.process.wait()
            winner.append("exited")
            task_group.cancel_scope.cancel()

        async with anyio.create_task_group() as task_group:
            task_group.start_soon(wait_until_settled, task_group)
            task_group.start_soon(wait_until_exited, task_group)
        if not winner or winner[0] == "exited" or pi.protocol_error is not None:
            reason = pi.protocol_error or f"Pi exited with status {pi.process.returncode}"
            raise RuntimeFailure(reason)
        responses: dict[str, dict[str, JSONValue]] = {}

        async def collect(key: str, command: str) -> None:
            responses[key] = await self._command(pi, command)

        async with anyio.create_task_group() as task_group:
            task_group.start_soon(collect, "text", "get_last_assistant_text")
            task_group.start_soon(collect, "stats", "get_session_stats")
            task_group.start_soon(collect, "state", "get_state")
        text_value = responses["text"].get("text")
        text = text_value if isinstance(text_value, str) else ""
        state = responses["state"]
        restoration = {"sessionFile": state.get("sessionFile"), "sessionId": state.get("sessionId")}
        return RuntimeResult(text=text, usage=responses["stats"], restoration=restoration)

    async def events(self, handle: object) -> AsyncIterator[RuntimeEvent]:
        pi = self._handle(handle)
        async with pi.event_receive:
            async for event in pi.event_receive:
                yield event

    async def steer(self, handle: object, message: str) -> None:
        await self._command(self._handle(handle), "steer", message=message)

    async def follow_up(self, handle: object, message: str) -> None:
        await self._command(self._handle(handle), "follow_up", message=message)

    async def abort(self, handle: object) -> None:
        await self._command(self._handle(handle), "abort")

    async def stop(self, handle: object) -> None:
        pi = self._handle(handle)
        if pi.process.returncode is not None:
            await self._finish_readers(pi)
            return
        if pi.protocol_error is not None:  # pragma: no cover - protocol failure already kills the process
            pi.process.kill()
            await pi.process.wait()
            await self._finish_readers(pi)
            return
        with contextlib.suppress(RuntimeFailure):
            with anyio.move_on_after(self._shutdown_grace_seconds):
                logfire.info("Aborting Pi agent {agent_id} before shutdown", agent_id=pi.agent_id)
                await self.abort(pi)
                await pi.settled.wait()
        if pi.process.returncode is None:
            logfire.info("Sending SIGTERM to Pi agent {agent_id}", agent_id=pi.agent_id)
            pi.process.terminate()
            with anyio.move_on_after(self._process_shutdown_seconds) as shutdown_scope:
                await pi.process.wait()
            if shutdown_scope.cancel_called:
                logfire.info("Sending SIGKILL to Pi agent {agent_id}", agent_id=pi.agent_id)
                pi.process.kill()
                await pi.process.wait()
        await self._finish_readers(pi)

    def is_resumable(self, agent: AgentRecord) -> bool:
        session_file = agent.restoration.get("sessionFile")
        return isinstance(session_file, str) and Path(session_file).is_file()

    async def restore(self, agent: AgentRecord, request: StartAgentRequest) -> object:
        handle = await self.start(request)
        session_file = agent.restoration.get("sessionFile")
        if not isinstance(session_file, str):
            await self.stop(handle)
            raise RuntimeFailure("Pi agent has no session file to restore")
        response = await self._command(self._handle(handle), "switch_session", sessionPath=session_file)
        if response.get("cancelled") is True:
            await self.stop(handle)
            raise RuntimeFailure("Pi session restoration was cancelled")
        return handle

    async def _command(self, handle: PiHandle, command: str, **values: Any) -> dict[str, JSONValue]:
        if handle.process.returncode is not None or handle.process.stdin is None:
            raise RuntimeFailure("Pi process is not running")
        handle.command_number += 1
        command_id = f"hub_{handle.command_number}"
        waiter = CommandWaiter()
        handle.responses[command_id] = waiter
        record = json.dumps({"id": command_id, "type": command, **values}, ensure_ascii=False).encode() + b"\n"
        try:
            async with handle.write_lock:
                await handle.process.stdin.send(record)
            await waiter.ready.wait()
        except (  # pragma: no cover - OS-level write race covered by process-exit integration tests
            anyio.BrokenResourceError,
            anyio.ClosedResourceError,
        ) as exc:
            raise RuntimeFailure("Pi RPC connection closed") from exc
        finally:
            handle.responses.pop(command_id, None)
        if waiter.error is not None:
            raise waiter.error
        response = waiter.value
        if response is None:  # pragma: no cover - waiters complete with either a value or an error
            raise RuntimeFailure("Pi RPC command returned no response")
        if not response.get("success", False):
            raise RuntimeFailure(str(response.get("error", f"Pi command {command!r} failed")))
        data = response.get("data")
        return data if isinstance(data, dict) else {}

    async def _read_stdout(self, handle: PiHandle) -> None:
        stream = handle.process.stdout
        if stream is None:  # pragma: no cover - open_process always creates a stdout pipe
            return
        buffer = b""
        async for chunk in stream:
            buffer += chunk
            if len(buffer) > self._max_record_bytes and b"\n" not in buffer:
                await self._protocol_failure(handle, "Pi JSONL record exceeds the configured limit")
                return
            while b"\n" in buffer:
                raw, buffer = buffer.split(b"\n", 1)
                if len(raw) > self._max_record_bytes:
                    await self._protocol_failure(handle, "Pi JSONL record exceeds the configured limit")
                    return
                if raw.endswith(b"\r"):
                    raw = raw[:-1]
                if raw:
                    await self._receive(handle, raw)
            if len(buffer) > self._max_record_bytes:
                await self._protocol_failure(handle, "Pi JSONL record exceeds the configured limit")
                return
        if buffer:
            await self._protocol_failure(handle, "Pi stdout ended with an incomplete JSONL record")
        for waiter in handle.responses.values():
            if not waiter.ready.is_set():
                waiter.error = RuntimeFailure("Pi RPC connection closed")
                waiter.ready.set()

    async def _receive(self, handle: PiHandle, raw: bytes) -> None:
        try:
            record = parse_json(raw)
        except ValidationError:
            await self._protocol_failure(handle, "Pi emitted malformed JSON")
            return
        if not isinstance(record, dict):
            await self._protocol_failure(handle, "Pi emitted a non-object record")
            return
        command_id = record.get("id")
        if record.get("type") == "response" and isinstance(command_id, str):
            waiter = handle.responses.get(command_id)
            if waiter is not None and not waiter.ready.is_set():
                waiter.value = record
                waiter.ready.set()
            return
        if record.get("type") == "agent_settled":
            handle.settled.set()
        for event in self._normalize(record):
            await handle.event_send.send(event)

    async def _protocol_failure(self, handle: PiHandle, message: str) -> None:
        handle.protocol_error = message
        handle.settled.set()
        with contextlib.suppress(anyio.BrokenResourceError, anyio.ClosedResourceError):
            await handle.event_send.send(RuntimeEvent("run.error", {"message": message}))
        for waiter in handle.responses.values():
            if not waiter.ready.is_set():
                waiter.error = RuntimeFailure(message)
                waiter.ready.set()
        if handle.process.returncode is None:
            handle.process.kill()

    async def _read_stderr(self, handle: PiHandle) -> None:
        stream = handle.process.stderr
        if stream is None:  # pragma: no cover - open_process always creates a stderr pipe
            return
        async for chunk in stream:
            remaining = self._max_stderr_bytes - handle.stderr_bytes
            if remaining <= 0:
                continue
            bounded = chunk[:remaining]
            handle.stderr_bytes += len(bounded)
            with contextlib.suppress(anyio.BrokenResourceError, anyio.ClosedResourceError):
                await handle.event_send.send(
                    RuntimeEvent("runtime.stderr", {"text": bounded.decode("utf-8", errors="replace")})
                )

    async def _reader(self, handle: PiHandle, reader: Callable[[PiHandle], Awaitable[None]]) -> None:
        try:
            await reader(handle)
        finally:
            handle.readers_remaining -= 1
            if handle.readers_remaining == 0:
                handle.readers_done.set()

    async def _finish_readers(self, handle: PiHandle) -> None:
        if handle.readers_finished:
            return
        handle.readers_finished = True
        await handle.readers_done.wait()
        await handle.event_send.aclose()
        await handle.event_receive.aclose()

    @staticmethod
    def _normalize(value: dict[str, JSONValue]) -> list[RuntimeEvent]:
        event_type = value.get("type")
        if event_type == "message_update":
            events: list[RuntimeEvent] = []
            delta = value.get("assistantMessageEvent")
            if isinstance(delta, dict) and delta.get("type") == "text_delta":
                events.append(RuntimeEvent("run.output.delta", {"text": delta.get("delta", "")}))
            elif isinstance(delta, dict) and delta.get("type") == "thinking_delta":
                events.append(RuntimeEvent("run.thinking.delta", {"text": delta.get("delta", "")}))
            usage = value.get("usage")
            if isinstance(usage, dict):
                events.append(RuntimeEvent("run.usage.updated", usage))
            return events
        mapping = {
            "tool_execution_start": "run.tool.started",
            "tool_execution_update": "run.tool.updated",
            "tool_execution_end": "run.tool.finished",
            "extension_error": "run.error",
        }
        normalized = mapping.get(event_type) if isinstance(event_type, str) else None
        if normalized is None:
            return []
        return [RuntimeEvent(normalized, {key: item for key, item in value.items() if key != "type"})]

    def _environment(self, agent_id: str) -> dict[str, str]:
        allowed = {"HOME", "PATH", "SHELL", "TMPDIR", "USER", "LANG", "LC_ALL", "TERM"}
        environment = {key: value for key, value in os.environ.items() if key in allowed or key.endswith("_API_KEY")}
        environment["AGENT_HUB_PARENT_AGENT_ID"] = agent_id
        if self._socket_path is not None:
            environment["AGENT_HUB_SOCKET"] = str(self._socket_path)
        return environment

    @staticmethod
    def _handle(handle: object) -> PiHandle:
        if not isinstance(handle, PiHandle):
            raise TypeError("Expected a Pi runtime handle")
        return handle
