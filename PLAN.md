# Agent Hub Plan

## Summary

Agent Hub is a local service that creates, monitors, steers, and stops coding agents. It provides one control plane for Pi subprocesses and Pydantic AI agents.

The service runs under Uvicorn on a Unix domain socket. Clients send JSON-RPC 2.0 commands framed as JSON Lines. They receive live events from a separate streaming JSONL endpoint.

A Pi extension provides the initial client. It registers a `task` tool for delegation and a `hub` interface for viewing and controlling agents.

## Goals

- Run multiple agents without tying their lifetime to one Pi session.
- Give every agent a stable ID, lifecycle state, parent relationship, and event history.
- Support blocking delegation and background delegation.
- Stream model output, tool activity, usage, status changes, and errors.
- Let a user steer, follow up, abort, park, and revive an agent.
- Preserve Pi sessions so parked or interrupted Pi agents can resume.
- Support Pi and Pydantic AI through separate runtime adapters.
- Keep the Pi extension thin. The service owns lifecycle and concurrency.
- Remain local and single-user by default.

## Non-goals

The first release will not provide:

- A hosted multi-user service.
- Distributed workers.
- A public network API.
- Automatic merging of concurrent edits.
- A browser application.
- Nested delegation without a configured recursion limit.
- Compatibility with every agent framework.

## Core decisions

### Runtime-neutral manager

The manager must not encode Pi-specific or Pydantic AI-specific lifecycle behavior in its core. Each backend implements an `AgentRuntime` protocol and emits normalized events.

The first two adapters are:

- `PiRuntime`: starts `pi --mode rpc` subprocesses and communicates through Pi's native JSONL RPC protocol.
- `PydanticAIRuntime`: runs configured Pydantic AI agents inside the service process.

Pi support comes first because it provides native Pi tools, extensions, session files, steering, and usage accounting. Pydantic AI support follows the same manager contract but receives only the tools explicitly registered by that runtime.

### Uvicorn over a Unix domain socket

The service listens on:

```text
~/.agent-hub/run/agent-hub.sock
```

The parent directory has mode `0700`. The socket is accessible only to its owner. The default Uvicorn deployment uses one worker so one process owns the live runtime handles and concurrency queues.

Uvicorn provides HTTP over the Unix socket. It uses `zttp` for HTTP parsing, and the daemon runs on `zuvloop`. Python clients use `httpx2`. It does not expose a custom raw socket protocol.

### JSONL framing

Requests, responses, and events use UTF-8 JSON Lines with LF as the only record delimiter.

```text
Content-Type: application/x-ndjson
```

Clients must buffer arbitrary byte chunks and split only on `\n`. HTTP chunks do not correspond to JSONL records.

### Separate command and event channels

Commands use short HTTP requests:

```text
POST /v1/rpc
```

Events use one long-lived streaming request:

```text
GET /v1/events?after=42
```

This avoids pretending that HTTP is a raw full-duplex stream. Commands flow to the service through `POST`; events flow back through the streaming `GET`.

A browser client can be added later through a token-protected loopback gateway. Browsers cannot connect directly to a Unix socket.

### MCP stdio bridge

`agent-hub mcp` exposes Agent Hub tools through MCP over stdio. Claude Code and Codex launch this thin client process, which forwards commands to the persistent daemon through the same private Unix socket. The bridge exposes delegation, agent inspection and cancellation, and run inspection and waiting without making the daemon network-accessible.

## User experience

### Blocking task

The parent Pi agent calls `task` and waits for the delegated run:

```json
{
  "agent": "reviewer",
  "prompt": "Review the authentication changes.",
  "background": false
}
```

The extension creates the agent, reports live progress as tool updates, and returns the final text and usage when the run settles.

### Background task

The parent starts an agent without waiting:

```json
{
  "agent": "scout",
  "prompt": "Find every caller of the deprecated API.",
  "background": true
}
```

The tool immediately returns an agent ID and run ID. The parent or user can inspect the run later. Background execution controls whether the caller waits; it does not make the agent persistent.

