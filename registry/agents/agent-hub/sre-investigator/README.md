# SRE investigator

Use this agent to investigate alerts, incidents, staging failures, Kubernetes behavior, and production regressions.

It uses the observability and infrastructure tools already configured for Pi. The runtime needs shell access for those tools, so the profile declares shared workspace access. Its instructions prohibit workspace and infrastructure mutations.

```json
{
  "agent": "agent-hub/sre-investigator",
  "prompt": "Investigate the elevated MCP tool latency in staging EU during the last two hours.",
  "background": true
}
```

Grant only read-only credentials. Review every proposed remediation before applying it.
