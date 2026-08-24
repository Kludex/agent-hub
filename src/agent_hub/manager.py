from __future__ import annotations

import contextlib
import time
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import anyio
import logfire
from anyio.abc import TaskGroup

from agent_hub.agent_commands import abort, follow_up, prompt, spawn, steer
from agent_hub.config import AgentProfile, HubConfig, load_profiles
from agent_hub.events import EventJournal
from agent_hub.isolation import IsolationFailure, IsolationManager
from agent_hub.lifecycle_commands import apply, discard, park, patch, revive, stop
from agent_hub.models import TERMINAL_RUN_STATES, AgentRecord, RunRecord
from agent_hub.persistence import Repository
from agent_hub.protocol import RPCError
from agent_hub.query_commands import get_agent, get_run, list_agents, snapshot, wait_run
from agent_hub.registry import BackgroundTask, LiveAgent, RuntimeRegistry, start_background
from agent_hub.runtimes.base import AgentRuntime, RuntimeFailure, RuntimeResult, StartAgentRequest, StartRunRequest
from agent_hub.scheduler import Scheduler, ScheduleRequest
from agent_hub.security import redact_data, redact_text

Handler = Callable[["AgentManager", dict[str, Any]], Awaitable[dict[str, Any]]]


class AgentManager:
    def __init__(
        self,
        config: HubConfig,
        repository: Repository,
        journal: EventJournal,
        runtimes: dict[str, AgentRuntime],
    ) -> None:
        self.config = config
        self.repository = repository
        self.journal = journal
        self.runtimes = runtimes
        self.scheduler = Scheduler(config.global_concurrency)
        self.isolation = IsolationManager(config.data_dir)
        self.live = RuntimeRegistry()
        self._run_tasks: dict[str, BackgroundTask] = {}
        self._run_waiters: dict[str, anyio.Event] = {}
        self._aborted_runs: set[str] = set()
        self._output_bytes: dict[str, int] = {}
        self._run_prompts: dict[str, str] = {}
        self._queued_at: dict[str, float] = {}
        self._started_at: dict[str, float] = {}
        self._first_output: set[str] = set()
        self._tool_started: dict[tuple[str, str], float] = {}
        self._command_lock = anyio.Lock()
        self._task_group: TaskGroup | None = None
        self._opened_runtimes: list[AgentRuntime] = []

    async def start(self) -> None:
        if self._task_group is not None:  # pragma: no cover - lifespan starts each manager once
            raise RuntimeError("Agent manager is already started")
        for runtime in dict.fromkeys(self.runtimes.values()):
            await runtime.open()
            self._opened_runtimes.append(runtime)
        self._task_group = anyio.create_task_group()
        await self._task_group.__aenter__()

    async def dispatch(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        handlers: dict[str, Handler] = {
            "hub.snapshot": snapshot,
            "agent.spawn": spawn,
            "agent.list": list_agents,
            "agent.get": get_agent,
            "agent.prompt": prompt,
            "agent.steer": steer,
            "agent.follow_up": follow_up,
            "agent.abort": abort,
            "agent.stop": stop,
            "agent.park": park,
            "agent.revive": revive,
            "agent.patch": patch,
            "agent.apply": apply,
            "agent.discard": discard,
            "run.get": get_run,
            "run.wait": wait_run,
        }
        handler = handlers.get(method)
        if handler is None:
            raise RPCError(-32601, f"Method not found: {method}")
        logfire.info("Accepted command {method}", method=method)
        try:
            idempotency_key = params.get("idempotencyKey")
            if not isinstance(idempotency_key, str):
                return await handler(self, params)
            async with self._command_lock:
                try:
                    previous = await self.repository.get_idempotent(idempotency_key, method)
                except ValueError as exc:
                    raise RPCError(-32602, str(exc)) from exc
                if previous is not None:
                    return previous
                result = await handler(self, params)
                await self.repository.put_idempotent(idempotency_key, method, result)
                return result
        finally:
            logfire.info("Completed command {method}", method=method)

    async def shutdown(self) -> None:
        tasks = list(self._run_tasks.values())
        for task in tasks:
            task.cancel()
        for task in tasks:
            await task.wait()
        for _agent_id, live in self.live.values():
            await self._close_live(live)
        if self._task_group is not None:
            await self._task_group.__aexit__(None, None, None)
            self._task_group = None
        for runtime in reversed(self._opened_runtimes):
            await runtime.close()
        self._opened_runtimes.clear()

    def _start_run(
        self,
        run: RunRecord,
        profile: AgentProfile,
        model: str | None,
        isolated: bool,
        *,
        reuse: bool = False,
    ) -> None:
        task = self._background(self._execute, run, profile, model, isolated, reuse)
        self._run_tasks[run.id] = task
        self._run_waiters[run.id] = anyio.Event()
        self._output_bytes[run.id] = 0
        self._queued_at[run.id] = time.monotonic()

    async def _execute(
        self, run: RunRecord, profile: AgentProfile, model: str | None, isolated: bool, reuse: bool
    ) -> None:
        agent = await self.repository.get_agent(run.agent_id)
        if agent is None:  # pragma: no cover - runs have a foreign key to durable agents
            return
        schedule = ScheduleRequest(run.id, agent.cwd, agent.access, isolated)
        try:
            async with self.scheduler.slot(schedule):
                started = time.monotonic()
                self._started_at[run.id] = started
                queued_at = self._queued_at.get(run.id, started)
                logfire.info(
                    "Run {run_id} acquired a scheduler slot after {queue_wait_seconds} seconds",
                    run_id=run.id,
                    queue_wait_seconds=started - queued_at,
                )
                with anyio.fail_after(profile.max_runtime_seconds):
                    await self._run_with_slot(agent, run, profile, model, reuse)
        except anyio.get_cancelled_exc_class():
            with anyio.CancelScope(shield=True):
                await self._finish_cancelled(agent, run)
        except TimeoutError:
            await self._finish_failure(agent, run, "Run exceeded its maximum runtime")
        except (IsolationFailure, RuntimeFailure, OSError) as exc:
            if run.id in self._aborted_runs:
                live = self.live.get(agent.id)
                if live is None:
                    await self._finish_cancelled(agent, run)
                else:
                    await self._finish_aborted(agent, run, profile, live)
            else:
                await self._finish_failure(agent, run, str(exc))
        except Exception as exc:
            await self._finish_failure(agent, run, f"Unexpected runtime failure: {exc}")
        finally:
            with anyio.CancelScope(shield=True):
                self._aborted_runs.discard(run.id)
                self._output_bytes.pop(run.id, None)
                self._run_prompts.pop(run.id, None)
                self._queued_at.pop(run.id, None)
                self._started_at.pop(run.id, None)
                self._first_output.discard(run.id)
                for key in [key for key in self._tool_started if key[0] == run.id]:
                    self._tool_started.pop(key, None)
                self._run_tasks.pop(run.id, None)
                self._run_waiters.setdefault(run.id, anyio.Event()).set()

    async def _run_with_slot(
        self,
        agent: AgentRecord,
        run: RunRecord,
        profile: AgentProfile,
        model: str | None,
        reuse: bool,
    ) -> None:
        if agent.isolated and not reuse:
            worktree = await self.isolation.prepare(agent)
            agent = await self.repository.update_agent(
                agent.id,
                agent.state,
                {**agent.restoration, "isolation": worktree.as_restoration()},
            )
        await self._set_run_state(run.id, "running")
        if reuse:
            live = self.live.get(agent.id)
            if live is None:  # pragma: no cover - agent.prompt validates the live runtime before scheduling
                raise RuntimeFailure("Agent runtime is unavailable")
            if live.idle_task is not None:
                live.idle_task.cancel()
        else:
            runtime = self.runtimes[agent.runtime]
            startup_started = time.monotonic()
            handle = await runtime.start(self._start_request(agent, profile, model))
            logfire.info(
                "Started runtime for agent {agent_id} in {startup_seconds} seconds",
                agent_id=agent.id,
                startup_seconds=time.monotonic() - startup_started,
            )
            event_task = self._background(self._pump_events, agent.id, runtime, handle)
            live = LiveAgent(runtime, handle, event_task)
            self.live.add(agent.id, live)
        live.current_run_id = run.id
        await self._set_agent_state(agent.id, "running")
        prompt = self._run_prompts.get(run.id, run.prompt)
        result = await live.runtime.prompt(live.handle, StartRunRequest(run.id, prompt))
        if run.id in self._aborted_runs:
            await self._finish_aborted(agent, run, profile, live)
        else:
            await self._finish_success(agent, run, profile, live, result)

    async def _finish_success(
        self, agent: AgentRecord, run: RunRecord, profile: AgentProfile, live: LiveAgent, result: RuntimeResult
    ) -> None:
        restoration = {**agent.restoration, **result.restoration}
        if agent.isolated:
            restoration = await self.isolation.capture(agent, restoration)
        await self.repository.update_agent(agent.id, "running", restoration)
        await self._set_run_state(
            run.id,
            "succeeded",
            result=self._bounded_text(redact_text(result.text), self.config.max_output_bytes),
            usage=result.usage,
        )
        live.current_run_id = None
        if profile.keep_alive:
            await self._set_agent_state(agent.id, "idle")
            self._schedule_idle(agent.id, profile, live)
            return
        await self._set_agent_state(agent.id, "stopping")
        self.live.remove(agent.id)
        await self._close_live(live)
        await self._set_agent_state(agent.id, "stopped")

    async def _finish_aborted(
        self,
        agent: AgentRecord,
        run: RunRecord,
        profile: AgentProfile,
        live: LiveAgent,
    ) -> None:
        await self._set_run_state(run.id, "aborted")
        live.current_run_id = None
        if profile.keep_alive:
            await self._set_agent_state(agent.id, "idle")
            self._schedule_idle(agent.id, profile, live)
            return
        self.live.remove(agent.id)
        await self._close_live(live)
        await self._set_agent_state(agent.id, "stopped")

    async def _finish_cancelled(self, agent: AgentRecord, run: RunRecord) -> None:
        current = await self.repository.get_run(run.id)
        if current is not None and current.state not in TERMINAL_RUN_STATES:
            await self._set_run_state(run.id, "aborted")
        live = self.live.remove(agent.id)
        if live is not None:
            await self._close_live(live)
        await self._set_agent_state(agent.id, "stopped")

    async def _finish_failure(self, agent: AgentRecord, run: RunRecord, message: str) -> None:
        message = redact_text(message)
        current = await self.repository.get_run(run.id)
        if current is not None and current.state not in TERMINAL_RUN_STATES:
            await self._set_run_state(run.id, "failed", error=message)
            await self.journal.emit("run.error", agent_id=agent.id, run_id=run.id, data={"message": message})
        live = self.live.remove(agent.id)
        if live is not None:
            await self._close_live(live)
        stored = await self.repository.get_agent(agent.id)
        runtime = self.runtimes[agent.runtime]
        state = "parked" if stored is not None and runtime.is_resumable(stored) else "failed"
        await self._set_agent_state(agent.id, state)

    async def _pump_events(self, agent_id: str, runtime: AgentRuntime, handle: object) -> None:
        try:
            async for event in runtime.events(handle):
                live = self.live.get(agent_id)
                run_id = live.current_run_id if live is not None else None
                data = self._bounded_event_data(run_id, event.type, redact_data(event.data))
                if run_id is not None:
                    self._observe_runtime_event(run_id, event.type, event.data)
                if data is not None:
                    if event.type == "run.usage.updated" and run_id is not None:
                        await self.repository.update_run_usage(run_id, data)
                    await self.journal.emit(event.type, agent_id=agent_id, run_id=run_id, data=data)
        except (RuntimeFailure, OSError) as exc:
            await self.journal.emit("run.error", agent_id=agent_id, data={"message": str(exc)})
        except Exception as exc:
            await self.journal.emit(
                "run.error",
                agent_id=agent_id,
                data={"message": redact_text(f"Unexpected runtime event failure: {exc}")},
            )

    async def _close_live(self, live: LiveAgent) -> None:
        if live.idle_task is not None:
            live.idle_task.cancel()
        with contextlib.suppress(RuntimeFailure, OSError):
            await live.runtime.stop(live.handle)
        live.event_task.cancel()
        await live.event_task.wait()

    def _background(self, function: Callable[..., Awaitable[None]], *args: object) -> BackgroundTask:
        if self._task_group is None:  # pragma: no cover - API handlers run only inside lifespan
            raise RuntimeError("Agent manager is not started")
        return start_background(self._task_group, function, *args)

    def _schedule_idle(self, agent_id: str, profile: AgentProfile, live: LiveAgent) -> None:
        if profile.idle_timeout_seconds is None:  # pragma: no cover - keep-alive profiles require this value
            return
        live.idle_task = self._background(self._park_after_idle, agent_id, profile.idle_timeout_seconds)

    async def _park_after_idle(self, agent_id: str, delay: float) -> None:
        await anyio.sleep(delay)
        live = self.live.get(agent_id)
        if live is not None:
            live.idle_task = None
        with contextlib.suppress(RPCError):
            agent = await self._agent_param({"agentId": agent_id})
            if live is not None and live.runtime.is_resumable(agent):
                await park(self, {"agentId": agent_id})
            else:
                await stop(self, {"agentId": agent_id})

    async def _set_agent_state(self, agent_id: str, state: Any) -> None:
        await self.repository.update_agent(agent_id, state)
        await self.journal.emit("agent.state.changed", agent_id=agent_id, data={"state": state})
        if state in {"stopped", "failed"}:
            await self.repository.prune_completed_streaming_events(self.config.completed_event_retention)
        logfire.info("Agent {agent_id} changed state to {state}", agent_id=agent_id, state=state)

    async def _set_run_state(self, run_id: str, state: Any, **values: Any) -> None:
        run = await self.repository.update_run(run_id, state, **values)
        await self.journal.emit("run.state.changed", agent_id=run.agent_id, run_id=run_id, data={"state": state})
        logfire.info("Run {run_id} changed state to {state}", run_id=run_id, state=state)

    async def _agent_param(self, params: dict[str, Any]) -> AgentRecord:
        agent_id = params.get("agentId")
        if not isinstance(agent_id, str):
            raise RPCError(-32602, "agentId is required")
        agent = await self.repository.get_agent(agent_id)
        if agent is None:
            raise RPCError(-32008, "Agent not found", {"agentId": agent_id})
        return agent

    async def _run_param(self, params: dict[str, Any]) -> RunRecord:
        run_id = params.get("runId")
        if not isinstance(run_id, str):
            raise RPCError(-32602, "runId is required")
        run = await self.repository.get_run(run_id)
        if run is None:
            raise RPCError(-32009, "Run not found", {"runId": run_id})
        return run

    async def _isolated_agent(self, params: dict[str, Any]) -> AgentRecord:
        agent = await self._agent_param(params)
        if not agent.isolated:
            raise RPCError(-32010, "Agent does not use workspace isolation")
        return agent

    async def _running_live(self, params: dict[str, Any]) -> LiveAgent:
        agent = await self._agent_param(params)
        live = self.live.get(agent.id)
        if agent.state != "running" or live is None:
            raise RPCError(-32004, "Agent is not running")
        return live

    async def _lineage(self, params: dict[str, Any]) -> tuple[str | None, str | None, int]:
        parent_id = params.get("parentAgentId")
        root_session_id = params.get("rootSessionId")
        if parent_id is None:
            return None, root_session_id if isinstance(root_session_id, str) else None, 0
        if not isinstance(parent_id, str):
            raise RPCError(-32602, "parentAgentId must be a string")
        parent = await self.repository.get_agent(parent_id)
        if parent is None:
            raise RPCError(-32008, "Parent agent not found")
        if not self._agent_profile(parent).allow_delegation:
            raise RPCError(-32012, "Parent agent profile does not allow nested delegation")
        return parent.id, parent.root_session_id, parent.depth + 1

    def _agent_profile(self, agent: AgentRecord) -> AgentProfile:
        profile = load_profiles(self.config, Path(agent.cwd)).get(agent.profile)
        if profile is None:
            raise RPCError(-32001, "Agent profile not found", {"profile": agent.profile})
        return profile

    def _start_request(self, agent: AgentRecord, profile: AgentProfile, model: str | None) -> StartAgentRequest:
        return StartAgentRequest(
            agent.id,
            profile,
            self.isolation.runtime_cwd(agent),
            self.config.data_dir / "sessions" / agent.id,
            model,
            agent.access,
        )

    @staticmethod
    def _model_override(profile: AgentProfile, value: Any) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str) or not value:
            raise RPCError(-32602, "model must be a non-empty string")
        if not profile.allow_model_override:
            raise RPCError(-32602, "Agent profile does not allow model overrides")
        return value

    @staticmethod
    def _profile_with_timeout(profile: AgentProfile, value: Any) -> AgentProfile:
        if value is None:
            return profile
        if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
            raise RPCError(-32602, "maxRuntimeSeconds must be positive")
        return profile.model_copy(update={"max_runtime_seconds": min(float(value), profile.max_runtime_seconds)})

    @staticmethod
    def _working_directory(value: Any) -> Path:
        path = Path(value) if isinstance(value, str) else Path.cwd()
        try:
            path = path.expanduser().resolve(strict=True)
        except OSError as exc:
            raise RPCError(-32602, f"Invalid working directory: {path}") from exc
        if not path.is_dir():
            raise RPCError(-32602, f"Working directory is not a directory: {path}")
        return path

    def _observe_runtime_event(self, run_id: str, event_type: str, data: dict[str, Any]) -> None:
        now = time.monotonic()
        if event_type in {"run.output.delta", "run.thinking.delta"} and run_id not in self._first_output:
            self._first_output.add(run_id)
            started = self._started_at.get(run_id, now)
            logfire.info(
                "Run {run_id} produced its first model output after {first_output_seconds} seconds",
                run_id=run_id,
                first_output_seconds=now - started,
            )
        tool_id = str(data.get("toolCallId", data.get("toolName", "tool")))
        if event_type == "run.tool.started":
            self._tool_started[(run_id, tool_id)] = now
        elif event_type == "run.tool.finished":
            key = (run_id, tool_id)
            tool_started_at = self._tool_started.pop(key) if key in self._tool_started else None
            if tool_started_at is not None:
                logfire.info(
                    "Tool {tool_name} finished for run {run_id} after {tool_seconds} seconds",
                    tool_name=data.get("toolName", "tool"),
                    run_id=run_id,
                    tool_seconds=now - tool_started_at,
                )

    def _bounded_event_data(
        self,
        run_id: str | None,
        event_type: str,
        data: dict[str, Any],
    ) -> dict[str, Any] | None:
        if run_id is None or event_type not in {"run.output.delta", "run.thinking.delta"}:
            return data
        text = data.get("text")
        if not isinstance(text, str):
            return data
        used = self._output_bytes.get(run_id, 0)
        remaining = self.config.max_output_bytes - used
        if remaining <= 0:
            return None
        bounded = self._bounded_text(text, remaining)
        self._output_bytes[run_id] = used + len(bounded.encode("utf-8"))
        return {**data, "text": bounded, "truncated": bounded != text}

    @staticmethod
    def _bounded_text(value: str, limit: int) -> str:
        return value.encode("utf-8")[:limit].decode("utf-8", errors="ignore")

    @staticmethod
    def _message(params: dict[str, Any]) -> str:
        message = params.get("message")
        if not isinstance(message, str) or not message:
            raise RPCError(-32602, "message must be a non-empty string")
        return message

    @staticmethod
    def _id(prefix: str) -> str:
        return f"{prefix}_{uuid.uuid4().hex}"
