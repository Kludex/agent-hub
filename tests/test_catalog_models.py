from __future__ import annotations

from pathlib import Path

import pytest

from agent_hub.catalog_models import AgentManifest, CatalogValidationError, find_bundle_paths, load_bundle


def test_curated_registry_bundles_follow_the_catalog_schema() -> None:
    paths = find_bundle_paths(Path("registry/agents"))

    bundles = [load_bundle(path) for path in paths]

    assert [bundle.manifest.identity for bundle in bundles] == ["agent-hub/implementation-planner"]
    planner = bundles[0]
    assert planner.manifest.to_profile(planner.instructions).instructions == planner.instructions
    assert (
        planner.manifest.to_profile(planner.instructions, planner.path).agent_spec == planner.path / "agent-spec.yaml"
    )
    assert planner.agent_spec is not None
    assert planner.agent_spec.name == "implementation-planner"


def test_bundle_allows_evaluation_files(tmp_path: Path) -> None:
    path = _write_bundle(tmp_path)
    evaluations = path / "evaluations"
    evaluations.mkdir()
    (evaluations / "smoke.toml").write_text('name = "smoke"\n', encoding="utf-8")
    (evaluations / "result.json").write_text("{}\n", encoding="utf-8")

    assert load_bundle(path).manifest.version == "1.0.0"


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ("missing", "missing required files"),
        ("unexpected", "unexpected files"),
        ("nested", "unexpected files"),
        ("evaluation_suffix", "unexpected files"),
        ("path", "Bundle path must end"),
        ("empty", "must not be empty"),
    ],
)
def test_bundle_rejects_invalid_layout(change: str, message: str, tmp_path: Path) -> None:
    path = _write_bundle(tmp_path)
    if change == "missing":
        (path / "instructions.md").unlink()
    elif change == "unexpected":
        (path / "credentials.json").write_text("{}", encoding="utf-8")
    elif change == "nested":
        nested = path / "other"
        nested.mkdir()
        (nested / "file.toml").write_text("", encoding="utf-8")
    elif change == "evaluation_suffix":
        evaluations = path / "evaluations"
        evaluations.mkdir()
        (evaluations / "smoke.txt").write_text("", encoding="utf-8")
    elif change == "path":
        (path / "agent.toml").write_text(_manifest(owner="different"), encoding="utf-8")
    else:
        (path / "README.md").write_text("", encoding="utf-8")

    with pytest.raises(CatalogValidationError, match=message):
        load_bundle(path)


@pytest.mark.parametrize(
    ("filename", "content"), [("agent-spec.yaml", "model: test\n"), ("agent-spec.json", '{"model":"test"}')]
)
def test_bundle_loads_pydantic_ai_agent_specs(filename: str, content: str, tmp_path: Path) -> None:
    path = _write_bundle(tmp_path)
    manifest = (path / "agent.toml").read_text(encoding="utf-8")
    (path / "agent.toml").write_text(
        f'{manifest}runtime = "pydantic-ai"\nagent_spec = "{filename}"\n', encoding="utf-8"
    )
    (path / filename).write_text(content, encoding="utf-8")

    bundle = load_bundle(path)

    assert bundle.agent_spec is not None
    assert bundle.agent_spec.model == "test"


def test_bundle_requires_its_declared_agent_spec(tmp_path: Path) -> None:
    path = _write_bundle(tmp_path)
    manifest = (path / "agent.toml").read_text(encoding="utf-8")
    (path / "agent.toml").write_text(
        f'{manifest}runtime = "pydantic-ai"\nagent_spec = "agent-spec.yaml"\n', encoding="utf-8"
    )

    with pytest.raises(CatalogValidationError, match="missing AgentSpec"):
        load_bundle(path)


def test_catalog_agent_specs_cannot_bypass_managed_capabilities(tmp_path: Path) -> None:
    path = _write_bundle(tmp_path)
    manifest = (path / "agent.toml").read_text(encoding="utf-8")
    (path / "agent.toml").write_text(
        f'{manifest}runtime = "pydantic-ai"\nagent_spec = "agent-spec.yaml"\n', encoding="utf-8"
    )
    (path / "agent-spec.yaml").write_text("model: test\ncapabilities: [Thinking]\n", encoding="utf-8")

    with pytest.raises(CatalogValidationError, match="capabilities are not supported"):
        load_bundle(path)


def test_agent_spec_requires_the_pydantic_ai_runtime() -> None:
    with pytest.raises(ValueError, match="agent_spec requires"):
        AgentManifest.model_validate(
            {
                "schema_version": 1,
                "owner": "owner",
                "name": "agent",
                "version": "1.0.0",
                "description": "Description",
                "agent_spec": "agent-spec.yaml",
            }
        )


def test_manifest_uses_profile_lifetime_validation() -> None:
    with pytest.raises(ValueError, match="idle_timeout_seconds"):
        AgentManifest.model_validate(
            {
                "schema_version": 1,
                "owner": "owner",
                "name": "agent",
                "version": "1.0.0",
                "description": "Description",
                "keep_alive": True,
            }
        )


def _write_bundle(tmp_path: Path) -> Path:
    path = tmp_path / "agents" / "owner" / "agent" / "1.0.0"
    path.mkdir(parents=True)
    (path / "agent.toml").write_text(_manifest(), encoding="utf-8")
    (path / "instructions.md").write_text("Follow instructions.\n", encoding="utf-8")
    (path / "README.md").write_text("# Agent\n", encoding="utf-8")
    return path


def _manifest(owner: str = "owner") -> str:
    return f'schema_version = 1\nowner = "{owner}"\nname = "agent"\nversion = "1.0.0"\ndescription = "Description"\n'
