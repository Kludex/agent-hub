# Performance investigator

Use this agent for benchmark regressions, memory growth, throughput changes, profiling, and flaky performance checks.

```json
{
  "agent": "agent-hub/performance-investigator",
  "prompt": "Determine whether pull request 123 regresses multipart parsing throughput. Compare it with main and explain the cause.",
  "background": true,
  "isolated": true
}
```

The agent reports reproducible commands, raw measurements, variance, and the limits of its conclusion. It does not treat a single benchmark run as proof.
