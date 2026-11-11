# Interpretation notes

## Linux

- `/proc/stat` CPU percentages cover the visible aggregate CPU accounting. Guest times already contribute to user/nice and must not be added twice. `cpu.executing` excludes idle, iowait and steal. Iowait is not a direct disk utilization or application blocked-time measurement; decreasing counters invalidate the interval calculation.
- A process using `1.0 cores` occupied approximately one CPU during the interval. This is not 100% of the whole machine or its cgroup entitlement. RSS is an instantaneous resident-page estimate; it is not a leak diagnosis.
- `MemAvailable` is more useful than free memory alone. Swap size is not current swap activity. Check swap-in/out rates, major faults and memory PSI together. `pswpin`/`pswpout` are pages, not bytes.
- PSI `some` measures time with at least one task stalled; `full` measures time with all non-idle tasks stalled. System CPU `full` is undefined and excluded. Interval PSI uses deltas of `total` microseconds, not `avg10`. No universal PSI percentage proves an incident.
- The cgroup CPU usage includes descendants. `nr_throttled / nr_periods` indicates the fraction of periods throttled at that level, not the fraction of CPU time lost. Ancestor restrictions can bind even when leaf counters show no throttling. Review collected ancestor quotas/cpusets. A large throttled-duration counter must not be interpreted as a bounded percent of wall time.
- Cgroup `memory.current` is not interchangeable with process RSS or Kubernetes working set. `memory.events` counters are cumulative; the collector derives interval rates and preserves raw values.
- Disk sector accounting uses 512-byte sectors. Device-mapper/partition/physical-device layers can describe the same I/O; do not sum them blindly. `io_time_ms` is retained as a rate, not a device saturation verdict. Assess throughput, latency, queueing and device behavior together.
- Interface error/drop rates need direction and interface scope. Low network throughput alone does not exclude DNS, retransmission, downstream or application latency.
- `/proc` visibility in containers can mix host-wide counters with namespace-limited processes/network devices. State this limitation before attributing a host incident.

Sources: [procfs](https://www.kernel.org/doc/html/latest/filesystems/proc.html), [PSI](https://www.kernel.org/doc/html/latest/accounting/psi.html), [cgroup v2](https://www.kernel.org/doc/html/latest/admin-guide/cgroup-v2.html), [I/O statistics](https://www.kernel.org/doc/html/latest/admin-guide/iostats.html).

## Kubernetes

- Metrics API CPU is average core usage over `window`; memory is working set. Use `timestamp` to detect stale or reused samples. Never present `kubectl top`-style data as request latency, CPU throttling, RSS, or historical percentiles.
- Missing Metrics API, RBAC denial and absent per-pod metrics remain unknown. Do not compute service totals as though uncovered containers used zero resources.
- CPU requests influence scheduling and allocation; CPU limits constrain usage. A request is not a hard maximum. A low average does not exclude bursts and throttling. Confirm throttling with cgroup counters or existing monitoring before recommending a quota change.
- Restart counts are cumulative over a pod/container's lifecycle. Check UID, state and last termination reason. `OOMKilled` is evidence of a kill, but does not by itself identify the allocation path or prove a memory leak. Pod replacement resets identity; compare workloads explicitly.
- Readiness, Service selector and EndpointSlice conditions are separate evidence. Ready endpoints do not prove healthy application response times. A Service without a selector is not an invitation to scan its namespace.
- An event reason is a clue. Events are ephemeral and the bundle omits messages; absence of an event is weak evidence. Similarly, a short sample cannot establish the absence of rare tail-latency incidents.
- Resource usage/limits shown here are per container. Init containers, native sidecars and pod-level resource settings need care; do not infer pod scheduler entitlement by summing this summary. Consult the original workload through an authorized, targeted read if this matters.
- If node data was requested, compare capacity/allocatable and pressure conditions only for selected pods' nodes. These snapshots do not reveal host-level run queues or storage latency.

Sources: [resource metrics](https://kubernetes.io/docs/tasks/debug/debug-cluster/resource-metrics-pipeline/), [resource management](https://kubernetes.io/docs/concepts/configuration/manage-resources-containers/), [EndpointSlices](https://kubernetes.io/docs/concepts/services-networking/endpoint-slices/).

## Further instrumentation

Use existing reviewed tools before generating probes. Choose PID/cgroup and a short observation interval. Confirm supported events and privileges rather than changing `perf_event_paranoid` or other settings automatically. Preserve raw output, tool versions and the measured scope.

For Ftrace, inspect existing tracer state and do not disturb an active session. For eBPF, bound duration, attach points and stack volume; probe complexity and observer effects matter. Neither path is implemented by the baseline collector. No claim of less than 0.1% overhead is made without measurement.
