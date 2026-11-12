# Perf Lab

A plugin for diagnosing running Linux servers and Kubernetes services. Collect bounded measurements, investigate causes, and verify the effect of changes. Version 0.1.0.

| Skill | Purpose |
|---|---|
| `perf-collect` | Collect an evidence bundle for a server, process, cgroup, or selected pods |
| `perf-analyze` | Produce findings and hypotheses backed by measurements |
| `perf-verify` | Compare observations and assess whether the evidence supports an improvement |

## Run the collector

Requires Python 3.10 or later, with no additional Python dependencies. Run Linux collection on the machine being diagnosed. Kubernetes collection uses an installed `kubectl` and your existing access permissions.

```bash
cd ~/git/perf-lab

# Observe this Linux machine for 10 seconds
python3 scripts/collect.py linux --duration 10 --out /tmp/perf-host-001

# Observe a specific process or its verified cgroup v2 directory
python3 scripts/collect.py linux --pid 1234 --out /tmp/perf-process-001
python3 scripts/collect.py linux \
  --cgroup /sys/fs/cgroup/system.slice/example.service \
  --out /tmp/perf-cgroup-001

# Collect pods behind a Kubernetes Service
python3 scripts/collect.py kubernetes \
  --context production --namespace payments --service api \
  --out /tmp/perf-service-001

# Select pods by label and include their nodes' status
python3 scripts/collect.py kubernetes \
  --context production --namespace payments --selector app=api \
  --include-nodes --out /tmp/perf-pods-001

# Compare two bundles descriptively
python3 scripts/compare.py /tmp/perf-host-001 /tmp/perf-host-002
```

Each run requires a new output directory. `summary.json` contains a compact summary; `manifest.json` records the result of each read; `raw/` contains source Linux files or selected Kubernetes fields. See the [bundle format](references/bundle-format.md).

Linux collection covers CPU, available memory, swap activity, major faults, disk and network I/O, and PSI. Optional process and cgroup v2 collection adds process metrics, throttling, and memory events. Rates are calculated from the difference between two snapshots. This does not measure latency percentiles.

Kubernetes collection covers container states and resources, readiness, restarts, termination reasons, events for selected pods, and available CPU/memory metrics. Service collection also includes its selector and EndpointSlices. Node status is optional. Context and namespace must always be explicit; collecting all pods in a namespace requires `--all-pods`.

The collector does not automatically elevate privileges, install packages, run `exec/debug`, change resources, restart workloads, or generate load. Optional Linux CPU profiling uses an installed `perf`; eBPF, Ftrace and Prometheus are not integrated. Missing metrics remain unknown. The Kubernetes Metrics API does not measure request latency or CPU throttling; those require additional sources.

## CPU profiles with perf

Add `--perf` to a Linux process collection to find functions and caller chains consuming user-space CPU:

```bash
python3 scripts/collect.py linux --pid 1234 --perf \
  --duration 10 --out /tmp/perf-profile-001
```

This runs `perf record` during the baseline observation, then `perf report` after the final procfs snapshot. It requests `cpu-clock:u` samples at 99 Hz, with DWARF call stacks capped at 4096 bytes per sample. Use `--perf-call-graph fp` when the application and relevant libraries preserve frame pointers. The selected process's threads and newly inherited child tasks are included; already-running child processes are not automatically attached. Host-wide and cgroup-only profiling are not supported.

The bundle adds `raw/perf/perf.data`, `raw/perf/report.txt`, and JSON diagnostics for the perf version, recording and report commands. `facts.perf` in `summary.json` records profile status, settings, process identity and artifact paths. Reports contain self overhead, sample counts and caller chains; they retain unresolved symbols. `compare.py` continues to compare baseline numeric metrics, not function percentages.

Recording has a hard 64 MiB file limit and a timeout of the requested duration plus 5 seconds. Report generation has a 15-second timeout; each command's stdout/stderr is limited to 2 MiB combined. Missing perf, denied access, exited/replaced processes and collection limits leave baseline evidence available and mark the bundle partial. Failed or identity-invalid recordings are not reported. Always inspect `facts.perf.status` and the manifest; a successful CLI exit can still contain a failed profile.

Perf configuration files and debuginfod downloads are disabled for these commands, and recording does not update the build-ID cache. No permission settings are changed. Native symbols and usable unwind information are needed for attribution; interpreted/JIT runtimes may require additional runtime support. A CPU profile does not measure off-CPU waits or request latency, and a function's sample share is not its potential application speedup.

