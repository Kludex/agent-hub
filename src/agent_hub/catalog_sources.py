from __future__ import annotations

import json
from pathlib import Path
from typing import Literal
from urllib.parse import urljoin

from pydantic import BaseModel, ConfigDict

from agent_hub.catalog import CatalogReader, CatalogSource, builtin_catalog_source


class CatalogSourceError(RuntimeError):
    """Raised when a catalog source operation cannot be completed."""


class _SourceConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1]
    sources: tuple[CatalogSource, ...]


class CatalogSourceStore:
    def __init__(self, data_dir: Path, builtin: CatalogSource | None = None) -> None:
        self._path = data_dir / "catalog" / "sources.json"
        self._builtin = builtin or builtin_catalog_source()

    def load(self) -> tuple[CatalogSource, ...]:
        if not self._path.is_file():
            return (self._builtin,)
        config = _SourceConfig.model_validate_json(self._path.read_bytes())
        return (self._builtin, *config.sources)

    async def add(self, reader: CatalogReader, name: str, location: str) -> CatalogSource:
        sources = self.load()
        if any(source.name == name for source in sources):
            raise CatalogSourceError(f"Catalog source already exists: {name}")
        source = CatalogSource(name=name, location=_normalize_location(location))
        candidate = await reader.index(source)
        identities = {agent.identity for agent in candidate.agents}
        for existing in sources:
            index = await reader.index(existing)
            overlap = sorted(identities & {agent.identity for agent in index.agents})
            if overlap:
                raise CatalogSourceError(f"Source {name} conflicts with {existing.name}: {', '.join(overlap)}")
        self._save((*sources[1:], source))
        return source

    def remove(self, name: str) -> CatalogSource:
        if name == self._builtin.name:
            raise CatalogSourceError("The built-in Agent Hub catalog source cannot be removed")
        sources = self.load()[1:]
        removed = next((source for source in sources if source.name == name), None)
        if removed is None:
            raise CatalogSourceError(f"Catalog source not found: {name}")
        self._save(tuple(source for source in sources if source.name != name))
        return removed

    def _save(self, sources: tuple[CatalogSource, ...]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        config = _SourceConfig(schema_version=1, sources=sources)
        temporary = self._path.with_suffix(".tmp")
        temporary.write_text(json.dumps(config.model_dump(), indent=2) + "\n", encoding="utf-8")
        temporary.replace(self._path)


async def execute_catalog_source_command(
    store: CatalogSourceStore, reader: CatalogReader, arguments: tuple[str, ...]
) -> str:
    if arguments == ("list",):
        return format_catalog_sources(store.load())
    if len(arguments) == 3 and arguments[0] == "add":
        source = await store.add(reader, arguments[1], arguments[2])
        return f"Added catalog source {source.name} - {source.location}"
    if len(arguments) == 2 and arguments[0] == "remove":
        source = store.remove(arguments[1])
        return f"Removed catalog source {source.name}"
    raise CatalogSourceError("Usage: agent-hub catalog source [add NAME LOCATION | list | remove NAME]")


def format_catalog_sources(sources: tuple[CatalogSource, ...]) -> str:
    return "\n".join(
        f"{source.name}{' (built-in)' if source.builtin else ''} - {source.location}" for source in sources
    )


def _normalize_location(location: str) -> str:
    if location.startswith(("https://", "http://")):
        return urljoin(location, "index.json") if location.endswith("/") else location
    path = Path(location).expanduser().resolve()
    return str(path / "index.json" if path.is_dir() else path)
