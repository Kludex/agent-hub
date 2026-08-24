Create a maintainer briefing for the repositories named in the request. Use the current repository when the request does not name one.

Read the repository contributor instructions before investigating. Collect current evidence from GitHub, local branches, CI, security alerts, and release workflows. Use read-only commands. Do not modify files, push commits, comment, merge, close issues, publish releases, or change repository settings.

Prioritize work by user impact, security, release risk, and whether another person is blocked. Distinguish a confirmed problem from an automated finding or an unverified report. Do not recommend action from a title alone.

Return concise Markdown with these sections:

1. `Act now` - Blocking failures, security concerns, regressions, and reviews that need an immediate decision.
2. `Waiting` - Work blocked on CI, reviewers, reporters, or upstream changes.
3. `Watch` - Flaky checks, uncertain reports, and trends that need more evidence.
4. `Later` - Valid maintenance that is not time-sensitive.

For every item, include the repository, a direct link, the evidence, and one concrete next action. Link every pull request reference. Omit empty sections and low-signal activity.
