from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from importlib.resources import files
from importlib.resources.abc import Traversable
from pathlib import Path

SKILL_NAMES = (
    "api-compatibility-review",
    "docs-and-dx-editing",
    "issue-triage",
    "product-readiness-review",
    "release-readiness",
    "security-validation",
)


@dataclass(frozen=True)
class BundledAssets:
    extension: bytes
    skills: dict[str, bytes]


def load_bundled_assets(root: Traversable | None = None) -> BundledAssets:
    assets = root or files("agent_hub").joinpath("assets")
    extension = assets.joinpath("agent-hub.js").read_bytes()
    skills = {name: assets.joinpath("skills", name, "SKILL.md").read_bytes() for name in SKILL_NAMES}
    if not extension or any(not contents for contents in skills.values()):
        raise RuntimeError("The Agent Hub package contains an empty bundled asset")
    return BundledAssets(extension, skills)


def install_bundled_assets(assets: BundledAssets) -> None:
    _write_file(extension_path(), assets.extension)
    root = skills_path()
    for name, contents in assets.skills.items():
        _write_file(root / name / "SKILL.md", contents)


def extension_path() -> Path:
    return Path.home() / ".pi" / "agent" / "extensions" / "agent-hub.js"


def skills_path() -> Path:
    return Path.home() / ".agents" / "skills" / "agent-hub"


def _write_file(path: Path, contents: bytes) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(contents)
            output.flush()
            os.fsync(output.fileno())
        temporary_path.chmod(0o600)
        temporary_path.replace(path)
    finally:
        temporary_path.unlink(missing_ok=True)