### Hub controls

The Hub supports these actions:

- List agents and current state.
- Open an agent transcript.
- Follow live output and tool activity.
- Send a steering message to an active run.
- Queue a follow-up turn.
- Abort the current run.
- Stop the agent process.
- Park an idle agent.
- Revive a parked agent.
- Copy or return the final result to the parent session.

## Architecture

```text
+---------------------------+
| Pi TUI                    |
|                           |
| task tool + hub extension |
+-------------+-------------+
              |
              | HTTP + JSONL over UDS
              v
+-------------+-------------+
| Agent Hub service         |
|                           |
| API                       |
| Agent registry            |
| Run registry              |
| Scheduler                 |
| Event journal             |
| Runtime adapters          |
+--------+------------------+
         |
         +-----------------------------+
         |                             |
         v                             v
+--------+---------+         +---------+------------+
| PiRuntime        |         | PydanticAIRuntime    |
|                  |         |                      |
| pi --mode rpc    |         | Pydantic AI Agent   |
| stdin/stdout     |         | explicit tools/MCP  |
| Pi JSONL RPC     |         | normalized events   |
+------------------+         +----------------------+
```

## Repository layout

```text
agent-hub/
├── pyproject.toml
├── README.md
├── PLAN.md
├── src/
│   └── agent_hub/
│       ├── __init__.py
│       ├── app.py
│       ├── cli.py
│       ├── client.py
│       ├── mcp_bridge.py
│       ├── service.py
│       ├── config.py
│       ├── protocol.py
│       ├── registry.py
│       ├── scheduler.py
│       ├── events.py
│       ├── persistence.py
│       ├── agent_commands.py
│       ├── lifecycle_commands.py
│       ├── query_commands.py
│       ├── isolation.py
│       ├── security.py
│       ├── workspace_tools.py
│       ├── assets/
│       │   └── agent-hub.js
│       └── runtimes/
│           ├── __init__.py
│           ├── base.py
│           ├── pi.py
│           └── pydantic_ai.py
├── extension/
│   ├── package.json
│   ├── tsconfig.json
│   └── src/
│       ├── index.ts
│       ├── client.ts
│       ├── jsonl.ts
│       ├── task-tool.ts
│       └── hub.ts
└── tests/
    ├── fixtures/
    ├── test_api.py
    ├── test_lifecycle.py
    ├── test_protocol.py
    ├── test_scheduler.py
    └── test_pi_runtime.py
```

The modules should remain small. Runtime-specific code must stay under `runtimes/`.

## Domain model

### Agent

An agent is a durable identity that may execute multiple runs.

```json
{
  "id": "agt_01J...",
  "runtime": "pi",
  "profile": "reviewer",
  "parentAgentId": "agt_01H...",
  "rootSessionId": "pi-session-id",
  "cwd": "/path/to/project",
  "state": "idle",
  "createdAt": "2026-08-24T12:00:00Z",
  "updatedAt": "2026-08-24T12:01:00Z"
}
```

Agent states are:

- `starting`: Runtime resources are being created.
- `idle`: The runtime is live and accepts another prompt.
- `running`: A run is active.
- `parked`: Runtime resources are released, but resumable state remains.
- `stopping`: Shutdown is in progress.
- `stopped`: Runtime resources and resumability are intentionally closed.
- `failed`: Startup or unrecoverable runtime failure occurred.

### Run

A run is one prompt and its resulting agent loop.

```json
{
  "id": "run_01J...",
  "agentId": "agt_01J...",
  "state": "running",
  "prompt": "Review the authentication changes.",
  "startedAt": "2026-08-24T12:00:01Z",
  "settledAt": null
}
```

Run states are:

- `queued`
- `running`
- `succeeded`
- `failed`
- `aborted`

Agent state and run state remain separate. A successful run normally leaves a keep-alive agent in `idle`.

### Event

Every normalized event has a manager-wide sequence number.

