from __future__ import annotations

import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from agent_hub.catalog_commands import CatalogService
from agent_hub.catalog_models import AgentBundle, AgentManifest, load_bundle


class InstalledAgentError(RuntimeError):
    """Raised when an installed agent operation cannot be completed."""


class PermissionExpansionError(InstalledAgentError):
    """Raised when an update needs explicit permission confirmation."""


@dataclass(frozen=True)
class InstalledAgent:
    bundle: AgentBundle

    @property
    def identity(self) -> str:
        return self.bundle.manifest.identity

    @property
    def version(self) -> str:
        return self.bundle.manifest.version


class InstalledAgentService:
    def __init__(self, catalog: CatalogService, data_dir: Path) -> None:
        self._catalog = catalog
        self._data_dir = data_dir

    def list(self) -> tuple[InstalledAgent, ...]:
        paths = sorted((self._data_dir / "agents").glob("*/*/*"))
        return tuple(InstalledAgent(load_bundle(path)) for path in paths if path.is_dir())

    async def update(
        self, identity: str, version: str | None = None, *, allow_permission_expansion: bool = False
    ) -> str:
        installed = self._find(identity)
        staging_root = self._data_dir / "catalog" / "staging"
        staging_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=staging_root) as temporary:
            _, selected, bundle = await self._catalog.download(identity, Path(temporary), version)
            if selected.version == installed.version:
                return f"{identity} is already at {installed.version}"
            expansions = _permission_expansions(installed.bundle.manifest, bundle.manifest)
            if expansions and not allow_permission_expansion:
                raise PermissionExpansionError(
                    f"Update expands permissions ({', '.join(expansions)}); rerun with --yes to confirm"
                )
            shutil.rmtree(installed.bundle.path)
            target = installed.bundle.path.parent / selected.version
            shutil.move(str(bundle.path), target)
        return f"Updated {identity} from {installed.version} to {selected.version}"

    def remove(self, identity: str) -> str:
        installed = self._find(identity)
        identity_path = installed.bundle.path.parent
        shutil.rmtree(identity_path)
        owner_path = identity_path.parent
        if not any(owner_path.iterdir()):
            owner_path.rmdir()
        return f"Removed {identity} {installed.version}"

    def _find(self, identity: str) -> InstalledAgent:
        matches = [agent for agent in self.list() if agent.identity == identity]
        if not matches:
            raise InstalledAgentError(f"Installed agent not found: {identity}")
        if len(matches) > 1:
            raise InstalledAgentError(f"Installed agent has multiple pinned versions: {identity}")
        return matches[0]


async def execute_installed_agent_command(
    service: InstalledAgentService,
    arguments: tuple[str, ...],
    version: str | None = None,
    *,
    confirmed: bool = False,
) -> str:
    if arguments == ("list",):
        agents = service.list()
        if not agents:
            return "No agents installed."
        return "\n".join(
            f"{agent.identity} {agent.version} - {agent.bundle.manifest.runtime}, {agent.bundle.manifest.access}"
            for agent in agents
        )
    if len(arguments) == 2 and arguments[0] == "update":
        return await service.update(arguments[1], version, allow_permission_expansion=confirmed)
    if len(arguments) == 2 and arguments[0] == "remove":
        return service.remove(arguments[1])
    raise InstalledAgentError("Usage: agent-hub agent [list | update OWNER/NAME | remove OWNER/NAME]")


def _permission_expansions(current: AgentManifest, updated: AgentManifest) -> tuple[str, ...]:
    expansions: list[str] = []
    added_tools = sorted(set(updated.tools) - set(current.tools))
    if added_tools:
        expansions.append(f"tools: {', '.join(added_tools)}")
    if current.access == "read-only" and updated.access == "shared-write":
        expansions.append("write access")
    if not current.network_access and updated.network_access:
        expansions.append("network access")
    added_mcp = sorted(set(updated.mcp_servers) - set(current.mcp_servers))
    if added_mcp:
        expansions.append(f"MCP servers: {', '.join(added_mcp)}")
    return tuple(expansions)
