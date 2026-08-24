from __future__ import annotations

import os
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Literal

import pytest

from agent_hub.update_files import (
    DatabaseBackup,
    PathSnapshot,
    backup_database,
    restore_database,
    restore_snapshot,
    snapshot_path,
)


def test_snapshots_and_restores_files_directories_links_and_missing_paths(tmp_path: Path) -> None:
    file_path = tmp_path / "file"
    file_path.write_text("before", encoding="utf-8")
    file_snapshot = snapshot_path(file_path, tmp_path / "snapshots" / "file")
    file_path.write_text("after", encoding="utf-8")
    restore_snapshot(file_snapshot)
    assert file_path.read_text(encoding="utf-8") == "before"

    directory = tmp_path / "directory"
    directory.mkdir()
    (directory / "item").write_text("before", encoding="utf-8")
    directory_snapshot = snapshot_path(directory, tmp_path / "snapshots" / "directory")
    (directory / "item").write_text("after", encoding="utf-8")
    restore_snapshot(directory_snapshot)
    assert (directory / "item").read_text(encoding="utf-8") == "before"

    link = tmp_path / "link"
    link.symlink_to(file_path)
    link_snapshot = snapshot_path(link, tmp_path / "snapshots" / "link")
    link.unlink()
    link.write_text("replacement", encoding="utf-8")
    restore_snapshot(link_snapshot)
    assert link.is_symlink()
    assert os.readlink(link) == str(file_path)

    missing = tmp_path / "missing"
    missing_snapshot = snapshot_path(missing, tmp_path / "snapshots" / "missing")
    missing.mkdir()
    restore_snapshot(missing_snapshot)
    assert not missing.exists()


def test_backs_up_and_restores_sqlite_databases(tmp_path: Path) -> None:
    database = tmp_path / "agent-hub.sqlite3"
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("CREATE TABLE records (value TEXT)")
        connection.execute("INSERT INTO records VALUES ('before')")
        connection.commit()

    backup = backup_database(database, tmp_path / "backups")
    assert backup.backup_path is not None
    assert backup.backup_path.stat().st_mode & 0o777 == 0o600
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("UPDATE records SET value = 'after'")
        connection.commit()
    Path(f"{database}-wal").touch()
    Path(f"{database}-shm").touch()

    restore_database(backup)

    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute("SELECT value FROM records").fetchone() == ("before",)
    assert not Path(f"{database}-wal").exists()
    assert not Path(f"{database}-shm").exists()

    absent = tmp_path / "absent.sqlite3"
    absent_backup = backup_database(absent, tmp_path / "backups")
    absent.touch()
    restore_database(absent_backup)
    assert not absent.exists()


@pytest.mark.parametrize("kind", ["symlink", "file", "directory"])
def test_rejects_incomplete_path_snapshots(tmp_path: Path, kind: Literal["symlink", "file", "directory"]) -> None:
    snapshot = PathSnapshot(tmp_path / "target", kind)

    with pytest.raises(RuntimeError, match="Missing .* backup|Missing link target"):
        restore_snapshot(snapshot)


def test_rejects_missing_database_backup(tmp_path: Path) -> None:
    backup = DatabaseBackup(tmp_path / "database", True, None)

    with pytest.raises(RuntimeError, match="backup is missing"):
        restore_database(backup)
