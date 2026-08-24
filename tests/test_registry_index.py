from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from agent_hub.registry_index import RegistryIndexError, generate_registry_index, main, write_registry_index


def test_registry_index_contains_search_metadata_versions_and_digests(tmp_path: Path) -> None:
    registry = tmp_path / "registry"
    source = Path("registry/agents/agent-hub/implementation-planner/1.0.0")
    first = registry / "agents" / "agent-hub" / "implementation-planner" / "1.0.0"
    second = registry / "agents" / "agent-hub" / "implementation-planner" / "1.1.0"
    shutil.copytree(source, first)
    shutil.copytree(source, second)
    manifest = (second / "agent.toml").read_text(encoding="utf-8")
    (second / "agent.toml").write_text(manifest.replace('version = "1.0.0"', 'version = "1.1.0"'), encoding="utf-8")
    ignored = registry / "agents" / "not" / "a"
    ignored.mkdir(parents=True)
    (ignored / "bundle").write_text("not a directory", encoding="utf-8")

    index = generate_registry_index(registry)

    agent = index["agents"][0]
    assert agent == {
        "owner": "agent-hub",
        "name": "implementation-planner",
        "description": (
            "Explores a repository and produces a grounded, execution-ready implementation plan "
            "without modifying files."
        ),
        "keywords": ["planning", "architecture", "implementation", "read-only", "pydantic-ai", "agent-spec"],
        "runtime": "pydantic-ai",
        "access": "read-only",
        "tools": ["read_file", "list_files"],
        "mcp_servers": [],
        "network_access": False,
        "external_dependencies": ["an Anthropic API key"],
        "agent_spec": True,
        "latest_version": "1.1.0",
        "versions": agent["versions"],
    }
    assert [version["version"] for version in agent["versions"]] == ["1.1.0", "1.0.0"]
    assert agent["versions"][0]["bundle_path"] == "agents/agent-hub/implementation-planner/1.1.0"
    assert len(agent["versions"][0]["sha256"]) == 64
    assert set(agent["versions"][0]["files"]) == {
        "README.md",
        "agent-spec.yaml",
        "agent.toml",
        "evaluations/schema.toml",
        "instructions.md",
    }


def test_registry_index_write_and_check(tmp_path: Path) -> None:
    registry = tmp_path / "registry"
    (registry / "agents").mkdir(parents=True)

    main([str(registry)])
    main([str(registry), "--check"])

    assert json.loads((registry / "index.json").read_text(encoding="utf-8")) == {
        "agents": [],
        "schema_version": 1,
    }
    (registry / "index.json").write_text("{}\n", encoding="utf-8")
    with pytest.raises(RegistryIndexError, match="not up to date"):
        write_registry_index(registry, check=True)


def test_committed_registry_index_is_current() -> None:
    write_registry_index(Path("registry"), check=True)
