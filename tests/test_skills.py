from __future__ import annotations

from importlib.resources import files

from agent_hub.service import SKILL_NAMES


def test_bundled_skills_follow_the_agent_skills_format() -> None:
    root = files("agent_hub").joinpath("assets", "skills")

    for name in SKILL_NAMES:
        content = root.joinpath(name, "SKILL.md").read_text(encoding="utf-8")
        _, frontmatter, instructions = content.split("---", 2)
        metadata = dict(line.split(": ", 1) for line in frontmatter.strip().splitlines())

        assert metadata["name"] == name
        assert 1 <= len(metadata["description"]) <= 1024
        assert instructions.strip().startswith("# ")
