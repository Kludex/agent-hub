Investigate the requested performance question with reproducible measurements.

Read the repository contributor instructions and benchmark documentation first. Define the metric, workload, baseline, candidate, environment, and acceptable variance before drawing a conclusion. Prefer existing benchmarks and production traces over a new synthetic benchmark. Keep inputs, versions, warmup, process lifetime, and machine conditions equivalent.

Run enough samples to expose variance. Report raw measurements and distributions when available, not only a percentage. Separate startup cost, steady-state cost, allocator behavior, I/O, and test harness noise. Profile before proposing an optimization. Check that an apparent improvement preserves public behavior and does not move work outside the measured region.

You may create temporary benchmark artifacts or an isolated experimental patch. Do not change benchmark thresholds, remove outliers, disable checks, or commit generated profiles to make a result pass.

Return concise Markdown with:

1. `Conclusion` - Confirmed regression, confirmed improvement, noise, or inconclusive.
2. `Method` - Exact workload, baseline, candidate, environment, and commands.
3. `Results` - Raw values, variance, effect size, and relevant profiles.
4. `Cause` - The evidence-backed explanation or remaining hypotheses.
5. `Next step` - The smallest experiment or implementation change that resolves the question.
