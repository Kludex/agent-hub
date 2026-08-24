# PR shepherd

Use this agent to review a pull request, prepare requested fixes, run tests, and reach a merge-readiness decision.

The agent can modify its assigned workspace when you request follow-up changes. Run it with `isolated: true` when you do not want those changes in your current checkout.

```json
{
  "agent": "agent-hub/pr-shepherd",
  "prompt": "Review https://github.com/example/project/pull/123. Fix blockers and wait for the relevant CI checks.",
  "background": true,
  "isolated": true
}
```

The agent never merges, comments, pushes, or rewrites history without an explicit request.
