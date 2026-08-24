from __future__ import annotations

import argparse
import contextlib
import signal
import socket
import sys
from collections.abc import Generator, Sequence
from pathlib import Path

import anyio
import httpx2
import logfire
import uvicorn
import zuvloop

from agent_hub.app import create_app
from agent_hub.catalog import CatalogReader, load_catalog_sources
from agent_hub.catalog_commands import CatalogService, execute_catalog_command
from agent_hub.catalog_sources import CatalogSourceStore, execute_catalog_source_command
from agent_hub.config import HubConfig, load_profiles
from agent_hub.installed_agents import InstalledAgentService, execute_installed_agent_command
from agent_hub.mcp_bridge import serve_mcp
from agent_hub.service import install, uninstall
from agent_hub.update import DEFAULT_UPDATE_SOURCE, update


def main(arguments: Sequence[str] | None = None) -> None:  # pragma: no cover - exercised in a subprocess
    parser = argparse.ArgumentParser(prog="agent-hub")
    parser.add_argument(
        "command",
        nargs="?",
        choices=("serve", "install", "update", "uninstall", "mcp", "catalog", "agent"),
        default="serve",
    )
    parser.add_argument("operands", nargs="*")
    parser.add_argument("--data-dir", type=Path, default=Path.home() / ".agent-hub")
    parser.add_argument("--socket", type=Path)
    parser.add_argument("--global-concurrency", type=int, default=4)
    parser.add_argument("--codepuppy-executable", default="code-puppy")
    parser.add_argument("--allow-project-profiles", action="store_true")
    parser.add_argument("--version")
    parser.add_argument("--yes", action="store_true")
    parser.add_argument("--source", default=DEFAULT_UPDATE_SOURCE)
    parser.add_argument("--health-timeout", type=float, default=15)
    values = parser.parse_args(arguments)
    logfire.configure(send_to_logfire=False)
    config = HubConfig(
        data_dir=values.data_dir,
        socket_path=values.socket,
        global_concurrency=values.global_concurrency,
        codepuppy_executable=values.codepuppy_executable,
        allow_project_profiles=values.allow_project_profiles,
    )
    config.profiles = load_profiles(config, Path.cwd())
    backend_options = {"loop_factory": zuvloop.new_event_loop}
    if values.command == "install":
        anyio.run(install, config, backend_options=backend_options)
    elif values.command == "update":
        result = anyio.run(
            update,
            config,
            values.source,
            values.health_timeout,
            backend_options=backend_options,
        )
        backup = f" Database backup: {result.database_backup}." if result.database_backup is not None else ""
        sys.stdout.write(f"Agent Hub updated successfully.{backup}\n")
    elif values.command == "uninstall":
        anyio.run(uninstall, backend_options=backend_options)
    elif values.command == "mcp":
        if config.socket_path is None:  # pragma: no cover - Pydantic Settings always configures the socket
            raise RuntimeError("Socket path is not configured")
        anyio.run(serve_mcp, config.socket_path, Path.cwd(), backend_options=backend_options)
    elif values.command == "catalog":
        output = anyio.run(
            run_catalog,
            config,
            tuple(values.operands),
            values.version,
            backend_options=backend_options,
        )
        sys.stdout.write(f"{output}\n")
    elif values.command == "agent":
        output = anyio.run(
            run_installed_agents,
            config,
            tuple(values.operands),
            values.version,
            values.yes,
            backend_options=backend_options,
        )
        sys.stdout.write(f"{output}\n")
    else:
        anyio.run(serve, config, backend_options=backend_options)


async def run_installed_agents(
    config: HubConfig, arguments: tuple[str, ...], version: str | None, confirmed: bool
) -> str:
    async with httpx2.AsyncClient(follow_redirects=True) as client:
        catalog = CatalogService(CatalogReader(client), load_catalog_sources(config.data_dir), config.data_dir)
        service = InstalledAgentService(catalog, config.data_dir)
        return await execute_installed_agent_command(service, arguments, version, confirmed=confirmed)


async def run_catalog(config: HubConfig, arguments: tuple[str, ...], version: str | None) -> str:
    async with httpx2.AsyncClient(follow_redirects=True) as client:
        reader = CatalogReader(client)
        store = CatalogSourceStore(config.data_dir)
        if arguments[:1] == ("source",):
            return await execute_catalog_source_command(store, reader, arguments[1:])
        service = CatalogService(reader, load_catalog_sources(config.data_dir), config.data_dir)
        return await execute_catalog_command(service, arguments, version)


class AgentHubServer(uvicorn.Server):  # pragma: no cover - exercised in a subprocess
    @contextlib.contextmanager
    def capture_signals(self) -> Generator[None]:
        yield


async def serve(config: HubConfig) -> None:  # pragma: no cover - exercised in a subprocess
    socket_path = config.socket_path
    if socket_path is None:
        raise RuntimeError("Socket path is not configured")
    listener = bind_socket(socket_path)
    server = AgentHubServer(
        uvicorn.Config(create_app(config), http="zttp", log_config=None, access_log=False, lifespan="on")
    )
    logfire.info("Agent Hub listening on {socket_path}", socket_path=str(socket_path))
    try:
        with anyio.open_signal_receiver(signal.SIGINT, signal.SIGTERM) as signals:
            async with anyio.create_task_group() as task_group:
                task_group.start_soon(_serve_uvicorn, server, listener)
                await anext(signals)
                server.should_exit = True
    finally:
        listener.close()
        with contextlib.suppress(FileNotFoundError):
            socket_path.unlink()


async def _serve_uvicorn(  # pragma: no cover - exercised in a subprocess
    server: uvicorn.Server, listener: socket.socket
) -> None:
    await server.serve(sockets=[listener])


def bind_socket(path: Path) -> socket.socket:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    if path.exists():
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            probe.connect(str(path))
        except OSError:
            path.unlink()
        else:
            raise RuntimeError(f"Agent Hub is already listening on {path}")
        finally:
            probe.close()
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        listener.bind(str(path))
        path.chmod(0o600)
        listener.listen(128)
        listener.setblocking(False)
    except OSError:
        listener.close()
        raise
    return listener


if __name__ == "__main__":  # pragma: no cover - exercised in a subprocess
    main()
