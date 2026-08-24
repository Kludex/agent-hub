from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from agent_hub.catalog_models import CatalogValidationError
from agent_hub.registry_validation import (
    RegistryValidationError,
    main,
    validate_immutable_versions,
    validate_registry,
)


def test_curated_registry_passes_security_and_evaluation_checks() -> None:
    main(["registry"])


def test_registry_accepts_https_mcp_servers_and_json_evaluations(tmp_path: Path) -> None:
    registry, bundle = _copy_registry(tmp_path)
    manifest = (bundle / "agent.toml").read_text(encoding="utf-8")
    (bundle / "agent.toml").write_text(
        manifest.replace("\n[usage_limits]", '\nmcp_servers = ["https://mcp.example.com/server"]\n\n[usage_limits]'),
        encoding="utf-8",
    )
    evaluation = bundle / "evaluations" / "result.json"
    evaluation.write_text(
        json.dumps({"schema_version": 1, "name": "smoke", "passed": True, "summary": "Passed"}),
        encoding="utf-8",
    )

    validate_registry(registry)


@pytest.mark.parametrize(
    "server",
    [
        "http://mcp.example.com",
        "https://user:password@mcp.example.com",
        "https://mcp.example.com?token=secret",
        "not-a-url",
    ],
)
def test_registry_rejects_unsafe_mcp_servers(server: str, tmp_path: Path) -> None:
    registry, bundle = _copy_registry(tmp_path)
    manifest = (bundle / "agent.toml").read_text(encoding="utf-8")
    (bundle / "agent.toml").write_text(
        manifest.replace("\n[usage_limits]", f'\nmcp_servers = ["{server}"]\n\n[usage_limits]'), encoding="utf-8"
    )

    with pytest.raises(RegistryValidationError, match="MCP server"):
        validate_registry(registry)


def test_registry_rejects_symlinks_and_credentials(tmp_path: Path) -> None:
    registry, bundle = _copy_registry(tmp_path)
    link = bundle / "linked.md"
    link.symlink_to(bundle / "README.md")
    with pytest.raises(RegistryValidationError, match="symlinks"):
        validate_registry(registry)

    link.unlink()
    (bundle / "README.md").write_text("-----BEGIN PRIVATE KEY-----\n", encoding="utf-8")
    with pytest.raises(RegistryValidationError, match="credential-like"):
        validate_registry(registry)


def test_registry_rejects_failed_evaluations_and_unknown_files(tmp_path: Path) -> None:
    registry, bundle = _copy_registry(tmp_path)
    evaluation = bundle / "evaluations" / "schema.toml"
    evaluation.write_text('schema_version = 1\nname = "schema"\npassed = false\nsummary = "Failed"\n', encoding="utf-8")
    with pytest.raises(RegistryValidationError, match="Evaluation did not pass"):
        validate_registry(registry)

    evaluation.unlink()
    (bundle / "evaluations" / "unknown").write_text("content", encoding="utf-8")
    with pytest.raises(CatalogValidationError, match="unexpected files"):
        validate_registry(registry)


def test_immutable_versions_allow_relocation_but_reject_identity_changes(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    registry, bundle = _copy_registry(repository)
    _git(repository, "init")
    _git(repository, "config", "user.email", "catalog@example.com")
    _git(repository, "config", "user.name", "Catalog Test")
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", "baseline")
    baseline = _git(repository, "rev-parse", "HEAD").stdout.strip()

    target = bundle.parent
    temporary = registry / "bundle"
    bundle.rename(temporary)
    target.rmdir()
    temporary.rename(target)
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", "flatten bundle")

    validate_immutable_versions(repository, baseline)

    other = registry / "agents" / "other" / "implementation-planner"
    other.parent.mkdir(parents=True)
    target.rename(other)
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", "change identity")
    with pytest.raises(RegistryValidationError, match="Published versions are immutable"):
        validate_immutable_versions(repository, baseline)


def test_immutable_versions_reject_changes_but_allow_new_versions(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    registry, bundle = _copy_registry(repository)
    _git(repository, "init")
    _git(repository, "config", "user.email", "catalog@example.com")
    _git(repository, "config", "user.name", "Catalog Test")
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", "baseline")
    baseline = _git(repository, "rev-parse", "HEAD").stdout.strip()

    new_bundle = registry / "agents" / "agent-hub" / "implementation-planner" / "1.1.0"
    shutil.copytree(bundle, new_bundle)
    manifest = (new_bundle / "agent.toml").read_text(encoding="utf-8")
    (new_bundle / "agent.toml").write_text(manifest.replace('version = "1.0.0"', 'version = "1.1.0"'), encoding="utf-8")
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", "new version")
    main([str(registry), "--repository", str(repository), "--base-ref", baseline])

    (bundle / "README.md").write_text("Changed\n", encoding="utf-8")
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", "change old version")
    with pytest.raises(RegistryValidationError, match="Published versions are immutable"):
        validate_immutable_versions(repository, baseline)


def _copy_registry(tmp_path: Path) -> tuple[Path, Path]:
    registry = tmp_path / "registry"
    bundle = registry / "agents" / "agent-hub" / "implementation-planner" / "1.0.0"
    shutil.copytree(Path("registry/agents/agent-hub/implementation-planner"), bundle)
    ignored = registry / "agents" / "not" / "a"
    ignored.mkdir(parents=True)
    (ignored / "bundle").write_text("ignored", encoding="utf-8")
    return registry, bundle


def _git(repository: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *arguments], cwd=repository, check=True, capture_output=True, text=True)
