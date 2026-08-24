from __future__ import annotations

import anyio

from agent_hub.client import HubClient, HubClientError
from agent_hub.config import HubConfig


async def wait_for_health(config: HubConfig, timeout_seconds: float) -> None:
    if config.socket_path is None:  # pragma: no cover - HubConfig always configures the socket path
        raise RuntimeError("Agent Hub socket path is not configured")
    try:
        with anyio.fail_after(timeout_seconds):
            while True:
                try:
                    async with HubClient(config.socket_path) as client:
                        await client.health()
                    return
                except HubClientError, ValueError:
                    await anyio.sleep(0.1)
    except TimeoutError as exc:
        raise RuntimeError(f"Agent Hub did not become healthy within {timeout_seconds:g} seconds") from exc
