from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic_ai import AgentSpec

from agent_hub.config import AccessMode, AgentProfile, RuntimeName, UsageLimitSettings


class CatalogValidationError(ValueError):
    """Raised when an agent catalog bundle is invalid."""


class CatalogVersion(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    version: str = Field(pattern=r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
    bundle_path: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    files: dict[str, str]


class CatalogAgent(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    owner: str
    name: str
    description: str
    keywords: tuple[str, ...] = ()
    runtime: RuntimeName
    access: AccessMode
    tools: tuple[str, ...] = ()
    mcp_servers: tuple[str, ...] = ()
    network_access: bool = False
    external_dependencies: tuple[str, ...] = ()
    agent_spec: bool = False
    latest_version: str
    versions: tuple[CatalogVersion, ...]

    @property
    def identity(self) -> str:
        return f"{self.owner}/{self.name}"


class CatalogIndex(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1]
    agents: tuple[CatalogAgent, ...]


class AgentManifest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1]
    owner: str = Field(pattern=r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$")
    name: str = Field(pattern=r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$")
    version: str = Field(pattern=r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
    description: str = Field(min_length=1, max_length=500)
    keywords: tuple[str, ...] = ()
    runtime: RuntimeName = "pi"
    model: str | None = None
    allow_model_override: bool = False
    access: AccessMode = "shared-write"
    keep_alive: bool = False
    idle_timeout_seconds: float | None = None
    max_runtime_seconds: float = 900
    tools: tuple[str, ...] = ()
    mcp_servers: tuple[str, ...] = ()
    network_access: bool = False
    external_dependencies: tuple[str, ...] = ()
    agent_spec: Literal["agent-spec.yaml", "agent-spec.yml", "agent-spec.json"] | None = None
    usage_limits: UsageLimitSettings = Field(default_factory=UsageLimitSettings)
    allow_delegation: bool = False

    @model_validator(mode="after")
    def validate_profile(self) -> Self:
        self.to_profile("")
        return self

    @property
    def identity(self) -> str:
        return f"{self.owner}/{self.name}"

    def to_profile(self, instructions: str, bundle_path: Path | None = None) -> AgentProfile:
        agent_spec = None
        if self.agent_spec is not None:
            agent_spec = Path(self.agent_spec) if bundle_path is None else bundle_path / self.agent_spec
        return AgentProfile(
            name=self.identity,
            runtime=self.runtime,
            model=self.model,
            allow_model_override=self.allow_model_override,
            access=self.access,
            keep_alive=self.keep_alive,
            idle_timeout_seconds=self.idle_timeout_seconds,
            max_runtime_seconds=self.max_runtime_seconds,
            instructions=instructions or None,
            tools=self.tools,
            mcp_servers=self.mcp_servers,
            network_access=self.network_access,
            agent_spec=agent_spec,
            usage_limits=self.usage_limits,
            allow_delegation=self.allow_delegation,
        )


@dataclass(frozen=True)
class AgentBundle:
    path: Path
    manifest: AgentManifest
    instructions: str
    readme: str
    agent_spec: AgentSpec | None


def load_bundle(path: Path) -> AgentBundle:
    required = {"agent.toml", "instructions.md", "README.md"}
    files = {item.relative_to(path).as_posix() for item in path.rglob("*") if item.is_file()}
    missing = required - files
    if missing:
        raise CatalogValidationError(f"Bundle is missing required files: {', '.join(sorted(missing))}")
    manifest = AgentManifest.model_validate(tomllib.loads((path / "agent.toml").read_text(encoding="utf-8")))
    allowed = required | ({manifest.agent_spec} if manifest.agent_spec is not None else set())
    unexpected = {name for name in files - allowed if not _is_evaluation(name)}
    if unexpected:
        raise CatalogValidationError(f"Bundle contains unexpected files: {', '.join(sorted(unexpected))}")
    if manifest.agent_spec is not None and manifest.agent_spec not in files:
        raise CatalogValidationError(f"Bundle is missing AgentSpec file: {manifest.agent_spec}")

    expected = (manifest.owner, manifest.name, manifest.version)
    actual = (path.parent.parent.name, path.parent.name, path.name)
    if actual != expected:
        raise CatalogValidationError(f"Bundle path must end with {manifest.owner}/{manifest.name}/{manifest.version}")
    instructions = (path / "instructions.md").read_text(encoding="utf-8")
    readme = (path / "README.md").read_text(encoding="utf-8")
    if not instructions.strip() or not readme.strip():
        raise CatalogValidationError("Bundle instructions and README must not be empty")
    agent_spec = AgentSpec.from_file(path / manifest.agent_spec) if manifest.agent_spec is not None else None
    if agent_spec is not None and agent_spec.capabilities:
        raise CatalogValidationError(
            "Catalog AgentSpec capabilities are not supported; declare tools and MCP servers in agent.toml"
        )
    return AgentBundle(
        path=path,
        manifest=manifest,
        instructions=instructions,
        readme=readme,
        agent_spec=agent_spec,
    )


def _is_evaluation(name: str) -> bool:
    path = Path(name)
    return len(path.parts) == 2 and path.parts[0] == "evaluations" and path.suffix in {".json", ".toml"}
