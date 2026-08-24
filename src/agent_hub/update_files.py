from __future__ import annotations

import fcntl
import os
import shutil
import sqlite3
from collections.abc import Generator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal


@dataclass(frozen=True)
class PathSnapshot:
    path: Path
    kind: Literal["missing", "file", "directory", "symlink"]
    backup_path: Path | None = None
    link_target: str | None = None


@dataclass(frozen=True)
class DatabaseBackup:
    database_path: Path
    existed: bool
    backup_path: Path | None


def snapshot_path(path: Path, destination: Path) -> PathSnapshot:
    if path.is_symlink():
        return PathSnapshot(path, "symlink", link_target=os.readlink(path))
    if path.is_file():
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        shutil.copy2(path, destination)
        return PathSnapshot(path, "file", destination)
    if path.is_dir():
        shutil.copytree(path, destination, symlinks=True)
        return PathSnapshot(path, "directory", destination)
    return PathSnapshot(path, "missing")


def restore_snapshot(snapshot: PathSnapshot) -> None:
    _remove_path(snapshot.path)
    if snapshot.kind == "missing":
        return
    snapshot.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if snapshot.kind == "symlink":
        if snapshot.link_target is None:
            raise RuntimeError(f"Missing link target for {snapshot.path}")
        snapshot.path.symlink_to(snapshot.link_target)
    elif snapshot.kind == "file":
        if snapshot.backup_path is None:
            raise RuntimeError(f"Missing file backup for {snapshot.path}")
        shutil.copy2(snapshot.backup_path, snapshot.path)
    else:
        if snapshot.backup_path is None:
            raise RuntimeError(f"Missing directory backup for {snapshot.path}")
        shutil.copytree(snapshot.backup_path, snapshot.path, symlinks=True)


def backup_database(database_path: Path, backup_directory: Path) -> DatabaseBackup:
    if not database_path.exists():
        return DatabaseBackup(database_path, False, None)
    backup_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    backup_path = backup_directory / f"agent-hub-{timestamp}.sqlite3"
    with closing(sqlite3.connect(f"file:{database_path}?mode=ro", uri=True)) as source:
        with closing(sqlite3.connect(backup_path)) as target:
            source.backup(target)
            integrity = target.execute("PRAGMA integrity_check").fetchone()
    if integrity != ("ok",):  # pragma: no cover - SQLite's backup API only produces a valid snapshot
        backup_path.unlink(missing_ok=True)
        raise RuntimeError("The Agent Hub database backup failed its integrity check")
    backup_path.chmod(0o600)
    return DatabaseBackup(database_path, True, backup_path)


def restore_database(backup: DatabaseBackup) -> None:
    for suffix in ("", "-wal", "-shm"):
        Path(f"{backup.database_path}{suffix}").unlink(missing_ok=True)
    if not backup.existed:
        return
    if backup.backup_path is None:
        raise RuntimeError("The Agent Hub database backup is missing")
    backup.database_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = backup.database_path.with_suffix(f"{backup.database_path.suffix}.restore")
    shutil.copy2(backup.backup_path, temporary)
    temporary.replace(backup.database_path)


@contextmanager
def acquire_update_lock(path: Path) -> Generator[None]:
    with path.open("w", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("Another Agent Hub update is already running") from exc
        yield


def _remove_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)
