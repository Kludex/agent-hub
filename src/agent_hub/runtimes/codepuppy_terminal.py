from __future__ import annotations

import os
import signal
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import anyio
from acp import RequestError
from acp.schema import (
    CreateTerminalResponse,
    EnvVariable,
    KillTerminalResponse,
    ReleaseTerminalResponse,
    TerminalExitStatus,
    TerminalOutputResponse,
    WaitForTerminalExitResponse,
)
from anyio.abc import ByteReceiveStream, Process, TaskGroup


@dataclass
class TerminalHandle:
    process: Process
    limit: int
    output: bytearray = field(default_factory=bytearray)
    truncated: bool = False
    readers: int = 2
    readers_done: anyio.Event = field(default_factory=anyio.Event)


class CodePuppyTerminals:
    def __init__(self, workspace: Path, access: str, task_group: TaskGroup, max_output_bytes: int) -> None:
        self.workspace = workspace
        self.access = access
        self.task_group = task_group
        self.max_output_bytes = max_output_bytes
        self.terminals: dict[str, TerminalHandle] = {}

    async def create(
        self,
        command: str,
        args: list[str] | None,
        env: list[EnvVariable] | None,
        cwd: str | None,
        output_byte_limit: int | None,
    ) -> CreateTerminalResponse:
        if self.access == "read-only":
            raise RequestError.invalid_request({"details": "CodePuppy profile is read-only"})
        directory = self.workspace if cwd is None else self._path(cwd)
        if not directory.is_dir():
            raise RequestError.invalid_params({"details": "Terminal cwd is not a directory"})
        environment = self._environment()
        for variable in env or []:
            environment[variable.name] = variable.value
        process = await anyio.open_process(
            [command, *(args or [])],
            cwd=directory,
            env=environment,
            start_new_session=os.name != "nt",
        )
        limit = min(output_byte_limit or self.max_output_bytes, self.max_output_bytes)
        terminal_id = f"term_{uuid.uuid4().hex}"
        terminal = TerminalHandle(process, limit)
        self.terminals[terminal_id] = terminal
        self.task_group.start_soon(self._collect, terminal, process.stdout)
        self.task_group.start_soon(self._collect, terminal, process.stderr)
        return CreateTerminalResponse(terminal_id=terminal_id)

    async def output(self, terminal_id: str) -> TerminalOutputResponse:
        terminal = self._terminal(terminal_id)
        return TerminalOutputResponse(
            output=bytes(terminal.output).decode("utf-8", errors="replace"),
            truncated=terminal.truncated,
            exit_status=self._exit_status(terminal.process.returncode),
        )

    async def release(self, terminal_id: str) -> ReleaseTerminalResponse:
        terminal = self._terminal(terminal_id)
        await self._finish(terminal)
        self.terminals.pop(terminal_id, None)
        return ReleaseTerminalResponse()

    async def wait(self, terminal_id: str) -> WaitForTerminalExitResponse:
        terminal = self._terminal(terminal_id)
        returncode = await terminal.process.wait()
        await terminal.readers_done.wait()
        status = self._exit_status(returncode)
        return WaitForTerminalExitResponse(
            exit_code=status.exit_code if status is not None else None,
            signal=status.signal if status is not None else None,
        )

    async def kill(self, terminal_id: str) -> KillTerminalResponse:
        terminal = self._terminal(terminal_id)
        if terminal.process.returncode is None:
            self._kill_process(terminal.process)
            await terminal.process.wait()
        return KillTerminalResponse()

    async def close(self) -> None:
        for terminal in list(self.terminals.values()):
            await self._finish(terminal)
        self.terminals.clear()

    async def _collect(self, terminal: TerminalHandle, stream: ByteReceiveStream | None) -> None:
        try:
            if stream is not None:
                async for chunk in stream:
                    terminal.output.extend(chunk)
                    if len(terminal.output) > terminal.limit:
                        terminal.truncated = True
                        del terminal.output[: len(terminal.output) - terminal.limit]
        except (  # pragma: no cover - OS-level stream closure race
            anyio.BrokenResourceError,
            anyio.ClosedResourceError,
        ):
            pass
        finally:
            terminal.readers -= 1
            if terminal.readers == 0:
                terminal.readers_done.set()

    async def _finish(self, terminal: TerminalHandle) -> None:
        if terminal.process.returncode is None:
            self._kill_process(terminal.process)
        await terminal.process.wait()
        await terminal.readers_done.wait()

    def _path(self, value: str) -> Path:
        path = Path(value)
        candidate = (
            (self.workspace / path).resolve(strict=True) if not path.is_absolute() else path.resolve(strict=True)
        )
        if not candidate.is_relative_to(self.workspace):
            raise RequestError.invalid_params({"details": f"Path is outside the assigned workspace: {value}"})
        return candidate

    def _terminal(self, terminal_id: str) -> TerminalHandle:
        terminal = self.terminals.get(terminal_id)
        if terminal is None:
            raise RequestError.invalid_params({"details": f"Unknown terminal: {terminal_id}"})
        return terminal

    @staticmethod
    def _kill_process(process: Process) -> None:
        if os.name == "nt":  # pragma: no cover - Windows process groups use the direct process handle
            process.kill()
        else:
            os.killpg(process.pid, signal.SIGKILL)

    @staticmethod
    def _exit_status(returncode: int | None) -> TerminalExitStatus | None:
        if returncode is None:
            return None
        if returncode >= 0:
            return TerminalExitStatus(exit_code=returncode)
        return TerminalExitStatus(signal=str(-returncode))

    @staticmethod
    def _environment() -> dict[str, str]:
        allowed = {"HOME", "PATH", "SHELL", "TMPDIR", "USER", "LANG", "LC_ALL", "TERM"}
        return {key: value for key, value in os.environ.items() if key in allowed or key.endswith("_API_KEY")}
