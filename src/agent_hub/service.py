from __future__ import annotations

import os
import plistlib
import shutil
import sys
from importlib.resources import files
from pathlib import Path

import anyio

from agent_hub.config import HubConfig

SERVICE_NAME = "dev.agent-hub"


async def install(config: HubConfig) -> None:
    _install_extension()
    if sys.platform == "darwin":
        await _install_launchd(config)
    elif sys.platform.startswith("linux"):
        await _install_systemd(config)
    else:
        raise RuntimeError(f"Agent Hub service installation is unsupported on {sys.platform}")


async def uninstall() -> None:
    if sys.platform == "darwin":
        target = f"gui/{os.getuid()}/{SERVICE_NAME}"
        await anyio.run_process(["launchctl", "bootout", target], check=False)
        _launchd_path().unlink(missing_ok=True)
    elif sys.platform.startswith("linux"):
        await anyio.run_process(["systemctl", "--user", "disable", "--now", "agent-hub.service"], check=False)
        _systemd_path().unlink(missing_ok=True)
        await _checked(["systemctl", "--user", "daemon-reload"])
    else:
        raise RuntimeError(f"Agent Hub service removal is unsupported on {sys.platform}")
    _extension_path().unlink(missing_ok=True)


def _install_extension() -> None:
    destination = _extension_path()
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    asset = files("agent_hub").joinpath("assets/agent-hub.js")
    with asset.open("rb") as source:
        with destination.open("wb") as target:
            shutil.copyfileobj(source, target)
    destination.chmod(0o600)


async def _install_launchd(config: HubConfig) -> None:
    path = _launchd_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    log_directory = config.data_dir / "log"
    log_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    payload = {
        "Label": SERVICE_NAME,
        "ProgramArguments": _service_arguments(config),
        "RunAtLoad": True,
        "KeepAlive": True,
        "ProcessType": "Background",
        "StandardOutPath": str(log_directory / "agent-hub.log"),
        "StandardErrorPath": str(log_directory / "agent-hub.error.log"),
    }
    path.write_bytes(plistlib.dumps(payload))
    path.chmod(0o600)
    domain = f"gui/{os.getuid()}"
    await anyio.run_process(["launchctl", "bootout", f"{domain}/{SERVICE_NAME}"], check=False)
    await _checked(["launchctl", "bootstrap", domain, str(path)])


async def _install_systemd(config: HubConfig) -> None:
    path = _systemd_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    command = " ".join(_systemd_escape(argument) for argument in _service_arguments(config))
    path.write_text(
        "\n".join(
            [
                "[Unit]",
                "Description=Agent Hub local agent manager",
                "",
                "[Service]",
                f"ExecStart={command}",
                "Restart=on-failure",
                "",
                "[Install]",
                "WantedBy=default.target",
                "",
            ]
        ),
        encoding="utf-8",
    )
    path.chmod(0o600)
    await _checked(["systemctl", "--user", "daemon-reload"])
    await _checked(["systemctl", "--user", "enable", "--now", "agent-hub.service"])


def _service_arguments(config: HubConfig) -> list[str]:
    executable = Path(sys.executable).parent / "agent-hub"
    arguments = [str(executable), "serve", "--data-dir", str(config.data_dir)]
    if config.socket_path != config.data_dir / "run" / "agent-hub.sock":
        if config.socket_path is None:  # pragma: no cover - Pydantic Settings always configures the socket
            raise RuntimeError("Socket path is not configured")
        arguments.extend(["--socket", str(config.socket_path)])
    arguments.extend(["--global-concurrency", str(config.global_concurrency)])
    arguments.extend(["--codepuppy-executable", config.codepuppy_executable])
    if config.allow_project_profiles:
        arguments.append("--allow-project-profiles")
    return arguments


async def _checked(command: list[str]) -> None:
    result = await anyio.run_process(command, check=False)
    if result.returncode != 0:
        message = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(message or f"Command failed: {' '.join(command)}")


def _extension_path() -> Path:
    return Path.home() / ".pi" / "agent" / "extensions" / "agent-hub.js"


def _launchd_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{SERVICE_NAME}.plist"


def _systemd_path() -> Path:
    return Path.home() / ".config" / "systemd" / "user" / "agent-hub.service"


def _systemd_escape(value: str) -> str:
    return "'" + value.replace("'", "'\\''") + "'"
