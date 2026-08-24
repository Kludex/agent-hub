# Agents and skills

Agents and skills solve different problems.

## Agent

An agent is an independent worker. Agent Hub gives it a profile with its own runtime, model context, tools, permissions, workspace access, and lifecycle.

Use an agent when the work needs one or more of these properties:

- Delegation with a separate result
- Background or concurrent execution
- An isolated workspace
- Persistent state across turns
- Permissions that differ from the caller

For example, `agent-hub/pr-shepherd` can inspect a pull request, run its test suite, wait for CI, and continue after the calling session disconnects.

## Skill

A skill is a reusable procedure loaded into the current agent. It contributes instructions, references, and optional helper files. It does not have its own process, model context, workspace, permissions, or lifecycle.

Use a skill when the current agent needs a reliable method for a specialized task. For example, `release-readiness` tells the current agent how to verify versions, changelogs, CI, and artifacts before a release.

Agent Hub installs its bundled skills under `~/.agents/skills/agent-hub/`. Pi discovers this shared Agent Skills location automatically. Restart Pi after `agent-hub install` to make newly installed skills available.

## Choose one

Ask whether the work must run independently from the current agent.

- If yes, create an agent profile.
- If no, create a skill.

Do not create an agent only to store a checklist or writing style. Do not use a skill for work that must continue in the background or use separate permissions.
