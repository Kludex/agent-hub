from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import anyio


class WorkspaceViolation(Exception):
    """A tool attempted to access a path outside its assigned workspace."""


def create_workspace_tools(root: Path, max_output_bytes: int) -> tuple[dict[str, Callable[..., Any]], set[str]]:
    workspace = root.resolve(strict=True)

    def resolve(path: str, *, must_exist: bool) -> Path:
        candidate = (workspace / path).resolve(strict=must_exist)
        if not candidate.is_relative_to(workspace):
            raise WorkspaceViolation(f"Path is outside the assigned workspace: {path}")
        return candidate

    async def read_file(path: str) -> str:
        data = await anyio.Path(resolve(path, must_exist=True)).read_bytes()
        return data[:max_output_bytes].decode("utf-8", errors="replace")

    async def list_files(pattern: str = "**/*") -> list[str]:
        if Path(pattern).is_absolute() or ".." in Path(pattern).parts:
            raise WorkspaceViolation(f"Pattern is outside the assigned workspace: {pattern}")
        paths = [str(path.relative_to(workspace)) for path in workspace.glob(pattern) if path.is_file()]
        encoded = "\n".join(sorted(paths)).encode("utf-8")[:max_output_bytes]
        return encoded.decode("utf-8", errors="ignore").splitlines()

    async def write_file(path: str, content: str) -> str:
        target = resolve(path, must_exist=False)
        target.parent.mkdir(parents=True, exist_ok=True)
        await anyio.Path(target).write_text(content, encoding="utf-8")
        return str(target.relative_to(workspace))

    return {
        "read_file": read_file,
        "list_files": list_files,
        "write_file": write_file,
    }, {"read_file", "list_files"}
