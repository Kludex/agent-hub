from __future__ import annotations

import fcntl
import os
import shutil
import stat
from pathlib import Path

import pytest

from agent_hub.assets import SKILL_NAMES, extension_path, skills_path
from agent_hub.update import update
from tests.conftest import RunningHub


@pytest.mark.anyio
async def test_updates_staged_package_assets_database_and_service(
    hub: RunningHub,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    home, tool_environment, service_log = _prepare_update_environment(monkeypatch, tmp_path)
    old_marker = tool_environment / "old-package"
    old_marker.write_text("old", encoding="utf-8")
    extension_path().parent.mkdir(parents=True)
    extension_path().write_text("old extension", encoding="utf-8")
    for name in SKILL_NAMES:
        destination = skills_path() / name / "SKILL.md"
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text("old skill", encoding="utf-8")

    result = await update(hub.config, "fixture-package", health_timeout_seconds=0.5)

    assert result.database_backup is not None
    assert result.database_backup.is_file()
    assert not old_marker.exists()
    asset_root = Path(__file__).parents[1] / "src" / "agent_hub" / "assets"
    assert extension_path().read_bytes() == (asset_root / "agent-hub.js").read_bytes()
    assert (skills_path() / "issue-triage" / "SKILL.md").read_bytes() == (
        asset_root / "skills" / "issue-triage" / "SKILL.md"
    ).read_bytes()
    assert str(tool_environment).encode() in (tool_environment / "bin" / "activate").read_bytes()
    assert b"transaction-" not in (tool_environment / "bin" / "activate").read_bytes()
    assert service_log.read_text(encoding="utf-8").splitlines() == [
        "--user stop agent-hub.service",
        "--user start agent-hub.service",
    ]
    assert home.is_dir()


@pytest.mark.anyio
async def test_rolls_back_when_asset_installation_fails(
    hub: RunningHub,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _, tool_environment, service_log = _prepare_update_environment(monkeypatch, tmp_path)
    old_marker = tool_environment / "old-package"
    old_marker.write_text("old", encoding="utf-8")
    extension_parent = extension_path().parent
    extension_parent.parent.mkdir(parents=True)
    extension_parent.write_text("blocks extension directory", encoding="utf-8")

    with pytest.raises(RuntimeError, match="failed and was rolled back"):
        await update(hub.config, "fixture-package", health_timeout_seconds=0.5)

    assert old_marker.read_text(encoding="utf-8") == "old"
    assert extension_parent.read_text(encoding="utf-8") == "blocks extension directory"
    assert service_log.read_text(encoding="utf-8").splitlines() == [
        "--user stop agent-hub.service",
        "--user stop agent-hub.service",
        "--user start agent-hub.service",
    ]


@pytest.mark.anyio
async def test_rejects_invalid_update_configuration(
    hub: RunningHub,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="must be positive"):
        await update(hub.config, health_timeout_seconds=0)

    home = tmp_path / "missing-service"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr("agent_hub.service.sys.platform", "linux")
    with pytest.raises(RuntimeError, match="not installed as a user service"):
        await update(hub.config)


@pytest.mark.anyio
async def test_reports_staging_and_installation_errors(
    hub: RunningHub,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _, tool_environment, service_log = _prepare_update_environment(monkeypatch, tmp_path)

    with pytest.raises(RuntimeError, match="package validation failed"):
        await update(hub.config, "fail-package")
    assert not service_log.exists()

    shutil.rmtree(tool_environment)
    with pytest.raises(RuntimeError, match="not installed as a uv tool"):
        await update(hub.config, "fixture-package")


@pytest.mark.anyio
async def test_requires_uv(
    hub: RunningHub,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _prepare_update_environment(monkeypatch, tmp_path)
    (tmp_path / "bin" / "uv").unlink()
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))

    with pytest.raises(RuntimeError, match="require uv"):
        await update(hub.config)


@pytest.mark.anyio
async def test_rejects_concurrent_updates(
    hub: RunningHub,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _prepare_update_environment(monkeypatch, tmp_path)
    update_directory = hub.config.data_dir / "updates"
    update_directory.mkdir()
    with (update_directory / "update.lock").open("w", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(RuntimeError, match="already running"):
            await update(hub.config)


@pytest.mark.anyio
async def test_reports_rollback_health_failures(
    hub: RunningHub,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _, tool_environment, _ = _prepare_update_environment(monkeypatch, tmp_path)
    old_marker = tool_environment / "old-package"
    old_marker.write_text("old", encoding="utf-8")
    extension_path().parent.mkdir(parents=True)
    extension_path().write_text("old extension", encoding="utf-8")
    old_skill = skills_path() / "issue-triage" / "SKILL.md"
    old_skill.parent.mkdir(parents=True)
    old_skill.write_text("old skill", encoding="utf-8")
    config = hub.config.model_copy(update={"socket_path": tmp_path / "missing.sock"})

    with pytest.raises(RuntimeError, match="rollback failed: Agent Hub did not become healthy"):
        await update(config, "fixture-package", health_timeout_seconds=0.01)

    assert old_marker.read_text(encoding="utf-8") == "old"
    assert extension_path().read_text(encoding="utf-8") == "old extension"
    assert old_skill.read_text(encoding="utf-8") == "old skill"


def _prepare_update_environment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> tuple[Path, Path, Path]:
    home = tmp_path / "home"
    binaries = tmp_path / "bin"
    tool_directory = tmp_path / "tools"
    tool_environment = tool_directory / "agent-hub"
    for directory in (home, binaries, tool_environment / "bin"):
        directory.mkdir(parents=True)
    service_log = tmp_path / "service.log"
    systemctl = binaries / "systemctl"
    systemctl.write_text(f"#!/bin/sh\nprintf '%s\\n' \"$*\" >> {service_log}\n", encoding="utf-8")
    systemctl.chmod(systemctl.stat().st_mode | stat.S_IXUSR)
    asset_root = Path(__file__).parents[1] / "src" / "agent_hub" / "assets"
    uv = binaries / "uv"
    uv.write_text(
        """#!/usr/bin/env python3
import os
import stat
import sys
from pathlib import Path

arguments = sys.argv[1:]
tool_directory = Path(os.environ["UV_TOOL_DIR"])
if arguments[:2] == ["tool", "dir"]:
    print(tool_directory)
    raise SystemExit
if arguments[:2] == ["tool", "install"]:
    if arguments[-1] == "fail-package":
        print("package validation failed", file=sys.stderr)
        raise SystemExit(1)
    environment = tool_directory / "agent-hub"
    binary = environment / "bin"
    binary.mkdir(parents=True, exist_ok=True)
    executable = binary / "agent-hub"
    executable.write_text("#!/bin/sh\\nexit 0\\n", encoding="utf-8")
    executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
    python = binary / "python"
    python.write_text('#!/bin/sh\\necho "$FAKE_ASSET_ROOT"\\n', encoding="utf-8")
    python.chmod(python.stat().st_mode | stat.S_IXUSR)
    (binary / "activate").write_text(f"VIRTUAL_ENV={environment}\\n", encoding="utf-8")
    (binary / "agent-hub-link").symlink_to("agent-hub")
    raise SystemExit
if arguments[:2] == ["pip", "check"]:
    raise SystemExit
print("unsupported fake uv command", file=sys.stderr)
raise SystemExit(1)
""",
        encoding="utf-8",
    )
    uv.chmod(uv.stat().st_mode | stat.S_IXUSR)
    old_executable = tool_environment / "bin" / "agent-hub"
    old_executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    old_executable.chmod(old_executable.stat().st_mode | stat.S_IXUSR)
    (home / ".config" / "systemd" / "user").mkdir(parents=True)
    (home / ".config" / "systemd" / "user" / "agent-hub.service").write_text("service", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("UV_TOOL_DIR", str(tool_directory))
    monkeypatch.setenv("FAKE_ASSET_ROOT", str(asset_root))
    monkeypatch.setenv("PATH", f"{binaries}:{os.environ['PATH']}")
    monkeypatch.setattr("agent_hub.service.sys.platform", "linux")
    return home, tool_environment, service_log
