---
name: api-compatibility-review
description: Review public API designs and changes for simplicity, typing, compatibility, and ecosystem impact. Use for new APIs, deprecations, extension points, and framework integration proposals.
---

# API compatibility review

Read the repository contributor guidance and compatibility policy first.

## Review

1. State the user problem without adopting the proposed implementation.
2. Inventory the existing public API, documented behavior, types, exceptions, lifecycle, and extension points.
3. Find real call sites in downstream projects. Separate common use from hypothetical flexibility.
4. Compare the smallest viable options. Prefer composition, protocols, and existing conventions over inheritance or a new abstraction.
5. Evaluate source, behavioral, typing, serialization, concurrency, and performance compatibility.
6. Define migration and deprecation behavior when existing users must change.
7. Write a complete usage example for the preferred design.
8. List public-API tests that protect the contract without pinning internal structure.

Do not approve an API only because it is flexible. Every parameter, hook, and type becomes a maintenance commitment.

## Report

Return:

- `Recommendation`: The preferred design in one paragraph.
- `Example`: Complete user-facing code.
- `Contract`: Inputs, outputs, failures, lifecycle, and invariants.
- `Compatibility`: Existing behavior, downstream effects, and migration.
- `Alternatives`: Rejected options and the concrete reason for rejecting each.
- `Validation`: Public behaviors and ecosystem tests.