```json
{
  "sequence": 42,
  "timestamp": "2026-08-24T12:00:02Z",
  "type": "run.output.delta",
  "agentId": "agt_01J...",
  "runId": "run_01J...",
  "data": {
    "text": "The authentication flow"
  }
}
```

Sequence numbers let clients reconnect without losing state. A client that falls behind the retained event window must request a fresh snapshot.

## Runtime protocol

The Python runtime interface should use structural typing:

```python
from __future__ import annotations

from collections.abc import AsyncIterator
from typing_extensions import Protocol


class AgentRuntime(Protocol):
    async def start(self, request: StartAgentRequest) -> RuntimeHandle: ...

    async def prompt(self, handle: RuntimeHandle, request: StartRunRequest) -> str: ...

    async def events(self, handle: RuntimeHandle) -> AsyncIterator[RuntimeEvent]: ...

    async def steer(self, handle: RuntimeHandle, message: str) -> None: ...

    async def follow_up(self, handle: RuntimeHandle, message: str) -> None: ...

    async def abort(self, handle: RuntimeHandle) -> None: ...

    async def stop(self, handle: RuntimeHandle) -> None: ...

    async def restore(self, agent: StoredAgent) -> RuntimeHandle: ...
```

The exact types will be refined during implementation. The manager depends only on this protocol.

## Manager protocol

### Command envelope

Commands use JSON-RPC 2.0 objects, one per line:

```json
{"jsonrpc":"2.0","id":"cmd_01J...","method":"agent.spawn","params":{"runtime":"pi","profile":"reviewer","prompt":"Review authentication.","cwd":"/repo","background":false}}
```

### Success response

```json
{"jsonrpc":"2.0","id":"cmd_01J...","result":{"agentId":"agt_01J...","runId":"run_01J..."}}
```

### Error response

```json
{"jsonrpc":"2.0","id":"cmd_01J...","error":{"code":-32001,"message":"Agent profile not found","data":{"profile":"reviewer"}}}
```

### Event notification

The streaming endpoint emits JSON-RPC notifications:

```json
{"jsonrpc":"2.0","method":"agent.event","params":{"sequence":42,"type":"run.state.changed","agentId":"agt_01J...","runId":"run_01J...","data":{"state":"succeeded"}}}
```

### Initial methods

- `hub.snapshot`: Return agents, active runs, and the latest event sequence.
- `agent.spawn`: Create an agent and its initial run.
- `agent.list`: List agents with optional state and parent filters.
- `agent.get`: Return one agent and its runs.
- `agent.prompt`: Start another run on an idle agent.
- `agent.steer`: Steer the current run.
- `agent.follow_up`: Queue a prompt after the current run.
- `agent.abort`: Abort the current run without discarding the agent.
- `agent.stop`: Stop the runtime and mark the agent stopped.
- `agent.park`: Release runtime resources while retaining resumable state.
- `agent.revive`: Restore a parked agent.
- `run.get`: Return run state and result.
- `run.wait`: Wait until a run reaches a terminal state.

Every mutating command accepts an idempotency key. Repeating a command after a connection failure must not create a second agent or run.

## Event types

The normalized event vocabulary starts with:

- `agent.created`
- `agent.state.changed`
- `agent.removed`
- `run.created`
- `run.state.changed`
- `run.output.delta`
- `run.thinking.delta`
- `run.tool.started`
- `run.tool.updated`
- `run.tool.finished`
- `run.usage.updated`
- `run.error`
- `runtime.stderr`

Runtime-specific payloads may be included under `data.runtime`, but clients must be able to render the common fields without understanding the backend.

## Pi runtime

### Process startup

The runtime starts Pi without a shell:

```text
pi --mode rpc --session-dir <agent-session-directory>
```

The manager owns stdin, stdout, stderr, exit status, and cancellation. Stdout is reserved for Pi RPC records. Stderr is captured as logs and normalized `runtime.stderr` events.

The manager must never parse stdout with a Unicode-aware generic line reader. It buffers bytes and splits on LF to match Pi's strict JSONL framing.

