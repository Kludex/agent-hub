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
    ],
)
def test_hub_limits_are_validated(values: dict[str, object], tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        HubConfig.model_validate({"data_dir": tmp_path, **values})


def test_usage_limits_must_be_positive() -> None:
    with pytest.raises(ValueError, match="positive"):
        UsageLimitSettings(total_tokens_limit=0)
