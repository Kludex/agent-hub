from __future__ import annotations

from collections.abc import AsyncIterable, AsyncIterator, Callable
from dataclasses import asdict, dataclass, field
from typing import Any

import anyio
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream
from pydantic_ai import Agent
from pydantic_ai.exceptions import AgentRunError, UserError
from pydantic_ai.messages import (
    AgentStreamEvent,
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    PartDeltaEvent,
    PartStartEvent,
    TextPart,
    TextPartDelta,
    ThinkingPart,
    ThinkingPartDelta,
)
from pydantic_ai.models import Model
from pydantic_ai.toolsets import AbstractToolset
from pydantic_ai.usage import RunUsage, UsageLimits

from agent_hub.models import AgentRecord
from agent_hub.runtimes.base import RuntimeEvent, RuntimeFailure, RuntimeResult, StartAgentRequest, StartRunRequest
from agent_hub.workspace_tools import create_workspace_tools

ToolFunction = Callable[..., Any]


@dataclass
class PydanticAIHandle:
    agent: Agent[Any, str]
    event_send: MemoryObjectSendStream[RuntimeEvent]
    event_receive: MemoryObjectReceiveStream[RuntimeEvent]
    messages: list[Any] = field(default_factory=list)
    usage_limits: UsageLimits = field(default_factory=UsageLimits)
    current_scope: anyio.CancelScope | None = None
    steering: list[str] = field(default_factory=list)
    follow_ups: list[str] = field(default_factory=list)