### Prompting and completion

The adapter sends Pi RPC commands with IDs:

```json
{"id":"pi_01J...","type":"prompt","message":"Review authentication."}
```

It maps Pi events to normalized Hub events. It treats `agent_settled`, not `agent_end`, as run completion because retries, compaction, or queued messages may follow `agent_end`.

After settlement, the adapter obtains:

- Final assistant text through `get_last_assistant_text`.
- Token and cost data through `get_session_stats`.
- Session identity and file through `get_state`.

### Steering and follow-up

Hub actions map directly to Pi RPC:

- `agent.steer` sends `steer`.
- `agent.follow_up` sends `follow_up`.
- `agent.abort` sends `abort`.

### Parking and revival

Parking an idle Pi agent records its `sessionFile`, gracefully closes the process, and retains the agent record. Revival starts a new RPC process and switches it to the recorded session through `switch_session`.

A failed graceful shutdown escalates in this order:

1. Pi `abort` for an active run.
2. Wait for the configured grace period.
3. Send `SIGTERM`.
4. Wait for the process shutdown timeout.
5. Send `SIGKILL`.

## Pydantic AI runtime

Pydantic AI profiles are configured by name. Each profile defines:

- Instructions.
- Model selector.
- Tool set.
- MCP servers, when needed.
- Usage limits.
- Maximum runtime.
- Whether nested delegation is allowed.

The runtime emits the same normalized lifecycle, output, tool, and usage events as `PiRuntime`.

Pydantic AI agents do not inherit Pi's tools. Coding capabilities must come from explicit manager-owned tools or configured MCP servers. Tool permissions and workspace restrictions must be enforced by the manager rather than by prompt text.

The first Pydantic AI implementation may retain conversations only while the daemon is running. Durable restoration should be added only after its storage contract is explicit and tested.

## Agent profiles

Profiles are loaded from user and project configuration:

```text
~/.config/agent-hub/agents/
<project>/.agent-hub/agents/
```

Project profiles override user profiles with the same name. A profile includes a runtime so the caller can use a stable name without knowing its implementation.

```toml
name = "reviewer"
runtime = "pi"
model = "anthropic/claude-sonnet-4"
access = "read-only"
keep_alive = false
max_runtime_seconds = 900
```

Profiles are reusable definitions, not running agent instances. Each `agent.spawn` creates a fresh instance from a profile. By default, the manager stops the runtime after its initial run succeeds, fails, is aborted, or reaches its deadline. It retains the agent record, transcript, result, and usage without retaining a live process.

Multi-turn instances require an explicit `keep_alive = true` profile setting. Keep-alive profiles must also define a bounded idle timeout, after which the manager parks a resumable runtime or stops a non-resumable one. A background run follows the same lifetime policy as a blocking run.

The initial bundled profiles are:

- `task`: General coding task.
- `scout`: Read-only repository exploration.
- `reviewer`: Read-only code review.

## Pi extension

The extension is responsible for presentation and transport only. It must not own subprocesses or durable lifecycle state.

### `task` tool

The tool accepts:

- `agent`
- `prompt`
- `background`
- `model`, when an override is allowed
- `access`
- `isolated`
- `maxRuntimeSeconds`

A blocking call subscribes to the run's events and returns after terminal state. A background call returns IDs immediately.

The final tool result includes nested model usage so Pi can include subagent work in session totals.

### Hub interface

The first Hub is a Pi TUI opened by `/hub`. It shows:

- Agent name and ID.
- Runtime.
- Parent.
- State.
- Current task.
- Current tool.
- Elapsed time.
- Token usage and cost.

Selecting an agent opens its transcript and actions. The extension rebuilds its view from `hub.snapshot` after reconnecting, then resumes events from the returned sequence.

## Scheduling and concurrency

The manager applies both a global limit and a per-workspace write limit.

