from __future__ import annotations

import json
import shutil
from pathlib import Path

import httpx2
import pytest

from agent_hub.catalog import CatalogReader, CatalogSource
from agent_hub.catalog_commands import CatalogError, CatalogService, execute_catalog_command, format_matches


@pytest.mark.anyio
async def test_catalog_commands_browse_search_show_and_install(tmp_path: Path) -> None:
    source = CatalogSource(name="local", location=str(Path("registry/index.json").resolve()))
    async with httpx2.AsyncClient() as client:
        service = CatalogService(CatalogReader(client), (source,), tmp_path)

        listing = await execute_catalog_command(service, ())
        search = await execute_catalog_command(service, ("search", "exploration"))
        missing_search = await execute_catalog_command(service, ("search", "does-not-exist"))
        details = await execute_catalog_command(service, ("show", "agent-hub/scout"), "1.0.0")
        installed = await execute_catalog_command(service, ("install", "agent-hub/scout"), None)

    assert "agent-hub/task 1.0.0" in listing
    assert search.startswith("agent-hub/scout 1.0.0")
    assert missing_search == "No agents found."
    assert "Access: read-only" in details
    assert "External dependencies: pi" in details
    assert "# Scout" in details
    target = tmp_path / "agents" / "agent-hub" / "scout" / "1.0.0"
    assert installed.endswith(f"Installed at {target}")
    assert (target / "agent.toml").is_file()


@pytest.mark.anyio
async def test_catalog_commands_report_resolution_errors(tmp_path: Path) -> None:
    source = CatalogSource(name="local", location=str(Path("registry/index.json").resolve()))
    async with httpx2.AsyncClient() as client:
        service = CatalogService(CatalogReader(client), (source,), tmp_path)
        with pytest.raises(CatalogError, match="Agent not found"):
            await service.show("other/missing")
        with pytest.raises(CatalogError, match="Version 2.0.0"):
            await service.show("agent-hub/scout", "2.0.0")
        await service.install("agent-hub/scout")
        with pytest.raises(CatalogError, match="already installed"):
            await service.install("agent-hub/scout")

        duplicate = CatalogService(
            CatalogReader(client), (source, source.model_copy(update={"name": "copy"})), tmp_path
        )
        with pytest.raises(CatalogError, match="multiple sources"):
            await duplicate.show("agent-hub/scout")

        with pytest.raises(CatalogError, match="Usage"):
            await execute_catalog_command(service, ("unknown",))


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("change", "message"),
    [
        ("file_digest", "File digest verification failed"),
        ("bundle_digest", "Digest verification failed"),
        ("unsafe_path", "Unsafe bundle path"),
    ],
)
async def test_catalog_install_verifies_paths_and_digests(change: str, message: str, tmp_path: Path) -> None:
    registry = tmp_path / "registry"
    shutil.copytree("registry", registry)
    index = json.loads((registry / "index.json").read_text(encoding="utf-8"))
    agent = next(item for item in index["agents"] if item["name"] == "scout")
    version = agent["versions"][0]
    if change == "file_digest":
        version["files"]["README.md"] = "0" * 64
    elif change == "bundle_digest":
        version["sha256"] = "0" * 64
    else:
        version["files"] = {"../README.md": "0" * 64}
    index_path = registry / "index.json"
    index_path.write_text(json.dumps(index), encoding="utf-8")
    source = CatalogSource(name="invalid", location=str(index_path))

    async with httpx2.AsyncClient() as client:
        service = CatalogService(CatalogReader(client), (source,), tmp_path / "data")
        with pytest.raises(CatalogError, match=message):
            await service.install("agent-hub/scout")


def test_catalog_match_formatter_handles_empty_results() -> None:
    assert format_matches(()) == "No agents found."
