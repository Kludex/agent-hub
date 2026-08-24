from __future__ import annotations

from pathlib import Path

import pytest

from agent_hub.workspace_tools import WorkspaceViolation, create_workspace_tools


@pytest.mark.anyio
async def test_workspace_tools_enforce_boundaries_and_output_limits(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "existing.txt").write_text("abcdefgh", encoding="utf-8")
    tools, read_only = create_workspace_tools(workspace, max_output_bytes=4)

    assert await tools["read_file"]("existing.txt") == "abcd"
    assert await tools["list_files"]() == ["exis"]
    assert await tools["write_file"]("directory/new.txt", "created") == "directory/new.txt"
    assert (workspace / "directory" / "new.txt").read_text(encoding="utf-8") == "created"
    assert read_only == {"read_file", "list_files"}

    (tmp_path / "outside.txt").write_text("secret", encoding="utf-8")
    with pytest.raises(WorkspaceViolation, match="outside"):
        await tools["read_file"]("../outside.txt")
    with pytest.raises(WorkspaceViolation, match="outside"):
        await tools["list_files"]("../*")