- Read-only agents may run concurrently.
- Only one non-isolated writing agent may run in a workspace at a time.
- Isolated writing agents may run concurrently when their isolation backend permits it.
- A parent cancellation does not automatically cancel detached background children.
- Non-detached children are cancelled when their parent agent is stopped.

Queue entries retain creation order. Cancellation while queued removes the run without acquiring a slot.

Nested delegation records `parentAgentId` and `depth`. The manager rejects a spawn that exceeds the configured recursion depth, regardless of runtime behavior.

## Workspace isolation

The MVP supports two access modes:

- `read-only`: Mutation tools are unavailable.
- `shared-write`: The agent writes directly to the caller's workspace under the per-workspace lock.

Git worktree isolation is added later. An isolated run receives its own worktree and returns a patch or commit reference. Applying changes remains an explicit action.

The manager must never infer safety from an agent profile name. Access mode controls the actual tool set and process environment.

## Persistence

SQLite stores control-plane data:

- Agent records.
- Run records.
- Parent relationships.
- Idempotency keys.
- Runtime restoration metadata.
- Normalized events.
- Final results and usage.

Pi continues to own its conversation JSONL files. Agent Hub stores paths and metadata but does not rewrite Pi sessions.

The first event retention policy keeps all events for active agents and a configurable window for completed agents. Final results and state transitions are retained longer than streaming deltas.

Database writes and event publication must be ordered so a published sequence is already recoverable from storage.

## Failure handling

### Service restart

After restart:

- Running processes from the old daemon are considered lost.
- Their active runs become `failed` with a manager-restart reason.
- Resumable Pi agents become `parked` when a session file exists.
- Non-resumable agents become `failed` or `stopped` according to runtime metadata.

### Client disconnect

Agents continue running when the Pi extension disconnects. The extension reconnects, requests `hub.snapshot`, and resumes the event stream from the latest sequence.

### Runtime crash

An unexpected child exit marks the active run `failed`. The manager captures the exit code and bounded stderr output. A Pi agent with a valid session file may still be revived for a new turn.

### Slow clients

Each event subscriber has a bounded queue. A subscriber that cannot keep up is disconnected and must reconnect from its last persisted sequence. A slow Hub must never block an agent run.

## Security

- Create the runtime directory with mode `0700`.
- Create the socket for owner-only access.
- Never expose the UDS through an unauthenticated TCP proxy.
- Spawn subprocesses without `shell=True`.
- Validate and normalize every working directory.
- Pass a minimal environment to child processes.
- Keep secrets out of commands, events, logs, and SQLite records.
- Redact provider credentials from runtime errors.
- Enforce tool and filesystem permissions in code.
- Require an explicit opt-in before loading project-local profiles.
- Apply maximum depth, runtime, output, and concurrency limits centrally.

## Observability

Use structured logging for:

- Command acceptance and completion.
- Agent and run state transitions.
- Queue wait time.
- Runtime startup time.
- Time to first model output.
- Tool duration.
- Cancellation and shutdown escalation.
- Event subscriber lag.

Logs include agent and run IDs but exclude prompts and model output by default.

The Hub obtains user-facing usage and cost from normalized runtime events. Logs are not the source of truth for UI state.

## Testing strategy

Tests exercise the public HTTP API and runtime boundary.

### Protocol tests

- Parse records split across arbitrary byte chunks.
- Parse multiple records in one chunk.
- Preserve Unicode line separators inside JSON strings.
- Reject malformed JSON and oversized records.
- Correlate command IDs and responses.
- Resume events from a sequence cursor.

### Lifecycle tests

- Spawn, run, settle, and reuse an agent.
- Run in the background.
- Steer an active run.
- Queue a follow-up.
- Abort an active run.
- Cancel a queued run.
- Park and revive an idle agent.
- Handle runtime crashes.
- Recover state after manager restart.

### Concurrency tests

- Enforce the global limit.
- Serialize shared writers in the same workspace.
- Allow read-only concurrency.
- Release slots after failures and cancellation.
- Reject excessive delegation depth.

### Pi integration tests

