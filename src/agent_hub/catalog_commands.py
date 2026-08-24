from __future__ import annotations

import hashlib
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from agent_hub.catalog import CatalogReader, CatalogSource
from agent_hub.catalog_models import AgentBundle, CatalogAgent, CatalogVersion, load_bundle
from agent_hub.registry_index import calculate_bundle_digests


class CatalogError(RuntimeError):
    """Raised when a catalog operation cannot be completed."""


@dataclass(frozen=True)
class CatalogMatch:
    source: CatalogSource
    agent: CatalogAgent


class CatalogService:
    def __init__(self, reader: CatalogReader, sources: tuple[CatalogSource, ...], data_dir: Path) -> None:
        self._reader = reader
        self._sources = sources
        self._data_dir = data_dir

    async def browse(self, query: str | None = None) -> tuple[CatalogMatch, ...]:
        matches: list[CatalogMatch] = []
        search = query.casefold() if query is not None else None
        for source in self._sources:
            index = await self._reader.index(source)
            for agent in index.agents:
                values = (agent.identity, agent.description, *agent.keywords, agent.runtime, agent.access)
                if search is None or any(search in value.casefold() for value in values):
                    matches.append(CatalogMatch(source=source, agent=agent))
        return tuple(sorted(matches, key=lambda match: match.agent.identity))

    async def show(self, identity: str, version: str | None = None) -> str:
        match = await self._find(identity)
        selected = self._version(match.agent, version)
        readme = await self._reader.read(match.source, f"{selected.bundle_path}/README.md")
        return f"{_describe(match.agent, selected)}\n\n{readme.decode().strip()}"

    async def install(self, identity: str, version: str | None = None) -> str:
        staging_root = self._data_dir / "catalog" / "staging"
        staging_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=staging_root) as temporary:
            match, selected, bundle = await self.download(identity, Path(temporary), version)
            identity_path = self._data_dir / "agents" / match.agent.owner / match.agent.name
            if identity_path.is_dir() and any(identity_path.iterdir()):
                raise CatalogError(f"{identity} is already installed")
            target = identity_path / selected.version
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(bundle.path), target)
        return f"{_describe(match.agent, selected)}\nInstalled at {target}"

    async def download(
        self, identity: str, destination: Path, version: str | None = None
    ) -> tuple[CatalogMatch, CatalogVersion, AgentBundle]:
        match = await self._find(identity)
        selected = self._version(match.agent, version)
        bundle_path = destination / match.agent.owner / match.agent.name / selected.version
        bundle_path.mkdir(parents=True)
        await self._download(match.source, selected, bundle_path)
        bundle = load_bundle(bundle_path)
        digest, files = calculate_bundle_digests(bundle_path)
        if digest != selected.sha256 or files != selected.files:
            raise CatalogError(f"Digest verification failed for {identity} {selected.version}")
        return match, selected, bundle

    async def _find(self, identity: str) -> CatalogMatch:
        matches = [match for match in await self.browse() if match.agent.identity == identity]
        if not matches:
            raise CatalogError(f"Agent not found: {identity}")
        if len(matches) > 1:
            sources = ", ".join(match.source.name for match in matches)
            raise CatalogError(f"Agent {identity} is published by multiple sources: {sources}")
        return matches[0]

    async def _download(self, source: CatalogSource, version: CatalogVersion, target: Path) -> None:
        for name, expected in version.files.items():
            relative = PurePosixPath(name)
            if relative.is_absolute() or ".." in relative.parts:
                raise CatalogError(f"Unsafe bundle path: {name}")
            content = await self._reader.read(source, f"{version.bundle_path}/{name}")
            if hashlib.sha256(content).hexdigest() != expected:
                raise CatalogError(f"File digest verification failed: {name}")
            path = target.joinpath(*relative.parts)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)

    @staticmethod
    def _version(agent: CatalogAgent, requested: str | None) -> CatalogVersion:
        version = requested or agent.latest_version
        for available in agent.versions:
            if available.version == version:
                return available
        raise CatalogError(f"Version {version} was not found for {agent.identity}")


async def execute_catalog_command(
    service: CatalogService, arguments: tuple[str, ...], version: str | None = None
) -> str:
    if not arguments:
        return format_matches(await service.browse())
    action, *operands = arguments
    if action == "search" and len(operands) == 1:
        return format_matches(await service.browse(operands[0]))
    if action == "show" and len(operands) == 1:
        return await service.show(operands[0], version)
    if action == "install" and len(operands) == 1:
        return await service.install(operands[0], version)
    raise CatalogError("Usage: agent-hub catalog [search QUERY | show OWNER/NAME | install OWNER/NAME]")


def format_matches(matches: tuple[CatalogMatch, ...]) -> str:
    if not matches:
        return "No agents found."
    return "\n".join(
        f"{match.agent.identity} {match.agent.latest_version} - {match.agent.description}" for match in matches
    )


def _describe(agent: CatalogAgent, version: CatalogVersion) -> str:
    tools = ", ".join(agent.tools) or "none"
    mcp_servers = ", ".join(agent.mcp_servers) or "none"
    dependencies = ", ".join(agent.external_dependencies) or "none"
    return (
        f"{agent.identity} {version.version}\n"
        f"Runtime: {agent.runtime}\n"
        f"Access: {agent.access}\n"
        f"Network access: {'allowed' if agent.network_access else 'denied'}\n"
        f"Tools: {tools}\n"
        f"MCP servers: {mcp_servers}\n"
        f"External dependencies: {dependencies}"
    )
