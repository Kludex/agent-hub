from __future__ import annotations

from pathlib import Path

import pytest

from agent_hub.config import AgentProfile, HubConfig, UsageLimitSettings, load_profiles


def test_hub_config_loads_environment_with_pydantic_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("AGENT_HUB_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AGENT_HUB_GLOBAL_CONCURRENCY", "7")

    config = HubConfig()

    assert config.data_dir == tmp_path
    assert config.socket_path == tmp_path / "run" / "agent-hub.sock"
    assert config.global_concurrency == 7
    assert set(config.profiles) == {"task", "scout", "reviewer"}


def test_installed_profiles_load_before_user_and_opted_in_project_overrides(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", lambda: home)
    bundle = tmp_path / "data" / "agents" / "owner" / "catalog-agent" / "1.0.0"
    bundle.mkdir(parents=True)
    (bundle / "agent.toml").write_text(
        'schema_version = 1\nowner = "owner"\nname = "catalog-agent"\nversion = "1.0.0"\n'
        'description = "Installed agent"\nruntime = "pydantic-ai"\naccess = "read-only"\n',
        encoding="utf-8",
    )
    (bundle / "instructions.md").write_text("Installed instructions.\n", encoding="utf-8")
    (bundle / "README.md").write_text("# Installed agent\n", encoding="utf-8")
    config = HubConfig(data_dir=tmp_path / "data", profiles={"task": AgentProfile(name="task")})

    installed = load_profiles(config)

    assert installed["owner/catalog-agent"].runtime == "pydantic-ai"
    assert installed["owner/catalog-agent"].instructions == "Installed instructions.\n"

    user_directory = home / ".config" / "agent-hub" / "agents"
    user_directory.mkdir(parents=True)
    (user_directory / "override.toml").write_text(
        'name = "owner/catalog-agent"\nruntime = "codepuppy"\naccess = "read-only"\n', encoding="utf-8"
    )
    project = tmp_path / "project"
    project_directory = project / ".agent-hub" / "agents"
    project_directory.mkdir(parents=True)
    (project_directory / "override.toml").write_text(
        'name = "owner/catalog-agent"\nruntime = "pi"\naccess = "read-only"\n', encoding="utf-8"
    )

    assert load_profiles(config, project)["owner/catalog-agent"].runtime == "codepuppy"
    enabled = config.model_copy(update={"allow_project_profiles": True})
    assert load_profiles(enabled, project)["owner/catalog-agent"].runtime == "pi"


def test_project_profile_requires_opt_in_and_overrides_user_profiles(tmp_path: Path) -> None:
    project = tmp_path / "project"
    profile_directory = project / ".agent-hub" / "agents"
    profile_directory.mkdir(parents=True)
    (profile_directory / "task.toml").write_text(
        'name = "task"\nruntime = "pydantic-ai"\nmodel = "test"\naccess = "read-only"\n',
        encoding="utf-8",
    )

    disabled = HubConfig(data_dir=tmp_path, profiles={"task": AgentProfile(name="task")})
    enabled = HubConfig(
        data_dir=tmp_path,
        allow_project_profiles=True,
        profiles={"task": AgentProfile(name="task")},
    )

    assert load_profiles(disabled, project)["task"].runtime == "pi"
    assert load_profiles(enabled, project)["task"].runtime == "pydantic-ai"


@pytest.mark.parametrize(
    "values",
    [
        {"name": "sticky", "keep_alive": True},
        {"name": "bad-idle", "idle_timeout_seconds": 0},
        {"name": "bad-runtime", "max_runtime_seconds": 0},
    ],
)
def test_profile_lifetime_limits_are_validated(values: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        AgentProfile.model_validate(values)


@pytest.mark.parametrize(
    "values",
    [
        {"global_concurrency": 0},
        {"recursion_limit": -1},
        {"subscriber_queue_size": 0},
        {"max_record_bytes": 0},
        {"max_output_bytes": 0},
        {"completed_event_retention": -1},
        {"shutdown_grace_seconds": 0},
        {"process_shutdown_seconds": 0},
        {"codepuppy_executable": ""},
    ],
)
def test_hub_limits_are_validated(values: dict[str, object], tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        HubConfig.model_validate({"data_dir": tmp_path, **values})


def test_usage_limits_must_be_positive() -> None:
    with pytest.raises(ValueError, match="positive"):
        UsageLimitSettings(total_tokens_limit=0)