class PydanticAIRuntime:
    def __init__(
        self,
        tools: dict[str, ToolFunction] | None = None,
        models: dict[str, Model] | None = None,
        mcp_servers: dict[str, AbstractToolset[Any]] | None = None,
        read_only_tools: set[str] | None = None,
        read_only_mcp_servers: set[str] | None = None,
        max_tool_output_bytes: int = 50 * 1024,
    ) -> None:
        self._tools = tools or {}
        self._models = models or {}
        self._mcp_servers = mcp_servers or {}
        self._read_only_tools = read_only_tools or set()
        self._read_only_mcp_servers = read_only_mcp_servers or set()
        self._max_tool_output_bytes = max_tool_output_bytes

    async def open(self) -> None:
        pass

    async def close(self) -> None:
        pass

    async def start(self, request: StartAgentRequest) -> object:
        model = request.model or request.profile.model
        if model is None:
            raise RuntimeFailure(f"Pydantic AI profile {request.profile.name!r} must define a model")
        workspace_tools, workspace_read_only = create_workspace_tools(request.cwd, self._max_tool_output_bytes)
        available_tools = {**self._tools, **workspace_tools}
        missing = set(request.profile.tools) - available_tools.keys()
        if missing:
            raise RuntimeFailure(f"Unknown Pydantic AI tools: {', '.join(sorted(missing))}")
        if request.access == "read-only":
            writable_tools = set(request.profile.tools) - (self._read_only_tools | workspace_read_only)
            writable_servers = set(request.profile.mcp_servers) - self._read_only_mcp_servers
            if writable_tools or writable_servers:
                names = sorted(writable_tools | writable_servers)
                raise RuntimeFailure(f"Read-only Pydantic AI profile requested write-capable tools: {', '.join(names)}")
        tools = [available_tools[name] for name in request.profile.tools]
        toolsets: list[AbstractToolset[Any]] = []
        for server in request.profile.mcp_servers:
            registered = self._mcp_servers.get(server)
            toolsets.append(registered if registered is not None else self._mcp_toolset(server))
        selected_model: Model | str = self._models.get(model, model)
        try:
            agent: Agent[Any, str] = Agent(
                selected_model,
                instructions=request.profile.instructions,
                tools=tools,
                toolsets=toolsets,
            )
        except UserError as exc:
            raise RuntimeFailure(str(exc)) from exc
        usage_limits = UsageLimits(**request.profile.usage_limits.model_dump())
        event_send, event_receive = anyio.create_memory_object_stream[RuntimeEvent](256)
        return PydanticAIHandle(agent, event_send, event_receive, usage_limits=usage_limits)

    async def prompt(self, handle: object, request: StartRunRequest) -> RuntimeResult:
        runtime = self._handle(handle)
        prompts = [request.prompt]
        outputs: list[str] = []
        total_usage = RunUsage()
        while prompts:
            prompt = prompts.pop(0)
            scope = anyio.CancelScope()
            runtime.current_scope = scope
            try:
                with scope:
                    result = await runtime.agent.run(
                        prompt,
                        message_history=runtime.messages,
                        event_stream_handler=lambda _context, events: self._stream(runtime, events),
                        usage_limits=runtime.usage_limits,
                    )
                if scope.cancel_called:
                    raise RuntimeFailure("Pydantic AI run was aborted")
            except (AgentRunError, UserError) as exc:
                raise RuntimeFailure(str(exc)) from exc
            finally:
                runtime.current_scope = None
            outputs.append(str(result.output))
            runtime.messages = list(result.all_messages())
            total_usage.incr(result.usage)
            prompts.extend(runtime.steering)
            prompts.extend(runtime.follow_ups)
            runtime.steering.clear()
            runtime.follow_ups.clear()
        usage = self._usage(total_usage)
        await runtime.event_send.send(RuntimeEvent("run.usage.updated", usage))
        return RuntimeResult(text="\n\n".join(outputs), usage=usage)

    async def events(self, handle: object) -> AsyncIterator[RuntimeEvent]:
        runtime = self._handle(handle)
        async with runtime.event_receive:
            async for event in runtime.event_receive:
                yield event

    async def steer(self, handle: object, message: str) -> None:
        runtime = self._handle(handle)
        if runtime.current_scope is None:
            raise RuntimeFailure("Pydantic AI agent is not running")
        runtime.steering.append(message)

    async def follow_up(self, handle: object, message: str) -> None:
        runtime = self._handle(handle)
        if runtime.current_scope is None:
            raise RuntimeFailure("Pydantic AI agent is not running")
        runtime.follow_ups.append(message)

    async def abort(self, handle: object) -> None:
        scope = self._handle(handle).current_scope
        if scope is not None:
            scope.cancel()

    async def stop(self, handle: object) -> None:
        runtime = self._handle(handle)
        await self.abort(runtime)
        await runtime.event_send.aclose()
        await runtime.event_receive.aclose()

    def is_resumable(self, agent: AgentRecord) -> bool:
        return False

    async def restore(self, agent: AgentRecord, request: StartAgentRequest) -> object:
        raise RuntimeFailure("Pydantic AI conversation restoration is not configured")

    async def _stream(self, handle: PydanticAIHandle, events: AsyncIterable[AgentStreamEvent]) -> None:
        async for event in events:
            normalized = self._normalize(event)
            if normalized is not None:
                await handle.event_send.send(normalized)

    @staticmethod
    def _normalize(event: AgentStreamEvent) -> RuntimeEvent | None:
        if isinstance(event, PartStartEvent):
            if isinstance(event.part, TextPart) and event.part.content:  # pragma: no cover - provider stream shape
                return RuntimeEvent("run.output.delta", {"text": event.part.content})
            if isinstance(event.part, ThinkingPart) and event.part.content:  # pragma: no cover - provider stream shape
                return RuntimeEvent("run.thinking.delta", {"text": event.part.content})
        if isinstance(event, PartDeltaEvent):
            if isinstance(event.delta, TextPartDelta):
                return RuntimeEvent("run.output.delta", {"text": event.delta.content_delta})
            if (  # pragma: no cover - provider stream shape
                isinstance(event.delta, ThinkingPartDelta) and event.delta.content_delta
            ):
                return RuntimeEvent("run.thinking.delta", {"text": event.delta.content_delta})
        if isinstance(event, FunctionToolCallEvent):
            return RuntimeEvent(
                "run.tool.started",
                {
                    "toolCallId": event.part.tool_call_id,
                    "toolName": event.part.tool_name,
                    "args": event.part.args_as_dict(),
                },
            )
        if isinstance(event, FunctionToolResultEvent):
            return RuntimeEvent(
                "run.tool.finished",
                {
                    "toolCallId": event.part.tool_call_id,
                    "toolName": event.part.tool_name,
                    "result": str(event.part.content),
                },
            )
        return None

    @staticmethod
    def _mcp_toolset(server: str) -> AbstractToolset[Any]:
        try:
            from pydantic_ai.mcp import MCPToolset
        except ImportError as exc:
            raise RuntimeFailure(
                "Pydantic AI MCP support requires the pydantic-ai-slim[mcp] optional dependency"
            ) from exc
        return MCPToolset(server)  # pragma: no cover - exercised when the optional MCP dependency is installed

    @staticmethod
    def _usage(usage: RunUsage) -> dict[str, Any]:
        values = asdict(usage)
        values["cost"] = float(usage.cost) if usage.cost is not None else 0.0
        values["total_tokens"] = usage.total_tokens
        return values

    @staticmethod
    def _handle(handle: object) -> PydanticAIHandle:
        if not isinstance(handle, PydanticAIHandle):
            raise TypeError("Expected a Pydantic AI runtime handle")
        return handle
