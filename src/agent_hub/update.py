from __future__ import annotations

import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

import anyio
import anyio.to_thread

from agent_hub.assets import extension_path, install_bundled_assets, skills_path
from agent_hub.config import HubConfig
from agent_hub.service import service_path, start_service, stop_service
from agent_hub.update_files import (
    DatabaseBackup,
    PathSnapshot,
    acquire_update_lock,
    backup_database,
    restore_database,
    restore_snapshot,
    snapshot_path,
)
from agent_hub.update_health import wait_for_health
from agent_hub.update_package import (
    StagedPackage,
    ToolSwap,
    discard_package_backup,
    promote_package,
    restore_package,
    run_checked,
    run_output,
    stage_package,
)

DEFAULT_UPDATE_SOURCE = "git+https://github.com/Kludex/agent-hub.git"


@dataclass(frozen=True)
class UpdateResult:
    database_backup: Path | None


async def update(
    config: HubConfig,
    source: str = DEFAULT_UPDATE_SOURCE,
    health_timeout_seconds: float = 15,
) -> UpdateResult:
    if health_timeout_seconds <= 0:
        raise ValueError("health_timeout_seconds must be positive")
    if not service_path().is_file():
        raise RuntimeError("Agent Hub is not installed as a user service; run `agent-hub install` first")
    uv = shutil.which("uv")
    if uv is None:
        raise RuntimeError("Agent Hub updates require uv on PATH")
    database_path = config.database_path
    socket_path = config.socket_path
    if database_path is None or socket_path is None:  # pragma: no cover - HubConfig always configures both paths
        raise RuntimeError("Agent Hub paths are not configured")

    update_directory = config.data_dir / "updates"
    update_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    with acquire_update_lock(update_directory / "update.lock"):
        with tempfile.TemporaryDirectory(prefix="transaction-", dir=update_directory) as temporary_name:
            temporary = Path(temporary_name)
            staged = await stage_package(uv, source, temporary)
            tool_directory = Path((await run_output([uv, "tool", "dir"])).strip())
            tool_environment = tool_directory / "agent-hub"
            if not tool_environment.is_dir():
                raise RuntimeError("Agent Hub is not installed as a uv tool")
            extension_snapshot = await anyio.to_thread.run_sync(
                snapshot_path, extension_path(), temporary / "extension-backup"
            )
            skills_snapshot = await anyio.to_thread.run_sync(snapshot_path, skills_path(), temporary / "skills-backup")
            return await _apply_update(
                config,
                staged,
                tool_environment,
                extension_snapshot,
                skills_snapshot,
                health_timeout_seconds,
            )


async def _apply_update(
    config: HubConfig,
    staged: StagedPackage,
    tool_environment: Path,
    extension_snapshot: PathSnapshot,
    skills_snapshot: PathSnapshot,
    health_timeout_seconds: float,
) -> UpdateResult:
    database_backup: DatabaseBackup | None = None
    tool_swap: ToolSwap | None = None
    await stop_service()
    try:
        if config.database_path is None:  # pragma: no cover - HubConfig always configures the database path
            raise RuntimeError("Agent Hub database path is not configured")
        database_backup = await anyio.to_thread.run_sync(
            backup_database, config.database_path, config.data_dir / "backups"
        )
        tool_swap = await anyio.to_thread.run_sync(promote_package, staged.environment, tool_environment)
        await run_checked([str(tool_environment / "bin" / "agent-hub"), "--help"])
        install_bundled_assets(staged.assets)
        await start_service()
        await wait_for_health(config, health_timeout_seconds)
    except BaseException as exc:
        rollback_error: BaseException | None = None
        with anyio.CancelScope(shield=True):
            rollback_error = await _rollback(
                config,
                tool_swap,
                extension_snapshot,
                skills_snapshot,
                database_backup,
                health_timeout_seconds,
            )
        if rollback_error is not None:
            raise RuntimeError(f"Agent Hub update failed: {exc}; rollback failed: {rollback_error}") from exc
        raise RuntimeError(f"Agent Hub update failed and was rolled back: {exc}") from exc
    await anyio.to_thread.run_sync(discard_package_backup, tool_swap)
    return UpdateResult(database_backup.backup_path)


async def _rollback(
    config: HubConfig,
    tool_swap: ToolSwap | None,
    extension_snapshot: PathSnapshot,
    skills_snapshot: PathSnapshot,
    database_backup: DatabaseBackup | None,
    health_timeout_seconds: float,
) -> BaseException | None:
    try:
        await stop_service(check=False)
        if tool_swap is not None:
            await anyio.to_thread.run_sync(restore_package, tool_swap)
        await anyio.to_thread.run_sync(restore_snapshot, extension_snapshot)
        await anyio.to_thread.run_sync(restore_snapshot, skills_snapshot)
        if database_backup is not None:
            await anyio.to_thread.run_sync(restore_database, database_backup)
        await start_service()
        await wait_for_health(config, health_timeout_seconds)
    except BaseException as exc:
        return exc
    return None
