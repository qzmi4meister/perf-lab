---
name: perf-verify
description: Evaluate whether a specific Linux-server or Kubernetes-service change improved performance using before/after evidence and repeatable measurements. Use for performance regression checks, resource-limit changes or optimization validation; distinguish descriptive differences from demonstrated improvements.
---

# Verify a performance change

Use the user's requested change and success criterion. Existing authorization persists. Verification does not itself authorize a production rollout, restart, tuning or load test. Resolve the installed plugin root two directories above this skill.

1. Define the outcome being tested: latency distribution, throughput at a stated error rate, resource cost for equivalent work, or incident frequency. If the criterion is unavailable, report descriptive comparison and the missing criterion rather than inventing a pass threshold.
2. Establish comparable workload, traffic, concurrency, application version except for the intended change, resource limits, machine/node class, warmup and observation windows. Check Pod UID, PID start time, kernel/boot identity and Metrics API timestamps. Explain intentional differences instead of ignoring them.
3. Confirm the change actually took effect. Examples: verify resource settings and the current pod generation; for THP inspect how much memory uses huge pages, not merely whether a setting permits them. Keep correctness/error-rate checks alongside performance measurements.
4. Use existing valid evidence first. Compare two perf-lab bundles with:

   ```bash
   python3 "$PLUGIN_DIR/scripts/compare.py" /path/to/before /path/to/after
   ```

   The result is deliberately `descriptive_only`. Review comparability issues, missing metrics, source artifacts and differing intervals. A lower CPU reading can simply mean lower throughput. Cumulative counters require stable identity and a known elapsed interval.
5. Where an experiment is authorized and practical, repeat baseline and candidate observations under matched load; interleave runs when drift matters. Report sample count, spread and application-level outcome. Do not treat a single noisy pair as statistically established improvement. Don't construct latency percentiles from average CPU/memory samples.

Return one of: **improvement supported**, **regression supported**, **no clear difference**, or **insufficient evidence**. Include the measured change with units, comparability conditions, correctness results and remaining uncertainty. Use "supported" only when the evidence meets the stated criterion. Stop after the requested proof; do not broaden into unrelated tuning.

If only baseline bundles are available, a valid outcome is: "CPU usage changed by X cores; application performance improvement is unproven because traffic and latency were not measured."
