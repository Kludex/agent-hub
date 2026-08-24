# Agent catalog

The catalog publishes versioned agent profiles. Agent Hub includes the registry in this repository as its built-in source.

## Browse and install agents

```bash
agent-hub catalog
agent-hub catalog search planning
agent-hub catalog show agent-hub/implementation-planner
agent-hub catalog install agent-hub/implementation-planner
agent-hub agent list
```

An agent identity has the form `<owner>/<name>`. The identity stays the same across catalog sources. Agent Hub rejects a source when it publishes an identity provided by an existing source.

`catalog show` and `catalog install` display the runtime, workspace access, network access, tools, MCP servers, and external dependencies. Installation downloads only the files listed in the registry index. It verifies every file digest and the complete bundle digest before moving the bundle into the local agent directory. It does not copy credentials or session history.

Installed agents are pinned to the selected version. Pass `--version` to select a different published version:

```bash
agent-hub catalog show agent-hub/implementation-planner --version 1.0.0
agent-hub catalog install agent-hub/implementation-planner --version 1.0.0
```

## Update and remove agents

```bash
agent-hub agent update agent-hub/implementation-planner
agent-hub agent remove agent-hub/implementation-planner
```

An update does not need confirmation when its permissions stay the same or become narrower. Agent Hub stops an update that adds tools, write access, network access, or MCP servers. Inspect the reported expansion, then confirm it explicitly:

```bash
agent-hub agent update agent-hub/implementation-planner --yes
```

User profiles under `~/.config/agent-hub/agents/` override installed profiles with the same `<owner>/<name>`. Project profiles can also override them when you install the service with `--allow-project-profiles`.

## Configure catalog sources

```bash
agent-hub catalog source add team https://catalog.example.com/registry/index.json
agent-hub catalog source list
agent-hub catalog source remove team
```

A source can be an HTTPS index URL or a local `index.json` path. You can pass a local registry directory instead of its index path. The built-in `agent-hub` source cannot be removed.

## Create an agent bundle

```bash
mkdir -p registry/agents/example/reviewer/1.0.0
cat > registry/agents/example/reviewer/1.0.0/agent.toml <<'EOF'
schema_version = 1
owner = "example"
name = "reviewer"
version = "1.0.0"
description = "Reviews changes without modifying the workspace."
keywords = ["review", "read-only"]
runtime = "pi"
access = "read-only"
network_access = false
keep_alive = false
max_runtime_seconds = 900
external_dependencies = ["pi"]
EOF
cat > registry/agents/example/reviewer/1.0.0/instructions.md <<'EOF'
Review the requested changes. Report concrete defects and missing tests. Do not edit files.
EOF
cat > registry/agents/example/reviewer/1.0.0/README.md <<'EOF'
# Reviewer

Use this agent for read-only code review. It requires Pi and a configured model provider.
EOF
```

Every bundle requires `agent.toml`, `instructions.md`, and `README.md`. Evaluation results are optional. Put them under `evaluations/` as TOML or JSON:

```toml
schema_version = 1
name = "profile-schema"
passed = true
summary = "The profile passes the registry checks."
```

The manifest supports the profile fields `model`, `allow_model_override`, `keep_alive`, `idle_timeout_seconds`, `tools`, `mcp_servers`, `usage_limits`, and `allow_delegation`. Declare commands, services, and provider requirements in `external_dependencies` so users see them before installation.

## Publish a Pydantic AI AgentSpec

```bash
mkdir -p registry/agents/example/pydantic-reviewer/1.0.0
cat > registry/agents/example/pydantic-reviewer/1.0.0/agent.toml <<'EOF'
schema_version = 1
owner = "example"
name = "pydantic-reviewer"
version = "1.0.0"
description = "Reviews changes with a native Pydantic AI AgentSpec."
keywords = ["pydantic-ai", "agent-spec", "review"]
runtime = "pydantic-ai"
access = "read-only"
agent_spec = "agent-spec.yaml"
external_dependencies = ["a configured model provider"]
EOF
cat > registry/agents/example/pydantic-reviewer/1.0.0/agent-spec.yaml <<'EOF'
name: pydantic-reviewer
model: anthropic:claude-sonnet-4-6
instructions:
  - Review the requested changes without editing files.
  - Report concrete defects and missing tests.
model_settings:
  temperature: 0
retries: 2
end_strategy: exhaustive
EOF
cat > registry/agents/example/pydantic-reviewer/1.0.0/instructions.md <<'EOF'
Use the available read-only workspace tools. Include file paths with every finding.
EOF
cat > registry/agents/example/pydantic-reviewer/1.0.0/README.md <<'EOF'
# Pydantic reviewer

Use this agent for read-only review with Pydantic AI.
EOF
```

Set `agent_spec` to `agent-spec.yaml`, `agent-spec.yml`, or `agent-spec.json`. Agent Hub validates the file with Pydantic AI's `AgentSpec` model when it builds the registry and installs the bundle. The runtime uses `Agent.from_spec()` and merges `instructions.md` with the spec instructions.

Agent Hub remains responsible for permissions. Declare workspace tools and MCP servers in `agent.toml`. Catalog AgentSpecs cannot define `capabilities`, because capabilities can add tools or network access outside the catalog permission review.

## Validate a registry

```bash
uv run python -m agent_hub.registry_index registry
uv run python -m agent_hub.registry_index registry --check
uv run python -m agent_hub.registry_validation registry
uv run pytest tests/test_catalog_models.py tests/test_registry_index.py tests/test_registry_validation.py
```

The index generator reads the bundles and writes `registry/index.json`. Do not edit the index by hand. Validation rejects invalid schemas, unexpected files, symlinks, credential-like content, unsafe MCP URLs, and failed evaluation results.

## Publish a registry

```bash
git add registry

git commit -m "Publish example/reviewer 1.0.0"
git push
```

Serve `registry/index.json` and the `registry/agents/` directory without changing their relative paths. Require maintainer approval for `registry/`. Run the index, schema, security, and evaluation checks in CI.

Published versions are immutable. Never change or delete a file under an existing `<owner>/<name>/<version>/` directory. Publish a new semantic version instead. The repository CI compares pull requests with their base branch and rejects changes to an existing version.
