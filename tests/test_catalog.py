from __future__ import annotations

from pathlib import Path

import httpx2
import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from agent_hub.catalog import BUILTIN_CATALOG_URL, CatalogReader, CatalogSource, load_catalog_sources
from agent_hub.cli import run_catalog
from agent_hub.config import HubConfig


@pytest.mark.anyio
async def test_catalog_reader_loads_local_indexes_and_bundle_files(tmp_path: Path) -> None:
    index_path = tmp_path / "index.json"
    index_path.write_bytes(Path("registry/index.json").read_bytes())
    bundle = tmp_path / "agents" / "owner" / "agent" / "1.0.0"
    bundle.mkdir(parents=True)
    (bundle / "README.md").write_text("# Agent\n", encoding="utf-8")
    source = CatalogSource(name="local", location=str(index_path))

    async with httpx2.AsyncClient() as client:
        reader = CatalogReader(client)
        index = await reader.index(source)
        content = await reader.read(source, "agents/owner/agent/1.0.0/README.md")

    assert index.agents[0].identity == "agent-hub/reviewer"
    assert content == b"# Agent\n"


@pytest.mark.anyio
async def test_catalog_reader_loads_http_sources() -> None:
    async def endpoint(request: Request) -> Response:
        if request.url.path == "/index.json":
            return Response(Path("registry/index.json").read_bytes(), media_type="application/json")
        return Response(b"bundle")

    app = Starlette(routes=[Route("/{path:path}", endpoint)])
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://catalog") as client:
        reader = CatalogReader(client)
        source = CatalogSource(name="remote", location="http://catalog/index.json")

        index = await reader.index(source)
        content = await reader.read(source, "agents/agent-hub/scout/1.0.0/README.md")

    assert index.schema_version == 1
    assert content == b"bundle"


@pytest.mark.anyio
async def test_cli_catalog_uses_configured_data_directory(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    source = CatalogSource(name="local", location=str(Path("registry/index.json").resolve()))
    monkeypatch.setattr("agent_hub.cli.load_catalog_sources", lambda _data_dir: (source,))

    output = await run_catalog(HubConfig(data_dir=tmp_path), ("search", "review"), None)

    assert output.startswith("agent-hub/reviewer 1.0.0")
    assert "agent-hub (built-in)" in await run_catalog(HubConfig(data_dir=tmp_path), ("source", "list"), None)


def test_agent_hub_repository_is_the_builtin_catalog_source() -> None:
    assert load_catalog_sources() == (CatalogSource(name="agent-hub", location=BUILTIN_CATALOG_URL, builtin=True),)
