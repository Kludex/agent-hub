from __future__ import annotations

import tomllib
from decimal import Decimal
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

AccessMode = Literal["read-only", "shared-write"]
RuntimeName = Literal["codepuppy", "pi", "pydantic-ai"]


class UsageLimitSettings(BaseModel):
    model_config = ConfigDict(frozen=True)

    cost_limit: Decimal | None = None
    request_limit: int | None = 50
    tool_calls_limit: int | None = None
    input_tokens_limit: int | None = None
    output_tokens_limit: int | None = None
    total_tokens_limit: int | None = None
    per_request_input_tokens_limit: int | None = None
    count_tokens_before_request: bool = False

    @model_validator(mode="after")
    def validate_limits(self) -> Self:
        for name, value in self:
            if name != "count_tokens_before_request" and value is not None and value <= 0:
                raise ValueError(f"{name} must be positive")
        return self


class AgentProfile(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    runtime: RuntimeName = "pi"
    model: str | None = None
    allow_model_override: bool = False
    access: AccessMode = "shared-write"
    keep_alive: bool = False
    idle_timeout_seconds: float | None = None
    max_runtime_seconds: float = 900
    instructions: str | None = None
    tools: tuple[str, ...] = ()
    mcp_servers: tuple[str, ...] = ()
    usage_limits: UsageLimitSettings = Field(default_factory=UsageLimitSettings)
    allow_delegation: bool = False

    @model_validator(mode="after")
    def validate_lifetime(self) -> Self:
        if self.keep_alive and self.idle_timeout_seconds is None:
            raise ValueError(f"Keep-alive profile {self.name!r} must define idle_timeout_seconds")
        if self.idle_timeout_seconds is not None and self.idle_timeout_seconds <= 0:
            raise ValueError("idle_timeout_seconds must be positive")
        if self.max_runtime_seconds <= 0:
            raise ValueError("max_runtime_seconds must be positive")
        return self


class HubConfig(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="AGENT_HUB_",
        env_nested_delimiter="__",
        extra="ignore",
    )

    data_dir: Path = Field(default_factory=lambda: Path.home() / ".agent-hub")
    socket_path: Path | None = None
    database_path: Path | None = None
    global_concurrency: int = 4
    recursion_limit: int = 3
    subscriber_queue_size: int = 256
    max_record_bytes: int = 1024 * 1024
    max_output_bytes: int = 50 * 1024
    completed_event_retention: int = 10_000
    shutdown_grace_seconds: float = 2
    process_shutdown_seconds: float = 5
    codepuppy_executable: str = "code-puppy"
    allow_project_profiles: bool = False
    profiles: dict[str, AgentProfile] = Field(default_factory=dict)

    @model_validator(mode="after")
    def configure_paths_and_profiles(self) -> Self:
        if self.socket_path is None:
            self.socket_path = self.data_dir / "run" / "agent-hub.sock"
        if self.database_path is None:
            self.database_path = self.data_dir / "agent-hub.sqlite3"
        if self.global_concurrency < 1:
            raise ValueError("global_concurrency must be positive")
        if self.recursion_limit < 0:
            raise ValueError("recursion_limit must not be negative")
        if self.subscriber_queue_size < 1:
            raise ValueError("subscriber_queue_size must be positive")
        if self.max_record_bytes < 1 or self.max_output_bytes < 1 or self.completed_event_retention < 0:
            raise ValueError("record and output limits must be positive; retention must not be negative")
        if self.shutdown_grace_seconds <= 0 or self.process_shutdown_seconds <= 0:
            raise ValueError("shutdown timeouts must be positive")
        if not self.codepuppy_executable:
            raise ValueError("codepuppy_executable must not be empty")
        if not self.profiles:
            self.profiles = bundled_profiles()
        return self


def bundled_profiles() -> dict[str, AgentProfile]:
    return {
        "task": AgentProfile(name="task", allow_model_override=True),
        "scout": AgentProfile(name="scout", access="read-only", allow_model_override=True),
        "reviewer": AgentProfile(name="reviewer", access="read-only", allow_model_override=True),
    }


def load_profiles(config: HubConfig, cwd: Path | None = None) -> dict[str, AgentProfile]:
    profiles = {**config.profiles}
    directories = [Path.home() / ".config" / "agent-hub" / "agents"]
    if cwd is not None and config.allow_project_profiles:
        directories.append(cwd / ".agent-hub" / "agents")
    for directory in directories:
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.toml")):
            profile = parse_profile(path)
            profiles[profile.name] = profile
    return profiles


def parse_profile(path: Path) -> AgentProfile:
    values = tomllib.loads(path.read_text(encoding="utf-8"))
    return AgentProfile.model_validate(values)
