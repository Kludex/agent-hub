---
name: docs-and-dx-editing
description: Write and review READMEs, guides, reference documentation, examples, diagrams, and installation flows. Use when improving developer documentation or command-line user experience.
---

# Documentation and DX editing

Read the documented code and run its examples before changing the explanation.

## Write

- Lead each concept with a complete, runnable, copy-pasteable example.
- Use short, plain sentences. Keep one idea in each sentence.
- Address the reader as `you`.
- Define a term when you first use it.
- Explain why a default exists and what failure it prevents.
- Structure pages as reference material with descriptive headings.
- Put warnings and important asides in titled admonitions.
- Show actual commands and output. Do not invent options or results.
- Prefer a purpose-built SVG when a generated diagram becomes dense or unreadable.
- Keep code references in backticks.

Do not narrate the writing process. Avoid filler, tutorial transitions, decorative emoji, and claims that the code does not support.

## Validate

1. Execute installation and quick-start commands in a clean environment when practical.
2. Check every internal link, heading anchor, command, file path, and package name.
3. Verify examples use the public API and current supported versions.
4. Render diagrams at the width used by the documentation site.
5. Ask whether a new user can recover from the most likely failure.

Return the edited documentation and a short list of commands or links you verified.
