---
name: release-readiness
description: Verify that a software release is ready. Use when checking versions, changelogs, CI, artifacts, compatibility, release notes, or a failed publishing workflow.
---

# Release readiness

Read the repository release documentation and recent release workflow before running commands.

## Verify

1. Identify the intended version, target branch, previous release, package names, and supported runtimes.
2. Confirm the working tree and release branch contain the intended commits.
3. Verify version metadata is consistent across source, lock files, generated files, tags, and documentation.
4. Review the changelog for user-facing changes, deprecations, security notes, contributors, and links.
5. Run the repository's required lint, type, test, coverage, documentation, and packaging checks.
6. Build artifacts in a clean environment. Inspect names and contents, then install and smoke-test what users will download.
7. Confirm release workflows, permissions, trusted publishing, attestations, checksums, and downstream compatibility.
8. For a failed release, establish which external artifacts already exist before proposing a retry.

Do not create or delete tags, publish artifacts, edit releases, rotate credentials, or rerun a publishing job without explicit approval.

## Report

Return:

- `Decision`: Ready, not ready, or blocked.
- `Blockers`: Required corrections with evidence.
- `Checks`: Commands and outcomes.
- `Artifacts`: Built and inspected files.
- `Release action`: Exact approved sequence, including recovery steps when relevant.
