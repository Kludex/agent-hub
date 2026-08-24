# Implementation planner

```bash
agent-hub catalog show agent-hub/implementation-planner
agent-hub catalog install agent-hub/implementation-planner
```

Use this agent before coding a feature, bug fix, migration, or architectural change. It inspects the repository and returns an execution-ready Markdown plan. It never modifies files.

## Permissions

| Requirement | Value |
| --- | --- |
| Runtime | Pydantic AI AgentSpec |
| Workspace | Read-only |
| Tools | `read_file`, `list_files` |
| MCP servers | None |
| Additional network access | No |
| Default provider | Anthropic |

You can override the model at invocation time. The selected provider must be configured in the Agent Hub service environment.

## Output

The plan describes the goal, current design, ordered implementation steps, validation work, risks, and unresolved decisions. Every proposed change cites the relevant file or symbol and explains why it is needed.
