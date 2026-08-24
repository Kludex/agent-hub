from __future__ import annotations

from pathlib import Path

import anyio
import pytest

from agent_hub.isolation import IsolationFailure, IsolationManager
from agent_hub.models import AgentRecord
from tests.conftest import RunningHub


async def git(cwd: Path, *arguments: str) -> None:
    result = await anyio.run_process(["git", "-C", str(cwd), *arguments], check=False)
    if result.returncode != 0:  # pragma: no cover - reports Git test infrastructure failure
        raise AssertionError(result.stderr.decode())


async def repository(path: Path) -> Path:
    path.mkdir()
    await git(path, "init", "--quiet")
    await git(path, "config", "user.email", "agent-hub@example.com")
    await git(path, "config", "user.name", "Agent Hub")
    (path / "tracked.txt").write_text("before\n", encoding="utf-8")
    await git(path, "add", "tracked.txt")
    await git(path, "commit", "--quiet", "-m", "Initial commit")
    return path


@pytest.mark.anyio
async def test_rejects_an_existing_worktree_for_the_same_agent(tmp_path: Path) -> None:
    source = await repository(tmp_path / "duplicate-repository")
    manager = IsolationManager(tmp_path / "data")
    agent = AgentRecord(
        id="agt_duplicate",
        runtime="pi",
        profile="task",
        cwd=str(source),
        access="shared-write",
        state="starting",
        keep_alive=False,
        isolated=True,
    )
    await manager.prepare(agent)

    with pytest.raises(IsolationFailure, match="already exists"):
        await manager.prepare(agent)

    await git(source, "worktree", "remove", "--force", str(tmp_path / "data" / "worktrees" / agent.id))


@pytest.mark.anyio
async def test_isolated_run_returns_and_applies_a_patch(hub: RunningHub, tmp_path: Path) -> None:
    source = await repository(tmp_path / "repository")
    spawned = await hub.rpc(
        "agent.spawn",
        {"prompt": "block-isolated", "cwd": str(source), "isolated": True},
    )
    for _ in range(100):
        if hub.runtime.start_requests:
            break
        await anyio.sleep(0.01)
    else:  # pragma: no cover - reports test infrastructure failure
        raise AssertionError("Isolated runtime did not start")
    worktree = hub.runtime.start_requests[0].cwd
    assert worktree != source
    assert worktree.is_relative_to(hub.config.data_dir / "worktrees")
    assert "error" in await hub.rpc("agent.apply", {"agentId": spawned["agentId"]})
    assert "error" in await hub.rpc("agent.discard", {"agentId": spawned["agentId"]})

    (worktree / "tracked.txt").write_text("after\n", encoding="utf-8")
    (worktree / "created.txt").write_text("created\n", encoding="utf-8")
    hub.runtime.release()
    settled = await hub.rpc("run.wait", {"runId": spawned["runId"], "timeoutSeconds": 3})
    assert settled["run"]["state"] == "succeeded"
    assert (source / "tracked.txt").read_text(encoding="utf-8") == "before\n"
    assert not (source / "created.txt").exists()

    inspected = await hub.rpc("agent.patch", {"agentId": spawned["agentId"]})
    assert "tracked.txt" in inspected["patch"]
    assert "created.txt" in inspected["patch"]
    assert inspected["truncated"] is False
    hub.config.max_output_bytes = 10
    truncated = await hub.rpc("agent.patch", {"agentId": spawned["agentId"]})
    assert truncated["truncated"] is True

    assert await hub.rpc("agent.apply", {"agentId": spawned["agentId"]}) == {"applied": True}
    assert (source / "tracked.txt").read_text(encoding="utf-8") == "after\n"
    assert (source / "created.txt").read_text(encoding="utf-8") == "created\n"

    assert await hub.rpc("agent.discard", {"agentId": spawned["agentId"]}) == {"discarded": True}
    assert not worktree.exists()


@pytest.mark.anyio
async def test_isolated_patch_conflicts_are_reported(hub: RunningHub, tmp_path: Path) -> None:
    source = await repository(tmp_path / "conflict-repository")
    spawned = await hub.rpc(
        "agent.spawn",
        {"prompt": "block-conflict", "cwd": str(source), "isolated": True},
    )
    for _ in range(100):
        if hub.runtime.start_requests:
            break
        await anyio.sleep(0.01)
    worktree = hub.runtime.start_requests[-1].cwd
    (worktree / "tracked.txt").write_text("agent\n", encoding="utf-8")
    hub.runtime.release()
    await hub.rpc("run.wait", {"runId": spawned["runId"], "timeoutSeconds": 3})
    (source / "tracked.txt").write_text("source\n", encoding="utf-8")

    applied = await hub.rpc("agent.apply", {"agentId": spawned["agentId"]})

    assert applied["error"]["code"] == -32010
    assert await hub.rpc("agent.discard", {"agentId": spawned["agentId"]}) == {"discarded": True}


@pytest.mark.anyio
async def test_isolation_requires_a_git_repository(hub: RunningHub, tmp_path: Path) -> None:
    spawned = await hub.rpc(
        "agent.spawn",
        {"prompt": "isolated", "cwd": str(tmp_path), "isolated": True},
    )

    settled = await hub.rpc("run.wait", {"runId": spawned["runId"], "timeoutSeconds": 3})

    assert settled["run"]["state"] == "failed"
    assert "not a git repository" in settled["run"]["error"]
    patch = await hub.rpc("agent.patch", {"agentId": spawned["agentId"]})
    discarded = await hub.rpc("agent.discard", {"agentId": spawned["agentId"]})
    assert patch["error"]["code"] == -32010
    assert discarded["error"]["code"] == -32010


@pytest.mark.anyio
async def test_applying_an_empty_isolated_patch_is_a_no_op(hub: RunningHub, tmp_path: Path) -> None:
    source = await repository(tmp_path / "empty-repository")
    spawned = await hub.rpc(
        "agent.spawn",
        {"prompt": "unchanged", "cwd": str(source), "isolated": True},
    )
    await hub.rpc("run.wait", {"runId": spawned["runId"], "timeoutSeconds": 3})

    assert await hub.rpc("agent.apply", {"agentId": spawned["agentId"]}) == {"applied": True}
    assert await hub.rpc("agent.discard", {"agentId": spawned["agentId"]}) == {"discarded": True}
