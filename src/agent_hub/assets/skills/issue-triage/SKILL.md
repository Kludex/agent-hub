---
name: issue-triage
description: Validate, reproduce, classify, and prioritize software issues. Use when deciding whether an issue is valid, already solved, duplicated, actionable, or needs more information.
---

# Issue triage

Read the repository contributor instructions before investigating.

## Investigate

1. Read the complete report, comments, linked work, and relevant history.
2. Identify the claimed public behavior, expected behavior, affected versions, environment, and missing information.
3. Search for duplicates, prior fixes, specifications, and downstream reports.
4. Reproduce through the public API with the smallest realistic input. Do not import private symbols only to trigger a branch.
5. Test the latest supported release and current main when the distinction matters.
6. Trace the behavior to the relevant implementation and tests.
7. Measure compatibility, security, and performance impact before assigning priority.

Do not modify files, post comments, close issues, or create pull requests unless the user asks.

## Report

Return:

- `Decision`: Valid, invalid, duplicate, already fixed, expected behavior, or needs information.
- `Evidence`: Exact commands, versions, output, specifications, and file paths.
- `Impact`: Who is affected and how severely.
- `Next action`: The smallest concrete action for the maintainer or reporter.

State uncertainty directly. An AI-generated report is neither valid nor invalid until the evidence establishes it.
