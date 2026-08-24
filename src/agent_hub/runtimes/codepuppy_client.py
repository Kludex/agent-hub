from __future__ import annotations

import contextlib
import json
from pathlib import Path
from typing import Any

import anyio
from acp import RequestError
from acp.schema import (
    AgentMessageChunk,
    AgentThoughtChunk,
    AllowedOutcome,
    ClientCapabilities,
    CreateElicitationResponse,
    CreateTerminalResponse,
    DeniedOutcome,
    EnvVariable,
    FileSystemCapabilities,
    KillTerminalResponse,
    PermissionOption,
    ReadTextFileResponse,
    ReleaseTerminalResponse,
    RequestPermissionResponse,
    TerminalOutputResponse,
    ToolCallProgress,
    ToolCallStart,
    ToolCallUpdate,
    WaitForTerminalExitResponse,
    WriteTextFileResponse,
)
from anyio.abc import TaskGroup
from anyio.streams.memory import MemoryObjectSendStream

from agent_hub.runtimes.base import RuntimeEvent
from agent_hub.runtimes.codepuppy_terminal import CodePuppyTerminals


class CodePuppyClient:
    def __init__(
        self,
        workspace: Path,
        access: str,
        event_send: MemoryObjectSendStream[RuntimeEvent],
        task_group: TaskGroup,
        max_output_bytes: int,
    ) -> None:
        self.workspace = workspace.resolve(strict=True)
        self.access = access
        self.event_send = event_send
        self.max_output_bytes = max_output_bytes
        self.output: list[str] = []
        self.events_enabled = False
        self.terminals = CodePuppyTerminals(self.workspace, access, task_group, max_output_bytes)

    @property
    def capabilities(self) -> ClientCapabilities:
        return ClientCapabilities(
            fs=FileSystemCapabilities(read_text_file=True, write_text_file=True),
            terminal=True,
        )

    def enable_events(self) -> None:
        self.events_enabled = True

    def begin_prompt(self) -> None:
        self.output.clear()

    async def request_permission(
        self,
        session_id: str,
        tool_call: ToolCallUpdate,
        options: list[PermissionOption],
        **kwargs: Any,
    ) -> RequestPermissionResponse:
        if self.access == "read-only":
            return RequestPermissionResponse(outcome=DeniedOutcome(outcome="cancelled"))
        option = next((item for item in options if item.kind in {"allow_once", "allow_always"}), None)
        if option is None:
            return RequestPermissionResponse(outcome=DeniedOutcome(outcome="cancelled"))
        return RequestPermissionResponse(outcome=AllowedOutcome(outcome="selected", option_id=option.option_id))

    async def session_update(self, session_id: str, update: Any, **kwargs: Any) -> None:
        if not self.events_enabled:
            return
        event = self._normalize(update)
        if event is None:
            return
        if event.type == "run.output.delta":
            text = event.data.get("text")
            if isinstance(text, str):
                self.output.append(text)
        with contextlib.suppress(anyio.BrokenResourceError, anyio.ClosedResourceError):
            await self.event_send.send(event)

    async def write_text_file(self, session_id: str, path: str, content: str, **kwargs: Any) -> WriteTextFileResponse:
        if self.access == "read-only":
            raise RequestError.invalid_request({"details": "CodePuppy profile is read-only"})
        target = self._path(path, must_exist=False)
        target.parent.mkdir(parents=True, exist_ok=True)
        await anyio.Path(target).write_text(content, encoding="utf-8")
        return WriteTextFileResponse()

    async def read_text_file(
        self,
        session_id: str,
        path: str,
        line: int | None = None,
        limit: int | None = None,
        **kwargs: Any,
    ) -> ReadTextFileResponse:
        content = await anyio.Path(self._path(path, must_exist=True)).read_text(encoding="utf-8", errors="replace")
        lines = content.splitlines(keepends=True)
        start = max((line or 1) - 1, 0)
        selected = lines[start:] if limit is None else lines[start : start + limit]
        bounded = "".join(selected).encode("utf-8")[: self.max_output_bytes]
        return ReadTextFileResponse(content=bounded.decode("utf-8", errors="ignore"))

    async def create_terminal(
        self,
        session_id: str,
        command: str,
        args: list[str] | None = None,
        env: list[EnvVariable] | None = None,
        cwd: str | None = None,
        output_byte_limit: int | None = None,
        **kwargs: Any,
    ) -> CreateTerminalResponse:
        return await self.terminals.create(command, args, env, cwd, output_byte_limit)

    async def terminal_output(self, session_id: str, terminal_id: str, **kwargs: Any) -> TerminalOutputResponse:
        return await self.terminals.output(terminal_id)

    async def release_terminal(self, session_id: str, terminal_id: str, **kwargs: Any) -> ReleaseTerminalResponse:
        return await self.terminals.release(terminal_id)

    async def wait_for_terminal_exit(
        self, session_id: str, terminal_id: str, **kwargs: Any
    ) -> WaitForTerminalExitResponse:
        return await self.terminals.wait(terminal_id)

    async def kill_terminal(self, session_id: str, terminal_id: str, **kwargs: Any) -> KillTerminalResponse:
        return await self.terminals.kill(terminal_id)

    async def create_elicitation(self, message: str, mode: Any, **kwargs: Any) -> CreateElicitationResponse:
        raise RequestError.method_not_found(  # pragma: no cover - capability is not advertised
            "session/request_elicitation"
        )

    async def complete_elicitation(self, elicitation_id: str, **kwargs: Any) -> None:
        raise RequestError.method_not_found(  # pragma: no cover - capability is not advertised
            "session/complete_elicitation"
        )

    async def ext_method(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        raise RequestError.method_not_found(method)  # pragma: no cover - CodePuppy uses no ACP extensions

    async def ext_notification(self, method: str, params: dict[str, Any]) -> None:
        raise RequestError.method_not_found(method)  # pragma: no cover - CodePuppy uses no ACP extensions

    def on_connect(self, conn: Any) -> None:
        pass

    async def close(self) -> None:
        await self.terminals.close()

    def _normalize(self, update: Any) -> RuntimeEvent | None:
        if isinstance(update, AgentMessageChunk):
            text = getattr(update.content, "text", None)
            return RuntimeEvent("run.output.delta", {"text": text}) if isinstance(text, str) else None
        if isinstance(update, AgentThoughtChunk):
            text = getattr(update.content, "text", None)
            return RuntimeEvent("run.thinking.delta", {"text": text}) if isinstance(text, str) else None
        if isinstance(update, (ToolCallStart, ToolCallProgress)):
            data = update.model_dump(mode="json", by_alias=True, exclude_none=True)
            data.pop("sessionUpdate", None)
            if len(json.dumps(data, ensure_ascii=False).encode("utf-8")) > self.max_output_bytes:
                data = {
                    "toolCallId": update.tool_call_id,
                    "toolName": update.title or "CodePuppy tool",
                    "status": update.status,
                    "truncated": True,
                }
            event_type = "run.tool.started" if isinstance(update, ToolCallStart) else "run.tool.updated"
            if isinstance(update, ToolCallProgress) and update.status in {"completed", "failed"}:
                event_type = "run.tool.finished"
            return RuntimeEvent(event_type, data)
        return None

    def _path(self, value: str, *, must_exist: bool) -> Path:
        path = Path(value)
        candidate = (
            (self.workspace / path).resolve(strict=must_exist)
            if not path.is_absolute()
            else path.resolve(strict=must_exist)
        )
        if not candidate.is_relative_to(self.workspace):
            raise RequestError.invalid_params({"details": f"Path is outside the assigned workspace: {value}"})
        return candidate
