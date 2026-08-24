from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import aiosqlite

from agent_hub.models import AgentRecord, AgentState, EventRecord, RunRecord, RunState, now


class Repository:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._database: aiosqlite.Connection | None = None

    async def open(self) -> None:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path.parent.chmod(0o700)
        self._database = await aiosqlite.connect(self.path)
        self.path.chmod(0o600)
        self._database.row_factory = aiosqlite.Row
        await self._database.execute("PRAGMA journal_mode=WAL")
        await self._database.execute("PRAGMA foreign_keys=ON")
        await self._database.executescript(
            """
            CREATE TABLE IF NOT EXISTS agents (
                id TEXT PRIMARY KEY, runtime TEXT NOT NULL, profile TEXT NOT NULL, cwd TEXT NOT NULL,
                access TEXT NOT NULL, state TEXT NOT NULL, keep_alive INTEGER NOT NULL, isolated INTEGER NOT NULL,
                detached INTEGER NOT NULL, depth INTEGER NOT NULL, parent_agent_id TEXT, root_session_id TEXT,
                restoration TEXT NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS runs (
                id TEXT PRIMARY KEY, agent_id TEXT NOT NULL REFERENCES agents(id), state TEXT NOT NULL,
                prompt TEXT NOT NULL, created_at TEXT NOT NULL, started_at TEXT, settled_at TEXT,
                result TEXT, usage TEXT NOT NULL, error TEXT
            );
            CREATE INDEX IF NOT EXISTS runs_agent_id ON runs(agent_id);
            CREATE TABLE IF NOT EXISTS events (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL, type TEXT NOT NULL,
                agent_id TEXT, run_id TEXT, data TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS idempotency (
                key TEXT PRIMARY KEY, method TEXT NOT NULL, result TEXT NOT NULL, created_at TEXT NOT NULL
            );
            """
        )
        columns = await self._database.execute_fetchall("PRAGMA table_info(agents)")
        if not any(row[1] == "isolated" for row in columns):
            await self._database.execute("ALTER TABLE agents ADD COLUMN isolated INTEGER NOT NULL DEFAULT 0")
        if not any(row[1] == "detached" for row in columns):
            await self._database.execute("ALTER TABLE agents ADD COLUMN detached INTEGER NOT NULL DEFAULT 0")
        await self._database.commit()

    async def close(self) -> None:
        if self._database is not None:
            await self._database.close()
            self._database = None

    async def create_agent(self, agent: AgentRecord) -> None:
        database = self._connection
        await database.execute(
            """INSERT INTO agents (
                id, runtime, profile, cwd, access, state, keep_alive, isolated, detached, depth,
                parent_agent_id, root_session_id, restoration, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                agent.id,
                agent.runtime,
                agent.profile,
                agent.cwd,
                agent.access,
                agent.state,
                agent.keep_alive,
                agent.isolated,
                agent.detached,
                agent.depth,
                agent.parent_agent_id,
                agent.root_session_id,
                json.dumps(agent.restoration),
                agent.created_at,
                agent.updated_at,
            ),
        )
        await database.commit()

    async def get_agent(self, agent_id: str) -> AgentRecord | None:
        cursor = await self._connection.execute("SELECT * FROM agents WHERE id = ?", (agent_id,))
        row = await cursor.fetchone()
        return self._agent(row) if row is not None else None

    async def list_agents(self, state: str | None = None, parent_agent_id: str | None = None) -> list[AgentRecord]:
        query = "SELECT * FROM agents"
        clauses: list[str] = []
        values: list[str] = []
        if state is not None:
            clauses.append("state = ?")
            values.append(state)
        if parent_agent_id is not None:
            clauses.append("parent_agent_id = ?")
            values.append(parent_agent_id)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY created_at"
        cursor = await self._connection.execute(query, values)
        return [self._agent(row) for row in await cursor.fetchall()]

    async def update_agent(
        self,
        agent_id: str,
        state: AgentState,
        restoration: dict[str, Any] | None = None,
    ) -> AgentRecord:
        agent = await self.get_agent(agent_id)
        if agent is None:
            raise KeyError(agent_id)
        updated = replace(agent, state=state, restoration=restoration or agent.restoration, updated_at=now())
        await self._connection.execute(
            "UPDATE agents SET state = ?, restoration = ?, updated_at = ? WHERE id = ?",
            (updated.state, json.dumps(updated.restoration), updated.updated_at, agent_id),
        )
        await self._connection.commit()
        return updated

    async def create_run(self, run: RunRecord) -> None:
        await self._connection.execute(
            "INSERT INTO runs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                run.id,
                run.agent_id,
                run.state,
                run.prompt,
                run.created_at,
                run.started_at,
                run.settled_at,
                run.result,
                json.dumps(run.usage),
                run.error,
            ),
        )
        await self._connection.commit()

    async def get_run(self, run_id: str) -> RunRecord | None:
        cursor = await self._connection.execute("SELECT * FROM runs WHERE id = ?", (run_id,))
        row = await cursor.fetchone()
        return self._run(row) if row is not None else None

    async def list_runs(self, agent_id: str | None = None, active_only: bool = False) -> list[RunRecord]:
        clauses: list[str] = []
        values: list[str] = []
        if agent_id is not None:
            clauses.append("agent_id = ?")
            values.append(agent_id)
        if active_only:
            clauses.append("state IN ('queued', 'running')")
        query = "SELECT * FROM runs" + ((" WHERE " + " AND ".join(clauses)) if clauses else "")
        cursor = await self._connection.execute(query + " ORDER BY created_at", values)
        return [self._run(row) for row in await cursor.fetchall()]

    async def update_run(
        self,
        run_id: str,
        state: RunState,
        *,
        result: str | None = None,
        usage: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> RunRecord:
        run = await self.get_run(run_id)
        if run is None:
            raise KeyError(run_id)
        started_at = now() if state == "running" and run.started_at is None else run.started_at
        settled_at = now() if state in {"succeeded", "failed", "aborted"} else run.settled_at
        updated = replace(
            run,
            state=state,
            started_at=started_at,
            settled_at=settled_at,
            result=result if result is not None else run.result,
            usage=usage if usage is not None else run.usage,
            error=error if error is not None else run.error,
        )
        await self._connection.execute(
            "UPDATE runs SET state = ?, started_at = ?, settled_at = ?, result = ?, usage = ?, error = ? WHERE id = ?",
            (
                updated.state,
                updated.started_at,
                updated.settled_at,
                updated.result,
                json.dumps(updated.usage),
                updated.error,
                run_id,
            ),
        )
        await self._connection.commit()
        return updated

    async def update_run_usage(self, run_id: str, usage: dict[str, Any]) -> None:
        await self._connection.execute("UPDATE runs SET usage = ? WHERE id = ?", (json.dumps(usage), run_id))
        await self._connection.commit()

    async def append_event(
        self, event_type: str, agent_id: str | None, run_id: str | None, data: dict[str, Any]
    ) -> EventRecord:
        timestamp = now()
        cursor = await self._connection.execute(
            "INSERT INTO events(timestamp, type, agent_id, run_id, data) VALUES (?, ?, ?, ?, ?)",
            (timestamp, event_type, agent_id, run_id, json.dumps(data)),
        )
        await self._connection.commit()
        if cursor.lastrowid is None:  # pragma: no cover - SQLite INSERT always returns the row ID
            raise RuntimeError("SQLite did not return an event sequence")
        return EventRecord(cursor.lastrowid, timestamp, event_type, agent_id, run_id, data)

    async def events_after(self, sequence: int) -> list[EventRecord]:
        cursor = await self._connection.execute(
            "SELECT * FROM events WHERE sequence > ? ORDER BY sequence", (sequence,)
        )
        return [self._event(row) for row in await cursor.fetchall()]

    async def events_for_agent(self, agent_id: str) -> list[EventRecord]:
        cursor = await self._connection.execute(
            "SELECT * FROM events WHERE agent_id = ? ORDER BY sequence", (agent_id,)
        )
        return [self._event(row) for row in await cursor.fetchall()]

    async def latest_sequence(self) -> int:
        cursor = await self._connection.execute("SELECT COALESCE(MAX(sequence), 0) AS value FROM events")
        row = await cursor.fetchone()
        if row is None:  # pragma: no cover - aggregate SELECT always returns one row
            return 0
        return int(row["value"])

    async def get_idempotent(self, key: str, method: str) -> dict[str, Any] | None:
        cursor = await self._connection.execute("SELECT method, result FROM idempotency WHERE key = ?", (key,))
        row = await cursor.fetchone()
        if row is None:
            return None
        if row["method"] != method:
            raise ValueError("Idempotency key was already used for another method")
        return dict(json.loads(row["result"]))

    async def put_idempotent(self, key: str, method: str, result: dict[str, Any]) -> None:
        await self._connection.execute(
            "INSERT INTO idempotency VALUES (?, ?, ?, ?)", (key, method, json.dumps(result), now())
        )
        await self._connection.commit()

    async def prune_completed_streaming_events(self, retain: int) -> None:
        await self._connection.execute(
            """DELETE FROM events WHERE sequence IN (
                SELECT events.sequence FROM events
                JOIN agents ON agents.id = events.agent_id
                WHERE agents.state IN ('stopped', 'failed')
                AND events.type IN ('run.output.delta', 'run.thinking.delta', 'run.tool.updated', 'runtime.stderr')
                ORDER BY events.sequence DESC LIMIT -1 OFFSET ?
            )""",
            (retain,),
        )
        await self._connection.commit()

    async def recover_after_restart(self, is_resumable: Callable[[AgentRecord], bool]) -> None:
        timestamp = now()
        await self._connection.execute(
            """UPDATE runs SET state = 'failed', settled_at = ?, error = 'Agent Hub restarted'
            WHERE state IN ('queued', 'running')""",
            (timestamp,),
        )
        cursor = await self._connection.execute(
            "SELECT * FROM agents WHERE state IN ('starting', 'idle', 'running', 'stopping')"
        )
        for row in await cursor.fetchall():
            state = "parked" if is_resumable(self._agent(row)) else "failed"
            await self._connection.execute(
                "UPDATE agents SET state = ?, updated_at = ? WHERE id = ?",
                (state, timestamp, row["id"]),
            )
        await self._connection.commit()

    @property
    def _connection(self) -> aiosqlite.Connection:
        if self._database is None:
            raise RuntimeError("Repository is not open")
        return self._database

    @staticmethod
    def _agent(row: aiosqlite.Row) -> AgentRecord:
        return AgentRecord(
            id=row["id"],
            runtime=row["runtime"],
            profile=row["profile"],
            cwd=row["cwd"],
            access=row["access"],
            state=row["state"],
            keep_alive=bool(row["keep_alive"]),
            isolated=bool(row["isolated"]),
            detached=bool(row["detached"]),
            depth=row["depth"],
            parent_agent_id=row["parent_agent_id"],
            root_session_id=row["root_session_id"],
            restoration=json.loads(row["restoration"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _run(row: aiosqlite.Row) -> RunRecord:
        return RunRecord(
            id=row["id"],
            agent_id=row["agent_id"],
            state=row["state"],
            prompt=row["prompt"],
            created_at=row["created_at"],
            started_at=row["started_at"],
            settled_at=row["settled_at"],
            result=row["result"],
            usage=json.loads(row["usage"]),
            error=row["error"],
        )

    @staticmethod
    def _event(row: aiosqlite.Row) -> EventRecord:
        return EventRecord(
            sequence=row["sequence"],
            timestamp=row["timestamp"],
            type=row["type"],
            agent_id=row["agent_id"],
            run_id=row["run_id"],
            data=json.loads(row["data"]),
        )
