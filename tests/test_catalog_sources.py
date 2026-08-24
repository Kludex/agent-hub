from __future__ import annotations

import json
from pathlib import Path

import httpx2
import pytest
from starlette.applications import Starlette
from starlette.responses import Response
from starlette.routing import Route

from agent_hub.catalog import CatalogReader, CatalogSource, load_catalog_sources
from agent_hub.catalog_sources import (
    CatalogSourceError,
    CatalogSourceStore,
    execute_catalog_source_command,
)


@pytest.mark.anyio
async def test_catalog_sources_are_persisted_listed_and_removed(tmp_path: Path) -> None:
    builtin = CatalogSource(name="builtin", location=str(Path("registry/index.json").resolve()), builtin=True)
    store = CatalogSourceStore(tmp_path, builtin)
    empty_registry = tmp_path / "empty"
    empty_registry.mkdir()
    (empty_registry / "index.json").write_text('{"schema_version": 1, "agents": []}\n', encoding="utf-8")

    async with httpx2.AsyncClient() as client:
        reader = CatalogReader(client)
        added = await execute_catalog_source_command(store, reader, ("add", "empty", str(empty_registry)))
        listing = await execute_catalog_source_command(store, reader, ("list",))
        removed = await execute_catalog_source_command(store, reader, ("remove", "empty"))

    assert added.startswith("Added catalog source empty")
    assert "builtin (built-in)" in listing
    assert "empty -" in listing
    assert removed == "Removed catalog source empty"
    assert store.load() == (builtin,)


@pytest.mark.anyio
async def test_catalog_source_add_rejects_names_and_agent_conflicts(tmp_path: Path) -> None:
    builtin = CatalogSource(name="builtin", location=str(Path("registry/index.json").resolve()), builtin=True)
    store = CatalogSourceStore(tmp_path, builtin)
    conflicting = str(Path("registry/index.json").resolve())

    async with httpx2.AsyncClient() as client:
        reader = CatalogReader(client)
        with pytest.raises(CatalogSourceError, match="already exists"):
            await store.add(reader, "builtin", conflicting)
        with pytest.raises(CatalogSourceError, match="conflicts with builtin"):
            await store.add(reader, "conflict", conflicting)

    with pytest.raises(CatalogSourceError, match="cannot be removed"):
        store.remove("builtin")
    with pytest.raises(CatalogSourceError, match="not found"):
        store.remove("missing")


@pytest.mark.anyio
async def test_catalog_source_commands_validate_usage_and_http_locations(tmp_path: Path) -> None:
    local_index = tmp_path / "builtin.json"
    local_index.write_text('{"schema_version": 1, "agents": []}\n', encoding="utf-8")
    builtin = CatalogSource(name="builtin", location=str(local_index), builtin=True)
    store = CatalogSourceStore(tmp_path, builtin)

    async def index(_: object) -> Response:
        return Response(json.dumps({"schema_version": 1, "agents": []}), media_type="application/json")

    app = Starlette(routes=[Route("/index.json", index)])
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app)) as client:
        reader = CatalogReader(client)
        source = await store.add(reader, "remote", "http://catalog/")
        with pytest.raises(CatalogSourceError, match="Usage"):
            await execute_catalog_source_command(store, reader, ("add",))

    assert source.location == "http://catalog/index.json"


def test_default_source_store_loads_from_the_data_directory(tmp_path: Path) -> None:
    assert load_catalog_sources(tmp_path)[0].name == "agent-hub"
