from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from agent_hub.config import AgentProfile
from agent_hub.runtimes.base import StartAgentRequest
from agent_hub.runtimes.pi import PiRuntime


@pytest.mark.installed_pi
@pytest.mark.skipif(shutil.which("pi") is None, reason="Pi is not installed")
@pytest.mark.anyio
async def test_installed_pi_starts_and_stops_without_a_provider(
    tmp_path: Path,
) -> None:  # pragma: no cover - optional system integration
    runtime = PiRuntime(shutdown_grace_seconds=0.1, process_shutdown_seconds=1)
    await runtime.open()
    handle = await runtime.start(
        StartAgentRequest(
            agent_id="agt_installed",
            profile=AgentProfile(name="installed"),
            cwd=tmp_path,
            session_directory=tmp_path / "sessions",
        )
    )

    await runtime.stop(handle)
    await runtime.close()
