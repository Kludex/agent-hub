from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

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
    external_dependencies: tuple[str, ...] = ()
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
    external_dependencies: tuple[str, ...] = ()
    usage_limits: UsageLimitSettings = Field(default_factory=UsageLimitSettings)
    allow_delegation: bool = False

    @model_validator(mode="after")
    def validate_profile(self) -> Self:
        self.to_profile("")
        return self

    @property
    def identity(self) -> str:
        return f"{self.owner}/{self.name}"

    def to_profile(self, instructions: str) -> AgentProfile:
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
            usage_limits=self.usage_limits,
            allow_delegation=self.allow_delegation,
        )


@dataclass(frozen=True)
class AgentBundle:
    path: Path
    manifest: AgentManifest
    instructions: str
    readme: str


def load_bundle(path: Path) -> AgentBundle:
    required = {"agent.toml", "instructions.md", "README.md"}
    files = {item.relative_to(path).as_posix() for item in path.rglob("*") if item.is_file()}
    missing = required - files
    if missing:
        raise CatalogValidationError(f"Bundle is missing required files: {', '.join(sorted(missing))}")
    unexpected = {name for name in files - required if not _is_evaluation(name)}
    if unexpected:
        raise CatalogValidationError(f"Bundle contains unexpected files: {', '.join(sorted(unexpected))}")

    manifest = AgentManifest.model_validate(tomllib.loads((path / "agent.toml").read_text(encoding="utf-8")))
    expected = (manifest.owner, manifest.name, manifest.version)
    actual = (path.parent.parent.name, path.parent.name, path.name)
    if actual != expected:
        raise CatalogValidationError(f"Bundle path must end with {manifest.owner}/{manifest.name}/{manifest.version}")
    instructions = (path / "instructions.md").read_text(encoding="utf-8")
    readme = (path / "README.md").read_text(encoding="utf-8")
    if not instructions.strip() or not readme.strip():
        raise CatalogValidationError("Bundle instructions and README must not be empty")
    return AgentBundle(path=path, manifest=manifest, instructions=instructions, readme=readme)


def _is_evaluation(name: str) -> bool:
    path = Path(name)
    return len(path.parts) == 2 and path.parts[0] == "evaluations" and path.suffix in {".json", ".toml"}
