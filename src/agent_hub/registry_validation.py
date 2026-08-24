from __future__ import annotations

import argparse
import json
import re
import subprocess
import tomllib
from collections.abc import Sequence
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field

from agent_hub.catalog_models import find_bundle_paths, load_bundle


class RegistryValidationError(ValueError):
    """Raised when a registry policy check fails."""


class EvaluationResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1]
    name: str = Field(min_length=1)
    passed: bool
    summary: str = Field(min_length=1)


def validate_registry(registry: Path) -> None:
    for path in find_bundle_paths(registry / "agents"):
        for item in path.rglob("*"):
            if item.is_symlink():
                raise RegistryValidationError(f"Bundle must not contain symlinks: {item}")
            if item.is_file() and _contains_secret(item):
                raise RegistryValidationError(f"Bundle contains credential-like content: {item}")
        bundle = load_bundle(path)
        for server in bundle.manifest.mcp_servers:
            parsed = urlsplit(server)
            if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password or parsed.query:
                raise RegistryValidationError(
                    f"MCP server must be an HTTPS URL without credentials or a query: {server}"
                )
        for evaluation in sorted((path / "evaluations").glob("*")):
            result = _load_evaluation(evaluation)
            if not result.passed:
                raise RegistryValidationError(f"Evaluation did not pass: {evaluation}")


def validate_immutable_versions(repository: Path, base_ref: str) -> None:
    completed = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", base_ref, "--", "registry/agents"],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    existing_versions = {_version_prefix(path) for path in completed.stdout.splitlines()}
    changed = subprocess.run(
        [
            "git",
            "diff",
            "--name-status",
            "--find-renames=100%",
            f"{base_ref}...HEAD",
            "--",
            "registry/agents",
        ],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    changed_versions: set[str] = set()
    for line in changed.stdout.splitlines():
        status, *paths = line.split("\t")
        if status == "A":
            continue
        if status == "R100" and _is_bundle_relocation(paths[0], paths[1]):
            continue
        changed_versions.add(_version_prefix(paths[0]))
    modified = sorted(changed_versions & existing_versions)
    if modified:
        raise RegistryValidationError(
            f"Published versions are immutable; add a new version instead: {', '.join(modified)}"
        )


def main(arguments: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m agent_hub.registry_validation")
    parser.add_argument("registry", type=Path)
    parser.add_argument("--repository", type=Path)
    parser.add_argument("--base-ref")
    values = parser.parse_args(arguments)
    validate_registry(values.registry)
    if values.repository is not None and values.base_ref is not None:
        validate_immutable_versions(values.repository, values.base_ref)


def _contains_secret(path: Path) -> bool:
    if path.suffix not in {".json", ".md", ".toml"}:
        return False
    content = path.read_text(encoding="utf-8").lower()
    markers = ("-----begin private key-----", "-----begin rsa private key-----", "sk-ant-api03-")
    return any(marker in content for marker in markers)


def _load_evaluation(path: Path) -> EvaluationResult:
    if path.suffix == ".json":
        values = json.loads(path.read_text(encoding="utf-8"))
    else:
        values = tomllib.loads(path.read_text(encoding="utf-8"))
    return EvaluationResult.model_validate(values)


def _version_prefix(path: str) -> str:
    parts = Path(path).parts
    length = 5 if len(parts) > 4 and re.fullmatch(r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)", parts[4]) else 4
    return "/".join(parts[:length])


def _is_bundle_relocation(source: str, target: str) -> bool:
    source_root = _version_prefix(source)
    target_root = _version_prefix(target)
    source_parts = Path(source_root).parts
    target_parts = Path(target_root).parts
    if source_parts[:4] != target_parts[:4]:
        return False
    source_relative = Path(source).relative_to(source_root)
    target_relative = Path(target).relative_to(target_root)
    return source_relative == target_relative


if __name__ == "__main__":  # pragma: no cover - module entry point
    main()
