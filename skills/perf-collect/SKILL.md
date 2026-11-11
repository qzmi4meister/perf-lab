---
name: perf-collect
description: Collect bounded diagnostic evidence from a running Linux server or selected Kubernetes pods or Service. Use for CPU, memory, I/O, resource-pressure or restart investigations, or preparing an offline evidence bundle. Does not change workloads or tune the system.
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
python3 "$PLUGIN_DIR/scripts/collect.py" linux --cgroup /sys/fs/cgroup/system.slice/example.service --out /tmp/perf-cgroup-001
python3 "$PLUGIN_DIR/scripts/collect.py" kubernetes --context production --namespace payments --service api --out /tmp/perf-k8s-001
```

Use a new output directory for every observation. Existing directories are refused. Default Linux observation is 10 seconds (maximum 60). Kubernetes commands have per-request time and output limits. `--include-nodes` adds reads of the nodes hosting selected pods when node-level evidence is relevant and within the user's scope.

## Preserve the evidence

Read `summary.json` and `manifest.json`. A partial bundle may still be useful. Exit 2 means no numeric metrics; inspect collected state instead of treating it as success or zero load. Exit 1 means collection failed; an incomplete bundle may remain.

The summary links metrics to source artifacts. Missing tools, RBAC, absent Metrics API, exited processes, output limits and timeouts are gaps in evidence. Explain the gap and the smallest useful next collection. Do not silently install packages, elevate privileges or widen the target.

Linux artifacts are selected procfs/cgroup files. Kubernetes artifacts are allowlisted projections of API responses, not verbatim full objects. Raw full Pod objects may be processed transiently but are never saved. No kubeconfig, Secret, ConfigMap, process environment, container arguments, application logs or event-message payloads are included. Bundles still contain internal names and resource information; keep them local unless the user requests transfer.

This baseline does not run perf, eBPF, Ftrace, kubectl exec/debug, load generators, restarts or tuning. Those require a separate targeted plan under the user's existing authorization. Read-only collection still has some overhead; no fixed overhead budget is claimed.

Return the absolute bundle path, target, observation interval, collection status and material missing evidence. If diagnosis was requested, proceed with the packaged `perf-analyze` workflow using the same bundle; otherwise stop after collection.
