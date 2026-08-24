from __future__ import annotations

import shutil
from pathlib import Path

import httpx2
import pytest

from agent_hub.catalog import CatalogReader, CatalogSource
from agent_hub.catalog_commands import CatalogService
from agent_hub.installed_agents import (
    InstalledAgentError,
    InstalledAgentService,
    PermissionExpansionError,
    execute_installed_agent_command,
)
from agent_hub.registry_index import write_registry_index


@pytest.mark.anyio
async def test_installed_agents_are_pinned_updated_with_confirmation_and_removed(tmp_path: Path) -> None:
    registry = _registry_with_updates(tmp_path)
    data_dir = tmp_path / "data"
    source = CatalogSource(name="updates", location=str(registry / "index.json"))
    async with httpx2.AsyncClient() as client:
        catalog = CatalogService(CatalogReader(client), (source,), data_dir)
        service = InstalledAgentService(catalog, data_dir)
        assert await execute_installed_agent_command(service, ("list",)) == "No agents installed."

        await catalog.install("agent-hub/scout", "1.0.0")
        listing = await execute_installed_agent_command(service, ("list",))
        updated = await execute_installed_agent_command(service, ("update", "agent-hub/scout"), "1.1.0")
        current = await service.update("agent-hub/scout", "1.1.0")
        with pytest.raises(PermissionExpansionError, match="tools: write_file.*write access.*network access.*MCP"):
            await service.update("agent-hub/scout", "2.0.0")
        expanded = await execute_installed_agent_command(
            service, ("update", "agent-hub/scout"), "2.0.0", confirmed=True
        )
        removed = await execute_installed_agent_command(service, ("remove", "agent-hub/scout"))

    assert listing == "agent-hub/scout 1.0.0 - pi, read-only"
    assert updated == "Updated agent-hub/scout from 1.0.0 to 1.1.0"
    assert current == "agent-hub/scout is already at 1.1.0"
    assert expanded == "Updated agent-hub/scout from 1.1.0 to 2.0.0"
    assert removed == "Removed agent-hub/scout 2.0.0"
    assert not (data_dir / "agents" / "agent-hub").exists()


@pytest.mark.anyio
async def test_installed_agent_commands_report_missing_duplicates_and_usage(tmp_path: Path) -> None:
    registry = _registry_with_updates(tmp_path)
    data_dir = tmp_path / "data"
    source = CatalogSource(name="updates", location=str(registry / "index.json"))
    async with httpx2.AsyncClient() as client:
        catalog = CatalogService(CatalogReader(client), (source,), data_dir)
        service = InstalledAgentService(catalog, data_dir)
        with pytest.raises(InstalledAgentError, match="not found"):
            service.remove("agent-hub/scout")
        with pytest.raises(InstalledAgentError, match="Usage"):
            await execute_installed_agent_command(service, ("unknown",))

        await catalog.install("agent-hub/scout", "1.0.0")
        first = data_dir / "agents" / "agent-hub" / "scout" / "1.0.0"
        second = first.parent / "1.1.0"
        shutil.copytree(first, second)
        manifest = (second / "agent.toml").read_text(encoding="utf-8")
        (second / "agent.toml").write_text(manifest.replace('version = "1.0.0"', 'version = "1.1.0"'), encoding="utf-8")
        with pytest.raises(InstalledAgentError, match="multiple pinned versions"):
            service.remove("agent-hub/scout")


def _registry_with_updates(tmp_path: Path) -> Path:
    registry = tmp_path / "registry"
    source = Path("registry/agents/agent-hub/scout/1.0.0")
    for version in ("1.0.0", "1.1.0", "2.0.0"):
        target = registry / "agents" / "agent-hub" / "scout" / version
        shutil.copytree(source, target)
        manifest = (target / "agent.toml").read_text(encoding="utf-8")
        manifest = manifest.replace('version = "1.0.0"', f'version = "{version}"')
        if version == "2.0.0":
            manifest = manifest.replace('access = "read-only"', 'access = "shared-write"')
            manifest += 'network_access = true\ntools = ["write_file"]\nmcp_servers = ["https://mcp.example.com"]\n'
        (target / "agent.toml").write_text(manifest, encoding="utf-8")
    write_registry_index(registry)
    return registry
