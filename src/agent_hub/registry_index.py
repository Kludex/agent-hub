from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from agent_hub.catalog_models import AgentBundle, load_bundle


class RegistryIndexError(ValueError):
    """Raised when the generated registry index is stale."""


def generate_registry_index(registry: Path) -> dict[str, Any]:
    grouped: dict[str, list[AgentBundle]] = {}
    for path in sorted((registry / "agents").glob("*/*/*")):
        if path.is_dir():
            bundle = load_bundle(path)
            grouped.setdefault(bundle.manifest.identity, []).append(bundle)

    agents: list[dict[str, Any]] = []
    for identity in sorted(grouped):
        bundles = sorted(grouped[identity], key=lambda item: _version_key(item.manifest.version), reverse=True)
        latest = bundles[0].manifest
        agents.append(
            {
                "owner": latest.owner,
                "name": latest.name,
                "description": latest.description,
                "keywords": list(latest.keywords),
                "runtime": latest.runtime,
                "access": latest.access,
                "tools": list(latest.tools),
                "mcp_servers": list(latest.mcp_servers),
                "external_dependencies": list(latest.external_dependencies),
                "latest_version": latest.version,
                "versions": [_index_version(registry, bundle) for bundle in bundles],
            }
        )
    return {"schema_version": 1, "agents": agents}


def write_registry_index(registry: Path, *, check: bool = False) -> None:
    path = registry / "index.json"
    content = json.dumps(generate_registry_index(registry), indent=2, sort_keys=True) + "\n"
    if check:
        if not path.is_file() or path.read_text(encoding="utf-8") != content:
            raise RegistryIndexError("registry/index.json is not up to date")
        return
    path.write_text(content, encoding="utf-8")


def main(arguments: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m agent_hub.registry_index")
    parser.add_argument("registry", type=Path)
    parser.add_argument("--check", action="store_true")
    values = parser.parse_args(arguments)
    write_registry_index(values.registry, check=values.check)


def _index_version(registry: Path, bundle: AgentBundle) -> dict[str, Any]:
    digests: dict[str, str] = {}
    digest = hashlib.sha256()
    for path in sorted(item for item in bundle.path.rglob("*") if item.is_file()):
        relative = path.relative_to(bundle.path).as_posix()
        content = path.read_bytes()
        digests[relative] = hashlib.sha256(content).hexdigest()
        digest.update(relative.encode())
        digest.update(b"\0")
        digest.update(content)
        digest.update(b"\0")
    return {
        "version": bundle.manifest.version,
        "bundle_path": bundle.path.relative_to(registry).as_posix(),
        "sha256": digest.hexdigest(),
        "files": digests,
    }


def _version_key(version: str) -> tuple[int, int, int]:
    major, minor, patch = version.split(".")
    return int(major), int(minor), int(patch)


if __name__ == "__main__":  # pragma: no cover - module entry point
    main()
