from __future__ import annotations

from pathlib import Path

import pytest

from agent_hub.assets import SKILL_NAMES, load_bundled_assets


def test_rejects_empty_bundled_assets(tmp_path: Path) -> None:
    (tmp_path / "agent-hub.js").touch()
    for name in SKILL_NAMES:
        skill = tmp_path / "skills" / name / "SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text("skill", encoding="utf-8")

    with pytest.raises(RuntimeError, match="empty bundled asset"):
        load_bundled_assets(tmp_path)
