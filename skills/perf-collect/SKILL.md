---
name: perf-collect
description: Collect bounded diagnostic evidence from a running Linux server or selected Kubernetes pods or Service, including optional perf CPU profiles for a Linux PID. Use for CPU, memory, I/O, resource-pressure or restart investigations, or preparing an offline evidence bundle. Does not tune workloads.
---

# Collect performance evidence

Use the user's target and existing authorization. An instruction to analyze an existing bundle does not require new collection. Do not ask for confirmation again when target and scope are already clear. User instructions override this workflow.

Resolve this installed skill's directory; the plugin root is two directories above it. Run `scripts/collect.py` from that root with Python 3.10+. Never assume the user's working directory is the plugin directory. The CLI's `--help` describes supported options.

## Choose the collection scope

- **Linux:** run on the actual target machine, using an already authorized shell/SSH connection. Running locally observes this machine only. Use `--pid` for the identified process. Use `--cgroup` only with a verified cgroup v2 directory in the target's visible `/sys/fs/cgroup`; a container's `/proc/PID/cgroup` path may not map directly into another namespace's mount.
- **Kubernetes:** specify `--context` and `--namespace`, then exactly one of `--service`, `--pod`, `--selector`, or explicitly requested `--all-pods`. Prefer Service or workload selector. The collector pins context per request and does not switch kubeconfig. A selectorless Service requires another explicit target; never broaden silently.
- If the target cannot be inferred from the request or existing context, ask one concise question and wait for the answer. Do not discover every cluster or scan unrelated hosts.

Examples, with `PLUGIN_DIR` set to the resolved plugin root:

```bash
python3 "$PLUGIN_DIR/scripts/collect.py" linux --pid 1234 --duration 10 --out /tmp/perf-linux-001
python3 "$PLUGIN_DIR/scripts/collect.py" linux --pid 1234 --perf --duration 10 --out /tmp/perf-profile-001
python3 "$PLUGIN_DIR/scripts/collect.py" linux --cgroup /sys/fs/cgroup/system.slice/example.service --out /tmp/perf-cgroup-001
python3 "$PLUGIN_DIR/scripts/collect.py" kubernetes --context production --namespace payments --service api --out /tmp/perf-k8s-001
```

Use a new output directory for every observation. Existing directories are refused. Default Linux observation is 10 seconds (maximum 60). Kubernetes commands have per-request time and output limits. `--include-nodes` adds reads of the nodes hosting selected pods when node-level evidence is relevant and within the user's scope.

## Optional CPU profiling

Use `linux --pid PID --perf` when function-level CPU attribution is relevant and process profiling is within the user's scope. It uses installed `perf` to record `cpu-clock:u` at 99 Hz during the observation, then writes a text report. Default DWARF recording captures up to 4096 bytes of user stack per sample; choose `--perf-call-graph fp` only when frame pointers are available. The binary can contain sensitive application memory, not just function names. Existing authorization persists; do not require a new confirmation merely because profiling is supported.

Read `facts.perf.status`, the manifest, `raw/perf/report.txt` and the referenced command diagnostics. Failed, truncated, empty or identity-invalid profiles are gaps in evidence even when the CLI exits 0. Recording is capped at 64 MiB and duration + 5 seconds; report generation is capped at 15 seconds and command output at 2 MiB. Missing tools or denied access preserve baseline metrics. Do not automatically change permissions or retry with a wider target.

This mode profiles an explicit PID and inherited child tasks. It does not profile a whole host/cgroup or automatically attach to a Kubernetes pod. For a container, use an already authorized node/container execution path and verify the PID in that environment. Do not infer a host PID from a pod name or launch `kubectl exec/debug` as an automatic fallback.

## Preserve the evidence

Read `summary.json` and `manifest.json`. A partial bundle may still be useful. Exit 2 means no numeric metrics; inspect collected state instead of treating it as success or zero load. Exit 1 means collection failed; an incomplete bundle may remain.

The summary links metrics to source artifacts. Missing tools, RBAC, absent Metrics API, exited processes, output limits and timeouts are gaps in evidence. Explain the gap and the smallest useful next collection. Do not silently install packages, elevate privileges or widen the target.

Baseline Linux artifacts are selected procfs/cgroup files. Kubernetes artifacts are allowlisted projections of API responses, not verbatim full objects. Raw full Pod objects may be processed transiently but are never saved. Baseline collection excludes kubeconfig, Secret, ConfigMap, process environment, container arguments, application logs and event-message payloads. Optional perf data contains names, paths and addresses; DWARF stack bytes may contain secrets. Keep bundles local unless the user requests transfer.

The collector does not run eBPF, Ftrace, kubectl exec/debug, load generators, restarts or tuning. Those require a separate targeted plan under the user's existing authorization. Perf remains opt-in through `--perf`; it does not change kernel permissions or download symbols. Read-only collection still has some overhead; no fixed overhead budget is claimed.

Return the absolute bundle path, target, observation interval, collection status and material missing evidence. If diagnosis was requested, proceed with the packaged `perf-analyze` workflow using the same bundle; otherwise stop after collection.