**Profile privacy:** `perf.data` includes process names, mapped paths and addresses. DWARF profiles also contain user-stack memory, which may include credentials or application data. Profiles are kept in the private bundle; the baseline collector's field-exclusion guarantees do not apply to those stack bytes. Profiling overhead depends on the workload and stack mode.

For a Kubernetes container, run this Linux mode through an already authorized execution path on its node or inside the container, with an explicitly verified PID in that environment. A pod name is not a Linux PID. The Kubernetes command does not run perf or create a debug container, and namespace/permission restrictions may prevent profiling.

See the [perf record](https://man7.org/linux/man-pages/man1/perf-record.1.html), [perf report](https://man7.org/linux/man-pages/man1/perf-report.1.html) and [access-control](https://docs.kernel.org/admin-guide/perf-security.html) documentation.

## Connect to Codex

The root `plugin.json` is the portable manifest. `.codex-plugin/plugin.json` provides compatibility with Codex clients using the earlier format. `.agents/plugins/marketplace.json` declares a local marketplace containing this plugin. These workflows do not require an MCP server.

With the repository open in a client that supports local marketplaces, open Plugins, select **Perf Lab (local)**, and install **Perf Lab**. After changing the source, you may need to refresh the installed copy and start a new session. Creating or cloning the repository does not install the plugin globally.

Install from GitHub:

```bash
codex plugin marketplace add qzmi4meister/perf-lab
codex plugin add perf-lab@perf-lab-local
```

For local development, use `codex plugin marketplace add ~/git/perf-lab` instead of the GitHub source.

These commands register the marketplace and install the plugin in your user configuration. During development, you can check discovery without saving settings:

```bash
codex \
  -c 'marketplaces.perf-lab-local.source_type="local"' \
  -c "marketplaces.perf-lab-local.source=\"$PWD\"" \
  plugin list --marketplace perf-lab-local --available --json
```

Run this check from the repository root. See the [official packaging and installation documentation](https://developers.openai.com/plugins/build/plugins).

Example requests after installation:

- "Collect evidence for Service api in namespace payments, context production, and explain possible causes of slow responses."
- "Analyze `/tmp/perf-service-001` and distinguish observations from hypotheses."
- "Compare these two bundles after a CPU limit change and assess whether the evidence supports an improvement."

Skills are regular directories under `skills/`, but they use shared plugin resources: keep the whole package together when moving it. For a remote Linux server, copy the `scripts/` directory, run collection there, and transfer the evidence bundle through an already authorized channel for analysis.

## Development checks

```bash
python3 -m unittest discover -s tests -v
python3 scripts/collect.py linux --duration 1 --out /tmp/perf-smoke-unique
```

Tests cover units, deltas, process replacement, Kubernetes field filtering, access failures, command limits, comparison, and the perf collection/report lifecycle. Perf tests use a fake executable to exercise success, denied access, missing tools, file/output limits, timeouts, empty samples and identity changes. Kubernetes integration tests use a fake `kubectl`; these tests do not verify actual profiling permissions or cluster access. [Behavioral acceptance cases for skills](tests/behavioral-cases.md) are evaluated separately from collector code. Passing unit tests does not establish AI diagnostic accuracy.

Baseline bundles exclude container environment variables and arguments, annotations, kubeconfig, secrets, and logs. Optional DWARF profiles can contain sensitive stack memory as described above. Full Pod API responses may briefly reside in memory before fields are filtered. Bundle files are accessible only to their owner, but still contain internal names and resource information. Collection has some overhead; overhead below 0.1% is not guaranteed.

## Methodology and references

- Brendan Gregg, *Performance Tools and Prompts*, Linux Plumbers Conference 2026: measure → form hypotheses → run targeted checks → validate the outcome.
- [Linux PSI](https://www.kernel.org/doc/html/latest/accounting/psi.html) and [cgroup v2](https://www.kernel.org/doc/html/latest/admin-guide/cgroup-v2.html).
- [Kubernetes resource metrics](https://kubernetes.io/docs/tasks/debug/debug-cluster/resource-metrics-pipeline/).
- [Intel Performance Skills](https://github.com/intel/intel-performance-skills) is a separate project covering detailed CPU analysis; none of its code is copied here.
