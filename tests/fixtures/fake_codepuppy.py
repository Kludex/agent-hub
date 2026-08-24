#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import os
import sys
import uuid
from pathlib import Path
from typing import Any, cast

from acp import PROTOCOL_VERSION, Agent, Client, RequestError, run_agent
from acp.helpers import (
    start_tool_call,
    update_agent_message_text,
    update_agent_thought_text,
    update_tool_call,
)
from acp.schema import (
    AgentCapabilities,
    AllowedOutcome,
    AvailableCommandsUpdate,
    ClientCapabilities,
    EnvVariable,
    Implementation,
    InitializeResponse,
    LoadSessionResponse,
    NewSessionResponse,
    PermissionOption,
    PromptCapabilities,
    PromptResponse,
    ResumeSessionResponse,
    SessionCapabilities,
    SessionResumeCapabilities,
    SetSessionConfigOptionResponse,
    ToolCallUpdate,
    Usage,
)


class FixtureAgent:
    def __init__(self) -> None:
        self.client: Client | None = None
        self.directories: dict[str, Path] = {}
        self.cancelled: dict[str, asyncio.Event] = {}

    def on_connect(self, conn: Client) -> None:
        self.client = conn

    async def initialize(
        self,
        protocol_version: int,
        client_capabilities: ClientCapabilities | None = None,
        client_info: Implementation | None = None,
        **kwargs: Any,
    ) -> InitializeResponse:
        return InitializeResponse(
            protocol_version=PROTOCOL_VERSION,
            agent_capabilities=AgentCapabilities(
                prompt_capabilities=PromptCapabilities(embedded_context=True),
                load_session=not Path(".fixture-no-restoration").exists(),
                session_capabilities=SessionCapabilities(resume=SessionResumeCapabilities()),
            ),
            agent_info=Implementation(name="fake-codepuppy", version="1"),
        )

    async def new_session(self, cwd: str, **kwargs: Any) -> NewSessionResponse:
        session_id = f"fixture_{uuid.uuid4().hex}"
        self.directories[session_id] = Path(cwd)
        self.cancelled[session_id] = asyncio.Event()
        if self.client is not None:
            await self.client.session_update(
                session_id,
                AvailableCommandsUpdate(session_update="available_commands_update", available_commands=[]),
            )
        return NewSessionResponse(session_id=session_id)

    async def load_session(self, session_id: str, cwd: str, **kwargs: Any) -> LoadSessionResponse:
        self.directories[session_id] = Path(cwd)
        self.cancelled[session_id] = asyncio.Event()
        return LoadSessionResponse()

    async def resume_session(self, session_id: str, cwd: str, **kwargs: Any) -> ResumeSessionResponse:
        self.directories[session_id] = Path(cwd)
        self.cancelled[session_id] = asyncio.Event()
        return ResumeSessionResponse()

    async def set_config_option(
        self, config_id: str, session_id: str, value: str | bool, **kwargs: Any
    ) -> SetSessionConfigOptionResponse:
        if value == "invalid-model":
            raise RequestError.invalid_params({"details": "invalid fixture model"})
        if value == "invalid-model-error":
            raise RequestError(-32602, "invalid fixture model data", "invalid data")
        (self.directories[session_id] / ".fixture-codepuppy-model").write_text(str(value), encoding="utf-8")
        return SetSessionConfigOptionResponse(config_options=[])

    async def prompt(self, session_id: str, prompt: list[Any], **kwargs: Any) -> PromptResponse:
        text = str(prompt[0].text)
        if "malformed" in text:
            sys.stdout.write("not-json\n")
            sys.stdout.flush()
            os._exit(2)
        if "crash" in text:
            print("fixture crash", file=sys.stderr, flush=True)
            os._exit(3)
        if "block" in text:
            await self.cancelled[session_id].wait()
            return PromptResponse(stop_reason="cancelled")
        if self.client is None:
            raise RuntimeError("Fixture ACP client is unavailable")
        stderr = "x" * 10_000 if "stderr-overflow" in text else "fixture stderr"
        print(stderr, file=sys.stderr, flush=True)
        await self.client.session_update(
            session_id, AvailableCommandsUpdate(session_update="available_commands_update", available_commands=[])
        )
        await self.client.session_update(session_id, update_agent_thought_text("thinking"))
        raw_input = {"text": "x" * 2048} if "oversized-tool" in text else {"text": text}
        await self.client.session_update(
            session_id,
            start_tool_call("tool-1", "Fixture tool", kind="read", status="in_progress", raw_input=raw_input),
        )
        if "permissions" in text or "tools" in text or "no-options" in text:
            options = [PermissionOption(option_id="reject", name="Reject", kind="reject_once")]
            if "no-options" not in text:
                options.insert(0, PermissionOption(option_id="allow", name="Allow", kind="allow_once"))
            permission = await self.client.request_permission(
                session_id,
                ToolCallUpdate(tool_call_id="tool-1", title="Workspace change", status="in_progress"),
                options,
            )
            allowed = isinstance(permission.outcome, AllowedOutcome)
            if allowed:
                target = self.directories[session_id] / "codepuppy.txt"
                await self.client.write_text_file(session_id, str(target), "written")
                file_text = (await self.client.read_text_file(session_id, str(target))).content
                terminal = await self.client.create_terminal(
                    session_id,
                    "/bin/sh",
                    args=["-c", "printf terminal"],
                    cwd=str(self.directories[session_id]),
                )
                await self.client.wait_for_terminal_exit(session_id, terminal.terminal_id)
                terminal_text = (await self.client.terminal_output(session_id, terminal.terminal_id)).output
                await self.client.release_terminal(session_id, terminal.terminal_id)
                text = f"allowed:{file_text}:{terminal_text}"
            else:
                text = "denied"
        if "force-write" in text:
            try:
                await self.client.write_text_file(session_id, str(self.directories[session_id] / "forced.txt"), "no")
            except RequestError:
                text = "write-blocked"
        if "force-terminal" in text:
            try:
                await self.client.create_terminal(session_id, "/bin/sh", args=["-c", "true"])
            except RequestError:
                text = "terminal-blocked"
        if "terminal-outside" in text:
            try:
                await self.client.create_terminal(session_id, "/bin/sh", cwd="/")
            except RequestError:
                text = "terminal-outside-blocked"
        if "terminal-invalid-cwd" in text:
            invalid_cwd = self.directories[session_id] / "not-a-directory"
            invalid_cwd.write_text("file", encoding="utf-8")
            try:
                await self.client.create_terminal(session_id, "/bin/sh", cwd=str(invalid_cwd))
            except RequestError:
                text = "cwd-blocked"
        if "terminal-kill" in text:
            terminal = await self.client.create_terminal(
                session_id,
                "/bin/sh",
                args=["-c", "printf 12345678; sleep 5"],
                env=[EnvVariable(name="FIXTURE", value="1")],
                output_byte_limit=4,
            )
            await asyncio.sleep(0.05)
            await self.client.terminal_output(session_id, terminal.terminal_id)
            await self.client.kill_terminal(session_id, terminal.terminal_id)
            await self.client.wait_for_terminal_exit(session_id, terminal.terminal_id)
            text = (await self.client.terminal_output(session_id, terminal.terminal_id)).output
            await self.client.release_terminal(session_id, terminal.terminal_id)
        if "terminal-leak" in text:
            await self.client.create_terminal(session_id, "/bin/sh", args=["-c", "sleep 5"])
            text = "terminal-created"
        if "unknown-terminal" in text:
            try:
                await self.client.terminal_output(session_id, "missing")
            except RequestError:
                text = "terminal-missing"
        if "outside" in text and "terminal-outside" not in text:
            try:
                await self.client.read_text_file(session_id, "/etc/passwd")
            except RequestError:
                text = "outside-blocked"
        await self.client.session_update(
            session_id,
            update_tool_call("tool-1", status="completed", raw_output={"text": text}),
        )
        await self.client.session_update(session_id, update_agent_message_text(f"result:{text}"))
        return PromptResponse(
            stop_reason="end_turn",
            usage=Usage(input_tokens=4, output_tokens=2, total_tokens=6),
        )

    async def cancel(self, session_id: str, **kwargs: Any) -> None:
        self.cancelled[session_id].set()


asyncio.run(run_agent(cast(Agent, FixtureAgent())))
