from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from agent_hub.persistence import Repository


@pytest.mark.anyio
async def test_migrates_existing_agent_tables(tmp_path: Path) -> None:
    path = tmp_path / "old.sqlite3"
    database = sqlite3.connect(path)
    database.execute(
        """CREATE TABLE agents (
            id TEXT PRIMARY KEY, runtime TEXT NOT NULL, profile TEXT NOT NULL, cwd TEXT NOT NULL,
            access TEXT NOT NULL, state TEXT NOT NULL, keep_alive INTEGER NOT NULL, depth INTEGER NOT NULL,
            parent_agent_id TEXT, root_session_id TEXT, restoration TEXT NOT NULL,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        )"""
    )
    database.commit()
    database.close()

    repository = Repository(path)
    await repository.open()
    await repository.close()

    database = sqlite3.connect(path)
    columns = {row[1] for row in database.execute("PRAGMA table_info(agents)").fetchall()}
    database.close()
    assert {"isolated", "detached"} <= columns


@pytest.mark.anyio
async def test_repository_rejects_missing_records_and_use_before_open(tmp_path: Path) -> None:
    repository = Repository(tmp_path / "hub.sqlite3")
    with pytest.raises(RuntimeError, match="not open"):
        await repository.get_agent("missing")

    await repository.open()
    assert repository.path.stat().st_mode & 0o777 == 0o600
    assert repository.path.parent.stat().st_mode & 0o777 == 0o700
    with pytest.raises(KeyError):
        await repository.update_agent("missing", "stopped")
    with pytest.raises(KeyError):
        await repository.update_run("missing", "failed")
    await repository.close()
