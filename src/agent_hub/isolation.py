from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import anyio

from agent_hub.models import AgentRecord


class IsolationFailure(Exception):
    """A git worktree operation could not be completed safely."""


@dataclass(frozen=True)
class Worktree:
    source_root: Path
    path: Path
    cwd: Path
    base_commit: str

    def as_restoration(self) -> dict[str, Any]:
        return {
            "kind": "git-worktree",
            "sourceRoot": str(self.source_root),
            "worktreePath": str(self.path),
            "relativeCwd": str(self.cwd.relative_to(self.path)),
            "baseCommit": self.base_commit,
        }


class IsolationManager:
    def __init__(self, data_directory: Path) -> None:
        self._data_directory = data_directory

    async def prepare(self, agent: AgentRecord) -> Worktree:
        source_cwd = Path(agent.cwd)
        source_root = Path(await self._git(source_cwd, "rev-parse", "--show-toplevel"))
        try:
            relative_cwd = source_cwd.relative_to(source_root)
        except ValueError as exc:  # pragma: no cover - Git always returns an ancestor for this command
            raise IsolationFailure("Working directory is outside its Git repository") from exc
        base_commit = await self._git(source_root, "rev-parse", "HEAD")
        worktree_path = self._data_directory / "worktrees" / agent.id
        worktree_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if worktree_path.exists():
            raise IsolationFailure(f"Isolation worktree already exists for {agent.id}")
        await self._git(source_root, "worktree", "add", "--detach", str(worktree_path), base_commit)
        return Worktree(source_root, worktree_path, worktree_path / relative_cwd, base_commit)

    async def capture(self, agent: AgentRecord, restoration: dict[str, Any]) -> dict[str, Any]:
        worktree = self._from_agent(agent)
        await self._git(worktree.path, "add", "--intent-to-add", "--all")
        patch = await self._git_bytes(worktree.path, "diff", "--binary", worktree.base_commit)
        head_commit = await self._git(worktree.path, "rev-parse", "HEAD")
        patch_directory = self._data_directory / "patches"
        patch_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        patch_path = patch_directory / f"{agent.id}.patch"
        patch_path.write_bytes(patch)
        patch_path.chmod(0o600)
        return {
            **restoration,
            "isolation": {
                **worktree.as_restoration(),
                "headCommit": head_commit,
                "patchPath": str(patch_path),
            },
        }

    async def inspect(self, agent: AgentRecord, limit: int) -> tuple[str, bool]:
        patch_path = self._patch_path(agent)
        patch = await anyio.Path(patch_path).read_bytes()
        truncated = len(patch) > limit
        return patch[:limit].decode("utf-8", errors="replace"), truncated

    async def apply(self, agent: AgentRecord) -> None:
        worktree = self._from_agent(agent)
        patch_path = self._patch_path(agent)
        if patch_path.stat().st_size == 0:
            return
        await self._git(worktree.source_root, "apply", "--3way", str(patch_path))

    async def discard(self, agent: AgentRecord) -> None:
        worktree = self._from_agent(agent)
        if worktree.path.exists():
            await self._git(worktree.source_root, "worktree", "remove", "--force", str(worktree.path))
        patch_path = self._optional_patch_path(agent)
        if patch_path is not None:
            patch_path.unlink(missing_ok=True)
        if worktree.path.exists():  # pragma: no cover - defensive cleanup after successful Git removal
            shutil.rmtree(worktree.path)

    def runtime_cwd(self, agent: AgentRecord) -> Path:
        if not agent.isolated:
            return Path(agent.cwd)
        return self._from_agent(agent).cwd

    @staticmethod
    def _from_agent(agent: AgentRecord) -> Worktree:
        raw = agent.restoration.get("isolation")
        if not isinstance(raw, dict) or raw.get("kind") != "git-worktree":
            raise IsolationFailure(f"Agent {agent.id} has no isolation worktree")
        try:
            source_root = Path(str(raw["sourceRoot"]))
            path = Path(str(raw["worktreePath"]))
            relative_cwd = Path(str(raw["relativeCwd"]))
            base_commit = str(raw["baseCommit"])
        except KeyError as exc:  # pragma: no cover - metadata is written atomically by prepare()
            raise IsolationFailure(f"Agent {agent.id} has incomplete isolation metadata") from exc
        return Worktree(source_root, path, path / relative_cwd, base_commit)

    @staticmethod
    def _optional_patch_path(agent: AgentRecord) -> Path | None:
        raw = agent.restoration.get("isolation")
        if not isinstance(raw, dict) or not isinstance(raw.get("patchPath"), str):
            return None
        return Path(raw["patchPath"])

    @classmethod
    def _patch_path(cls, agent: AgentRecord) -> Path:
        path = cls._optional_patch_path(agent)
        if path is None:
            raise IsolationFailure(f"Agent {agent.id} has no captured patch")
        return path

    @classmethod
    async def _git(cls, cwd: Path, *arguments: str) -> str:
        return (await cls._git_bytes(cwd, *arguments)).decode("utf-8", errors="replace").strip()

    @staticmethod
    async def _git_bytes(cwd: Path, *arguments: str) -> bytes:
        result = await anyio.run_process(
            ["git", "-C", str(cwd), *arguments],
            check=False,
            stderr=-1,
            stdout=-1,
        )
        if result.returncode != 0:
            message = result.stderr.decode("utf-8", errors="replace").strip()
            raise IsolationFailure(message or f"git {arguments[0]} failed")
        return result.stdout
