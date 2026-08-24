from __future__ import annotations

import plistlib
import stat
from pathlib import Path

import pytest

from agent_hub.config import HubConfig
from agent_hub.service import install, uninstall


@pytest.mark.anyio
@pytest.mark.parametrize("platform", ["darwin", "linux"])
async def test_installs_and_removes_the_extension_and_user_service(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    platform: str,
) -> None:
    home = tmp_path / "home"
    binaries = tmp_path / "bin"
    home.mkdir()
    binaries.mkdir()
    for command_name in ("launchctl", "systemctl"):
        command = binaries / command_name
        command.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        command.chmod(command.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", f"{binaries}:{Path('/usr/bin')}:{Path('/bin')}")
    monkeypatch.setattr("agent_hub.service.sys.platform", platform)
    config = HubConfig(
        data_dir=home / "agent hub's data",
        socket_path=home / "custom.sock",
        allow_project_profiles=True,
    )

    await install(config)

    extension = home / ".pi" / "agent" / "extensions" / "agent-hub.js"
    assert extension.stat().st_mode & 0o777 == 0o600
    assert b"registerAgentHub" in extension.read_bytes()
    if platform == "darwin":
        service = home / "Library" / "LaunchAgents" / "dev.agent-hub.plist"
        payload = plistlib.loads(service.read_bytes())
        assert payload["KeepAlive"] is True
        assert payload["ProgramArguments"][1] == "serve"
        assert "--socket" in payload["ProgramArguments"]
        assert "--allow-project-profiles" in payload["ProgramArguments"]
    else:
        service = home / ".config" / "systemd" / "user" / "agent-hub.service"
        contents = service.read_text(encoding="utf-8")
        assert "Restart=on-failure" in contents
        assert "'\\''" in contents

    await uninstall()

    assert not extension.exists()
    assert not service.exists()


@pytest.mark.anyio
async def test_reports_service_manager_failures(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    home = tmp_path / "home"
    binaries = tmp_path / "bin"
    home.mkdir()
    binaries.mkdir()
    systemctl = binaries / "systemctl"
    systemctl.write_text("#!/bin/sh\necho unavailable >&2\nexit 1\n", encoding="utf-8")
    systemctl.chmod(systemctl.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", f"{binaries}:{Path('/usr/bin')}:{Path('/bin')}")
    monkeypatch.setattr("agent_hub.service.sys.platform", "linux")

    with pytest.raises(RuntimeError, match="unavailable"):
        await install(HubConfig(data_dir=home / "data"))


@pytest.mark.anyio
async def test_rejects_service_management_on_unsupported_platform(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr("agent_hub.service.sys.platform", "unsupported")
    config = HubConfig(data_dir=tmp_path / "data")

    with pytest.raises(RuntimeError, match="unsupported"):
        await install(config)
    with pytest.raises(RuntimeError, match="unsupported"):
        await uninstall()
