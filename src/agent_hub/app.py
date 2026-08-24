from __future__ import annotations

from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, cast

import logfire
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route
from typing_extensions import TypedDict

from agent_hub.config import HubConfig
from agent_hub.events import EventJournal
from agent_hub.manager import AgentManager
from agent_hub.persistence import Repository
from agent_hub.protocol import RPCError, decode_records, encode_record, failure, notification, success
from agent_hub.runtimes import AgentRuntime, CodePuppyRuntime, PiRuntime, PydanticAIRuntime
from agent_hub.security import redact_text


class LifespanState(TypedDict):
    manager: AgentManager


def create_app(
    config: HubConfig | None = None,
    runtimes: dict[str, AgentRuntime] | None = None,
) -> Starlette:
    hub_config = config or HubConfig()

    @asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncGenerator[LifespanState]:
        database_path = cast(Any, hub_config.database_path)
        repository = Repository(database_path)
        configured_runtimes: dict[str, AgentRuntime] = runtimes or {
            "codepuppy": CodePuppyRuntime(
                hub_config.codepuppy_executable,
                socket_path=hub_config.socket_path,
                process_shutdown_seconds=hub_config.process_shutdown_seconds,
                max_record_bytes=hub_config.max_record_bytes,
                max_output_bytes=hub_config.max_output_bytes,
            ),
            "pi": PiRuntime(
                shutdown_grace_seconds=hub_config.shutdown_grace_seconds,
                process_shutdown_seconds=hub_config.process_shutdown_seconds,
                socket_path=hub_config.socket_path,
                max_record_bytes=hub_config.max_record_bytes,
                max_stderr_bytes=hub_config.max_output_bytes,
            ),
            "pydantic-ai": PydanticAIRuntime(max_tool_output_bytes=hub_config.max_output_bytes),
        }
        await repository.open()
        await repository.recover_after_restart(
            lambda agent: (
                agent.runtime in configured_runtimes and configured_runtimes[agent.runtime].is_resumable(agent)
            )
        )
        journal = EventJournal(repository, hub_config.subscriber_queue_size)
        manager = AgentManager(hub_config, repository, journal, configured_runtimes)
        await manager.start()
        try:
            yield {"manager": manager}
        finally:
            await manager.shutdown()
            await repository.close()

    return Starlette(
        routes=[
            Route("/health", health, methods=["GET"]),
            Route("/v1/rpc", rpc, methods=["POST"]),
            Route("/v1/events", events, methods=["GET"]),
        ],
        lifespan=lifespan,
    )


async def health(_request: Request) -> Response:
    return JSONResponse({"status": "ok"})


async def rpc(request: Request[LifespanState]) -> Response:
    body = await request.body()
    responses: list[dict[str, Any]] = []
    try:
        records = decode_records(body, request.state["manager"].config.max_record_bytes)
    except RPCError as exc:
        return Response(encode_record(failure(None, exc)), media_type="application/x-ndjson")
    for record in records:
        try:
            result = await request.state["manager"].dispatch(record["method"], record["params"])
            responses.append(success(record["id"], result))
        except RPCError as exc:
            responses.append(failure(record["id"], exc))
        except (KeyError, OSError, RuntimeError, TypeError, ValueError) as exc:
            logfire.error("Command {method} failed: {error}", method=record["method"], error=redact_text(str(exc)))
            responses.append(failure(record["id"], RPCError(-32603, "Internal error")))
    return Response(b"".join(encode_record(item) for item in responses), media_type="application/x-ndjson")


async def events(request: Request[LifespanState]) -> Response:
    raw_after = request.query_params.get("after", "0")
    try:
        after = int(raw_after)
    except ValueError:
        return JSONResponse({"error": "after must be an integer"}, status_code=400)
    if after < 0:
        return JSONResponse({"error": "after must not be negative"}, status_code=400)

    async def records() -> AsyncIterator[bytes]:
        async for event in request.state["manager"].journal.stream(after):
            yield encode_record(notification(event.as_dict()))

    return StreamingResponse(records(), media_type="application/x-ndjson")
