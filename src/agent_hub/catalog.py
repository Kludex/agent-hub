from __future__ import annotations

from pathlib import Path
from urllib.parse import urljoin

import httpx2
from pydantic import BaseModel, ConfigDict, Field

from agent_hub.catalog_models import CatalogIndex

BUILTIN_CATALOG_URL = "https://raw.githubusercontent.com/Kludex/agent-hub/main/registry/index.json"


class CatalogSource(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(pattern=r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$")
    location: str = Field(min_length=1)
    builtin: bool = False


class CatalogReader:
    def __init__(self, client: httpx2.AsyncClient) -> None:
        self._client = client

    async def index(self, source: CatalogSource) -> CatalogIndex:
        return CatalogIndex.model_validate_json(await self.read(source))

    async def read(self, source: CatalogSource, relative_path: str | None = None) -> bytes:
        if source.location.startswith(("https://", "http://")):
            location = source.location if relative_path is None else urljoin(source.location, relative_path)
            response = await self._client.get(location)
            response.raise_for_status()
            return response.content
        path = Path(source.location)
        if relative_path is not None:
            path = path.parent / relative_path
        return path.read_bytes()


def builtin_catalog_source() -> CatalogSource:
    return CatalogSource(name="agent-hub", location=BUILTIN_CATALOG_URL, builtin=True)


def load_catalog_sources(data_dir: Path | None = None) -> tuple[CatalogSource, ...]:
    if data_dir is None:
        return (builtin_catalog_source(),)
    from agent_hub.catalog_sources import CatalogSourceStore

    return CatalogSourceStore(data_dir).load()
