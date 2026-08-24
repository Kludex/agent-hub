from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, cast

import anyio
import pytest
from pydantic_ai import RunContext
from pydantic_ai.messages import ModelMessage
from pydantic_ai.models import ModelRequestParameters, StreamedResponse
from pydantic_ai.models.test import TestModel
from pydantic_ai.settings import ModelSettings

from agent_hub.config import AgentProfile
from agent_hub.models import AgentRecord
from agent_hub.runtimes.base import RuntimeFailure, StartAgentRequest, StartRunRequest
from agent_hub.runtimes.pi import PiRuntime
from agent_hub.runtimes.pydantic_ai import PydanticAIRuntime


class SlowTestModel(TestModel):
    @asynccontextmanager
    async def request_stream(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
        run_context: RunContext[Any] | None = None,
    ) -> AsyncIterator[StreamedResponse]:
        await anyio.sleep_forever()  # pragma: no cover - cancellation may arrive before model startup
        yield cast(StreamedResponse, None)  # pragma: no cover - AnyIO never returns from sleep_forever()


def request(tmp_path: Path, profile: AgentProfile) -> StartAgentRequest:
    return StartAgentRequest("agt_test", profile, tmp_path, tmp_path / "sessions")


@pytest.mark.anyio
async def test_pi_runtime_reports_startup_and_lifecycle_misuse(tmp_path: Path) -> None:
    missing = PiRuntime(str(tmp_path / "missing"))
    await missing.open()
    with pytest.raises(RuntimeFailure, match="Could not start Pi"):
        await missing.start(request(tmp_path, AgentProfile(name="pi")))
    with pytest.raises(RuntimeError, match="already open"):
        await missing.open()
    await missing.close()

    executable = Path(__file__).parent / "fixtures" / "fake_pi.py"
    executable.chmod(0o700)
    closed = PiRuntime(str(executable))
    with pytest.raises(RuntimeError, match="not open"):
        await closed.start(request(tmp_path, AgentProfile(name="pi")))

    runtime = PiRuntime(str(executable), shutdown_grace_seconds=0.01, process_shutdown_seconds=0.05)
    await runtime.open()
    start_request = request(tmp_path, AgentProfile(name="pi"))
    for session_file, message in ((None, "no session file"), ("/tmp/cancel.jsonl", "cancelled")):
        agent = AgentRecord(
            id="agt_test",
            runtime="pi",
            profile="pi",
            cwd=str(tmp_path),
            access="shared-write",
            state="parked",
            keep_alive=True,
            restoration={} if session_file is None else {"sessionFile": session_file},
        )
        with pytest.raises(RuntimeFailure, match=message):
            await runtime.restore(agent, start_request)
    handle = await runtime.start(start_request)
    await runtime.stop(handle)
    await runtime.stop(handle)
    with pytest.raises(RuntimeFailure, match="not running"):
        await runtime.steer(handle, "late")
    with pytest.raises(TypeError, match="handle"):
        await runtime.stop(object())
    await runtime.close()


@pytest.mark.anyio
async def test_pydantic_runtime_can_steer_follow_and_abort_an_active_run(tmp_path: Path) -> None:
    runtime = PydanticAIRuntime(models={"slow": SlowTestModel()})
    await runtime.open()
    handle = await runtime.start(request(tmp_path, AgentProfile(name="slow", runtime="pydantic-ai", model="slow")))
    send, receive = anyio.create_memory_object_stream[str](1)

    async def prompt() -> None:
        try:
            await runtime.prompt(handle, StartRunRequest("run_slow", "wait"))
        except RuntimeFailure as exc:
            await send.send(str(exc))

    async with anyio.create_task_group() as task_group:
        task_group.start_soon(prompt)
        await anyio.sleep(0.01)
        await runtime.steer(handle, "steer")
        await runtime.follow_up(handle, "follow")
        await runtime.abort(handle)
        error = await receive.receive()

    assert error == "Pydantic AI run was aborted"
    await runtime.stop(handle)
    await runtime.close()
    await send.aclose()
    await receive.aclose()


@pytest.mark.anyio
async def test_pydantic_runtime_validates_profiles_and_non_running_actions(tmp_path: Path) -> None:
    runtime = PydanticAIRuntime(models={"test": TestModel(custom_output_text="done")})
    await runtime.open()
    with pytest.raises(RuntimeFailure, match="must define a model"):
        await runtime.start(request(tmp_path, AgentProfile(name="missing-model", runtime="pydantic-ai")))
    with pytest.raises(RuntimeFailure, match="Unknown model"):
        await PydanticAIRuntime().start(
            request(tmp_path, AgentProfile(name="invalid-model", runtime="pydantic-ai", model="invalid"))
        )
    with pytest.raises(RuntimeFailure, match="Unknown Pydantic AI tools"):
        await runtime.start(
            request(
                tmp_path,
                AgentProfile(name="missing-tool", runtime="pydantic-ai", model="test", tools=("missing",)),
            )
        )
    with pytest.raises(RuntimeFailure, match="MCP support"):
        await runtime.start(
            request(
                tmp_path,
                AgentProfile(name="missing-mcp", runtime="pydantic-ai", model="test", mcp_servers=("server.py",)),
            )
        )
    with pytest.raises(RuntimeFailure, match="Could not load Pydantic AI AgentSpec"):
        await runtime.start(
            request(
                tmp_path,
                AgentProfile(
                    name="missing-spec",
                    runtime="pydantic-ai",
                    model="test",
                    agent_spec=tmp_path / "missing.yaml",
                ),
            )
        )
    capability_spec = tmp_path / "capability.yaml"
    capability_spec.write_text("model: test\ncapabilities: [Thinking]\n", encoding="utf-8")
    with pytest.raises(RuntimeFailure, match="capabilities are not supported"):
        await runtime.start(
            request(
                tmp_path,
                AgentProfile(name="capability", runtime="pydantic-ai", agent_spec=capability_spec),
            )
        )

    start_request = request(tmp_path, AgentProfile(name="plain", runtime="pydantic-ai", model="test"))
    handle = await runtime.start(start_request)
    with pytest.raises(RuntimeFailure, match="not running"):
        await runtime.steer(handle, "steer")
    with pytest.raises(RuntimeFailure, match="not running"):
        await runtime.follow_up(handle, "follow")
    await runtime.abort(handle)
    with pytest.raises(RuntimeFailure, match="restoration"):
        await runtime.restore(
            AgentRecord(
                id="agt_test",
                runtime="pydantic-ai",
                profile="plain",
                cwd=str(tmp_path),
                access="shared-write",
                state="parked",
                keep_alive=True,
            ),
            start_request,
        )
    await runtime.stop(handle)
    with pytest.raises(TypeError, match="handle"):
        await runtime.stop(object())
    await runtime.close()
