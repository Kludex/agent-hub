from __future__ import annotations

from pathlib import Path

import pytest

from agent_hub.catalog_models import AgentManifest, CatalogValidationError, load_bundle


def test_curated_registry_bundles_follow_the_catalog_schema() -> None:
    paths = sorted(Path("registry/agents").glob("*/*/*"))

    bundles = [load_bundle(path) for path in paths]

    assert [bundle.manifest.identity for bundle in bundles] == [
        "agent-hub/reviewer",
        "agent-hub/scout",
        "agent-hub/task",
    ]
    assert bundles[0].manifest.to_profile(bundles[0].instructions).instructions == bundles[0].instructions


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
