from __future__ import annotations

import os
import stat
import sys
import uuid
from pathlib import Path

import anyio
import httpx2
import pytest

from agent_hub.cli import bind_socket


def test_socket_binding_removes_stale_paths_and_rejects_live_daemons() -> None:
    directory = Path("/tmp") / f"ah-bind-{uuid.uuid4().hex}"
    directory.mkdir()
    socket_path = directory / "hub.sock"
    socket_path.write_text("stale", encoding="utf-8")
    listener = bind_socket(socket_path)
    try:
        with pytest.raises(RuntimeError, match="already listening"):
            bind_socket(socket_path)
    finally:
        listener.close()
        socket_path.unlink()
        directory.rmdir()


def test_socket_binding_closes_the_listener_after_an_os_error(tmp_path: Path) -> None:
    path = tmp_path / ("x" * 200)

    with pytest.raises(OSError):
        bind_socket(path)


@pytest.mark.anyio
async def test_cli_serves_on_a_private_socket_and_rejects_a_second_daemon(tmp_path: Path) -> None:
    socket_directory = Path("/tmp") / f"ah-cli-{uuid.uuid4().hex}"
    socket_path = socket_directory / "hub.sock"
    command = [
        sys.executable,
        "-m",
        "agent_hub.cli",
        "serve",
        "--data-dir",
        str(tmp_path),
        "--socket",
        str(socket_path),
    ]
    process = await anyio.open_process(command, env={**os.environ, "LOGFIRE_IGNORE_NO_CONFIG": "1"})
    client = httpx2.AsyncClient(
        transport=httpx2.AsyncHTTPTransport(uds=str(socket_path)),
        base_url="http://agent-hub",
    )
    try:
        for _ in range(100):
            try:
                response = await client.get("/health")
            except httpx2.ConnectError:
                await anyio.sleep(0.02)
                continue
            if response.status_code == 200:
                break
        else:  # pragma: no cover - reports subprocess infrastructure failure
            raise AssertionError("CLI daemon did not start")

        assert socket_path.stat().st_mode & 0o777 == 0o600
        assert socket_path.parent.stat().st_mode & 0o777 == 0o700
        second = await anyio.run_process(command, check=False)
        assert second.returncode != 0
    finally:
        await client.aclose()
        if process.returncode is None:
            process.terminate()
            await process.wait()

    assert not socket_path.exists()
    assert stat.S_ISDIR(socket_path.parent.stat().st_mode)
    socket_directory.rmdir()
