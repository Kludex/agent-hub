# Maintainer inbox

Use this agent for a prioritized briefing across one or more GitHub repositories.

It inspects pull requests, issues, CI, security alerts, and releases. It never mutates GitHub or the workspace. The Pi runtime needs shell access to run the authenticated `gh` CLI, so the profile declares shared workspace access even though its instructions prohibit writes.

```json
{
  "agent": "agent-hub/maintainer-inbox",
  "prompt": "Brief me on Starlette, Uvicorn, and python-multipart. Show only items that need my attention.",
  "background": true
}
```

Review the briefing before taking any action. Automated findings and failing checks are evidence to investigate, not automatic decisions.
