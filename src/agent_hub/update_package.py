from __future__ import annotations

import os
import shutil
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import anyio

from agent_hub.assets import BundledAssets, load_bundled_assets


@dataclass(frozen=True)
class StagedPackage:
    environment: Path
    assets: BundledAssets


@dataclass(frozen=True)
class ToolSwap:
    environment: Path
    backup: Path


async def stage_package(uv: str, source: str, transaction: Path) -> StagedPackage:
    tool_directory = transaction / "tools"
    bin_directory = transaction / "bin"
    environment = {**os.environ, "UV_TOOL_DIR": str(tool_directory), "UV_TOOL_BIN_DIR": str(bin_directory)}
    await run_checked(
        [uv, "tool", "install", "--force", "--refresh-package", "agent-hub", source],
        environment,
    )
    tool_environment = tool_directory / "agent-hub"
    executable = tool_environment / "bin" / "agent-hub"
    python = tool_environment / "bin" / "python"
    await run_checked([str(executable), "--help"])
    await run_checked([uv, "pip", "check", "--python", str(python)])
    asset_root = Path(
        (
            await run_output(
                [
                    str(python),
                    "-c",
                    "from importlib.resources import files; print(files('agent_hub').joinpath('assets'))",
                ]
            )
        ).strip()
    )
    return StagedPackage(tool_environment, load_bundled_assets(asset_root))


def promote_package(staged: Path, destination: Path) -> ToolSwap:
    identifier = uuid.uuid4().hex
    prepared = destination.parent / f".agent-hub-prepared-{identifier}"
    backup = destination.parent / f".agent-hub-backup-{identifier}"
    shutil.copytree(staged, prepared, symlinks=True)
    _rewrite_environment_paths(prepared, staged, destination)
    destination.replace(backup)
    try:
        prepared.replace(destination)
    except OSError:  # pragma: no cover - defensive recovery for an atomic rename failure
        backup.replace(destination)
        raise
    return ToolSwap(destination, backup)


def restore_package(swap: ToolSwap) -> None:
    failed = swap.environment.parent / f".agent-hub-failed-{uuid.uuid4().hex}"
    if swap.environment.exists():
        swap.environment.replace(failed)
    swap.backup.replace(swap.environment)
    shutil.rmtree(failed, ignore_errors=True)


def discard_package_backup(swap: ToolSwap) -> None:
    shutil.rmtree(swap.backup, ignore_errors=True)


async def run_checked(command: list[str], environment: Mapping[str, str] | None = None) -> None:
    await run_output(command, environment)


async def run_output(command: list[str], environment: Mapping[str, str] | None = None) -> str:
    result = await anyio.run_process(command, check=False, env=environment)
    if result.returncode != 0:
        message = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(message or f"Command failed: {' '.join(command)}")
    return result.stdout.decode("utf-8", errors="strict")


def _rewrite_environment_paths(environment: Path, old_path: Path, new_path: Path) -> None:
    old = os.fsencode(old_path)
    new = os.fsencode(new_path)
    for path in (environment / "bin").iterdir():
        if path.is_symlink() or not path.is_file():
            continue
        contents = path.read_bytes()
        if old in contents:
            path.write_bytes(contents.replace(old, new))
