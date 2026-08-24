# Agent Hub contributor guidance

- An agent is an independent worker with its own model context, tools, permissions, workspace, and lifecycle.
- A skill is a reusable procedure loaded into the current agent. It has no independent process, context, workspace, or lifecycle.
- Classify a proposed capability before implementing it. Use an agent only when the work needs delegation, isolation, concurrency, persistence, or separate permissions. Use a skill when the current agent only needs a specialized workflow.
- Keep catalog agents focused. Do not turn a workflow checklist into another generic agent.
- Use Pyright in strict mode for Python type checking. Do not add Mypy configuration or dependencies.