A fixture executable implements Pi's public RPC protocol. Tests drive it only through the manager API and cover delayed output, malformed records, stderr, retries, settlement, and forced termination.

A smaller end-to-end suite runs against an installed Pi executable without contacting a model provider. Live provider behavior is not mocked into unit tests.

### Extension tests

- Connect through a Unix socket.
- Decode arbitrarily chunked JSONL.
- Render snapshots and incremental events.
- Return blocking results and usage.
- Return background handles.
- Reconnect without duplicating events.

## Delivery phases

### Phase 1: Service skeleton

- Create the Python package and CLI.
- Start Uvicorn on the configured UDS.
- Secure and clean up the socket path.
- Implement health checks.
- Implement JSONL request and response handling.
- Add configuration and structured logging.

Exit criteria: a client can send `hub.snapshot` over the UDS and receive a valid response.

### Phase 2: Registry and event stream

- Add agent and run state models.
- Add SQLite persistence.
- Add the manager-wide event sequence.
- Add the streaming event endpoint.
- Add cursor resume and snapshot reconciliation.
- Add idempotency handling.

Exit criteria: clients can reconnect without losing terminal state or duplicating commands.

### Phase 3: Pi runtime

- Spawn and supervise `pi --mode rpc`.
- Implement strict Pi JSONL parsing.
- Map Pi lifecycle and tool events.
- Implement prompt, steer, follow-up, abort, and stop.
- Capture session files, results, usage, and stderr.
- Implement park and revive.

Exit criteria: a Pi agent can complete multiple turns, survive a Hub disconnect, and resume from its session file.

### Phase 4: Pi extension

- Implement the UDS client.
- Register the `task` tool.
- Support blocking and background calls.
- Stream progress into Pi tool updates.
- Return nested usage.
- Add basic list, inspect, steer, and abort commands.

Exit criteria: a parent Pi session can delegate a task and receive its final result.

### Phase 5: Agent Hub TUI

- Add `/hub`.
- Render agents, runs, current tools, usage, and lineage.
- Add transcript view.
- Add steer, follow-up, abort, park, revive, and stop actions.
- Restore the view after manager or client reconnects.

Exit criteria: every lifecycle action can be performed without leaving Pi.

### Phase 6: Pydantic AI runtime

- Add profile-based Pydantic AI agents.
- Add explicit tool and MCP configuration.
- Normalize model, tool, output, error, and usage events.
- Add conversation continuation.
- Define and implement durable restoration separately.

Exit criteria: the same Pi `task` tool and Hub UI can operate both Pi and Pydantic AI agents without runtime-specific UI logic.

### Phase 7: Isolation and packaging

- Add git worktree isolation.
- Add patch inspection and explicit application.
- Add daemon auto-start or platform service files.
- Package the Pi extension and Python service together.
- Add a token-protected browser gateway only if needed.

Exit criteria: parallel writing agents cannot corrupt a shared workspace, and installation has one documented command.

## MVP acceptance criteria

The MVP is complete when:

- The service listens only on a user-owned Unix socket.
- A Pi extension can start the service or report that it is unavailable.
- Pi agents support blocking and background runs.
- Events stream as JSONL and resume by sequence.
- Users can list, inspect, steer, follow up, and abort agents.
- Completion waits for Pi's `agent_settled` event.
- Final output and nested usage return to the parent Pi session.
- The manager enforces concurrency and recursion limits.
- Disconnecting the parent Pi session does not terminate background agents.
- A parked Pi agent can resume from its session file.
- Public behavior has complete test coverage.

## Deferred decisions

These decisions should be made after the Pi-backed MVP is usable:

- Whether Pydantic AI conversations need cross-restart restoration.
- Whether the Hub should become a standalone TUI or browser application.
- Whether events need a PostgreSQL backend for longer retention.
- Whether remote workers are worth the authentication and security cost.
- Whether Agent Hub should expose an optional MCP facade for third-party clients.

MCP may be added as a convenience interface for tool discovery and simple operations. It should not replace the JSONL lifecycle stream.
