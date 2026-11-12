#!/usr/bin/env python3
"""Collect Linux or Kubernetes performance evidence without changing the target."""

import argparse
import json
import os
from pathlib import Path
import platform
import re
import shutil
import sys
import time

from common import Bundle, run, utc_now
from perf_profile import record_perf, report_perf

PROC_FILES = ("stat", "meminfo", "loadavg", "diskstats", "net/dev",
              "pressure/cpu", "pressure/memory", "pressure/io", "vmstat")
CGROUP_FILES = ("cpu.stat", "cpu.max", "memory.current", "memory.max", "memory.events",
                "cpuset.cpus.effective", "cpu.pressure", "memory.pressure", "io.pressure", "io.stat")


def flat(text):
    result = {}
    for line in (text or "").splitlines():
        parts = line.replace(":", "").split()
        if len(parts) >= 2:
            try:
                result[parts[0]] = int(parts[1])
            except ValueError:
                pass
    return result


def delta(a, b):
    return b - a if a is not None and b is not None and b >= a else None


def cpu_times(text):
    for line in (text or "").splitlines():
        if line.startswith("cpu "):
            # guest and guest_nice are already included in user and nice.
            return list(map(int, line.split()[1:9]))
    return None


def process_stat(text):
    if not text:
        return None
    fields = text[text.rfind(")") + 2:].split()
    return {"cpu_ticks": int(fields[11]) + int(fields[12]), "start_ticks": int(fields[19]),
            "rss_pages": int(fields[21])}


def pressure(text):
    return {parts[0]: {k: float(v) for k, v in (p.split("=", 1) for p in parts[1:])}
            for line in (text or "").splitlines() if (parts := line.split())}


def disk_counters(text):
    result = {}
    for line in (text or "").splitlines():
        p = line.split()
        if len(p) >= 14:
            result[p[2]] = {"read_ops": int(p[3]), "read_bytes": int(p[5]) * 512,
                            "write_ops": int(p[7]), "write_bytes": int(p[9]) * 512,
                            "io_time_ms": int(p[12])}
    return result


def net_counters(text):
    result = {}
    for line in (text or "").splitlines():
        if ":" in line:
            name, data = line.split(":", 1)
            p = list(map(int, data.split()))
            if len(p) >= 16:
                result[name.strip()] = {"rx_bytes": p[0], "rx_errors": p[2], "rx_drops": p[3],
                                        "tx_bytes": p[8], "tx_errors": p[10], "tx_drops": p[11]}
    return result


def linux_snapshot(bundle, label, pid=None, cgroup=None):
    started = time.monotonic()
    data = {name: bundle.read(Path("/proc") / name, f"raw/{label}/proc/{name}") for name in PROC_FILES}
    if pid:
        for name in ("stat", "status", "io", "cgroup"):
            data[f"pid/{name}"] = bundle.read(Path(f"/proc/{pid}") / name, f"raw/{label}/pid/{name}")
    if cgroup:
        data["cgroup_inode"] = cgroup.stat().st_ino
        for name in CGROUP_FILES:
            data[f"cgroup/{name}"] = bundle.read(cgroup / name, f"raw/{label}/cgroup/{name}")
    data["monotonic"] = (started + time.monotonic()) / 2
    data["observed_at"] = utc_now()
    return data


def summarize_linux(bundle, before, after):
    elapsed = after["monotonic"] - before["monotonic"]
    bundle.summary["window_seconds"] = elapsed
    bundle.summary["facts"]["sample_times"] = [before["observed_at"], after["observed_at"]]
    cgroup_stable = before.get("cgroup_inode") == after.get("cgroup_inode")
    if "cgroup_inode" in after:
        bundle.summary["facts"]["cgroup_inode"] = after["cgroup_inode"]
        if not cgroup_stable:
            bundle.note("Cgroup was replaced during collection; all cgroup interval metrics omitted.")

    def sources(name):
        folder = "" if name.startswith(("pid/", "cgroup/")) else "proc/"
        return [f"raw/{label}/{folder}{name}" for label in ("before", "after")]

    def rate(key, old, new, unit, evidence):
        change = delta(old, new)
        if change is None:
            bundle.note(f"{key}: missing or decreasing counter; no rate calculated.")
        else:
            bundle.metric(key, change / elapsed, unit, evidence)

    a, b = cpu_times(before.get("stat")), cpu_times(after.get("stat"))
    if a and b and len(a) == len(b) == 8:
        changes = [new - old for old, new in zip(a, b)]
        if min(changes) >= 0 and sum(changes) > 0:
            total = sum(changes)
            bundle.metric("cpu.executing", sum(changes[i] for i in (0, 1, 2, 5, 6)) * 100 / total, "%", sources("stat"))
            for name, i in (("idle", 3), ("iowait", 4), ("steal", 7)):
                bundle.metric(f"cpu.{name}", changes[i] * 100 / total, "%", sources("stat"))
        else:
            bundle.note("CPU counters decreased or did not advance; CPU percentages omitted.")
    mem = flat(after.get("meminfo"))
    for name in ("MemTotal", "MemAvailable", "SwapTotal", "SwapFree"):
        if name in mem:
            bundle.metric(f"memory.{name}", mem[name] * 1024, "bytes", [sources("meminfo")[1]])
    for scope in ("pressure/", "cgroup/"):
        if scope == "cgroup/" and not cgroup_stable:
            continue
        for resource in ("cpu", "memory", "io"):
            name = f"pressure/{resource}" if scope == "pressure/" else f"cgroup/{resource}.pressure"
            a, b = pressure(before.get(name)), pressure(after.get(name))
            for category in a.keys() & b.keys():
                if scope == "pressure/" and resource == "cpu" and category == "full":
                    continue  # Undefined at system level.
                change = delta(a[category].get("total"), b[category].get("total"))
                if change is not None:
                    bundle.metric(f"{scope}{resource}.{category}", change / (elapsed * 10000), "%", sources(name))
    for prefix, source, parser in (("disk", "diskstats", disk_counters), ("network", "net/dev", net_counters)):
        a, b = parser(before.get(source)), parser(after.get(source))
        for device in a.keys() & b.keys():
            for counter in a[device]:
                unit = "bytes/s" if counter.endswith("bytes") else "count/s"
                if counter == "io_time_ms":
                    unit = "ms/s"
                rate(f"{prefix}.{device}.{counter}", a[device][counter], b[device][counter], unit, sources(source))
    a, b = flat(before.get("vmstat")), flat(after.get("vmstat"))
    for counter in ("pgmajfault", "pswpin", "pswpout", "oom_kill"):
        rate(f"vm.{counter}", a.get(counter), b.get(counter), "pages/s" if counter.startswith("pswp") else "count/s", sources("vmstat"))
    if "pid/stat" in before:
        a, b = process_stat(before.get("pid/stat")), process_stat(after.get("pid/stat"))
        if a and b and a["start_ticks"] == b["start_ticks"]:
            bundle.summary["facts"]["process_start_ticks"] = b["start_ticks"]
            change = delta(a["cpu_ticks"], b["cpu_ticks"])
            if change is not None:
                bundle.metric("process.cpu", change / os.sysconf("SC_CLK_TCK") / elapsed, "cores", sources("pid/stat"))
            bundle.metric("process.rss", b["rss_pages"] * os.sysconf("SC_PAGE_SIZE"), "bytes", [sources("pid/stat")[1]])
            a, b = flat(before.get("pid/io")), flat(after.get("pid/io"))
            for counter in ("read_bytes", "write_bytes"):
                rate(f"process.{counter}", a.get(counter), b.get(counter), "bytes/s", sources("pid/io"))
        else:
            bundle.note("Process exited, was replaced, or was unreadable; process rates omitted.")
    if "cgroup_inode" in before:
        if not cgroup_stable:
            return
        a, b = flat(before.get("cgroup/cpu.stat")), flat(after.get("cgroup/cpu.stat"))
        usage = delta(a.get("usage_usec"), b.get("usage_usec"))
        if usage is not None:
            bundle.metric("cgroup.cpu", usage / 1e6 / elapsed, "cores", sources("cgroup/cpu.stat"))
        periods = delta(a.get("nr_periods"), b.get("nr_periods"))
        throttled = delta(a.get("nr_throttled"), b.get("nr_throttled"))
        if periods and throttled is not None and throttled <= periods:
            bundle.metric("cgroup.throttled_periods", throttled * 100 / periods, "%", sources("cgroup/cpu.stat"))
        if after.get("cgroup/memory.current"):
            bundle.metric("cgroup.memory", int(after["cgroup/memory.current"]), "bytes", [sources("cgroup/memory.current")[1]])
        a, b = flat(before.get("cgroup/memory.events")), flat(after.get("cgroup/memory.events"))
        for counter in a.keys() & b.keys():
            rate(f"cgroup.memory_events.{counter}", a[counter], b[counter], "count/s", sources("cgroup/memory.events"))


def collect_linux(args, bundle):
    facts = bundle.summary["facts"]
    facts.update(kernel=platform.release(), machine=platform.machine(), hostname=platform.node(),
                 visible_cpus=os.cpu_count(), clock_ticks=os.sysconf("SC_CLK_TCK"), page_bytes=os.sysconf("SC_PAGE_SIZE"))
    facts["boot_id"] = (bundle.read("/proc/sys/kernel/random/boot_id", "raw/boot_id") or "").strip()
    bundle.read("/proc/self/cgroup", "raw/collector.cgroup")
    bundle.read("/proc/self/mountinfo", "raw/collector.mountinfo")
    if args.cgroup:
        # Retain ancestor limits; leaf quota alone is not the effective entitlement.
        for index, directory in enumerate([args.cgroup, *args.cgroup.parents]):
            if directory == Path("/sys/fs"):
                break
            for name in ("cpu.max", "memory.max", "cpuset.cpus.effective"):
                if (directory / name).exists():
                    bundle.read(directory / name, f"raw/cgroup-ancestors/{index}/{name}")
        facts["cgroup_limits_sources"] = "raw/cgroup-ancestors/"
        bundle.note("Leaf throttling counters do not prove absence of throttling at ancestors; review ancestor limits.")
    before = linux_snapshot(bundle, "before", args.pid, args.cgroup)
    started = time.monotonic()
    if args.perf:
        process = process_stat(before.get("pid/stat"))
        recording = record_perf(bundle, args.pid, args.duration, args.perf_call_graph,
                                process["start_ticks"] if process else None)
    # A missing/denied perf must not shorten the baseline observation interval.
    time.sleep(max(0, args.duration - (time.monotonic() - started)))
    after = linux_snapshot(bundle, "after", args.pid, args.cgroup)
    summarize_linux(bundle, before, after)
    if args.perf:
        process = process_stat(after.get("pid/stat"))
        report_perf(bundle, recording, process["start_ticks"] if process else None)
    facts["tools_available"] = {name: bool(shutil.which(name)) for name in ("perf", "iostat", "pidstat", "vmstat", "bpftrace")}
    bundle.note("Metrics describe the collector's visible procfs/network namespaces; a container view is not a full host view.")
    bundle.note("Two samples yield interval averages, not latency percentiles or proof of a bottleneck. Disk layers may double-count I/O.")


def pick(obj, names):
    return {k: obj[k] for k in names if k in obj}


def identity(obj):
    meta = obj.get("metadata", {})
    return pick(meta, ("name", "namespace", "uid", "creationTimestamp", "generation"))


def state(value):
    return {name: pick(details, ("reason", "exitCode", "signal", "startedAt", "finishedAt"))
            for name, details in value.items() if name in ("waiting", "running", "terminated")}


def project_pod(obj):
    spec, status = obj.get("spec", {}), obj.get("status", {})
    result = {"metadata": identity(obj), "owners": [pick(o, ("kind", "name", "uid")) for o in obj.get("metadata", {}).get("ownerReferences", [])],
              "node": spec.get("nodeName"), "phase": status.get("phase"), "qos_class": status.get("qosClass"),
              "conditions": [pick(c, ("type", "status", "reason", "lastTransitionTime")) for c in status.get("conditions", [])],
              "containers": []}
    for spec_key, status_key in (("containers", "containerStatuses"), ("initContainers", "initContainerStatuses"),
                                 ("ephemeralContainers", "ephemeralContainerStatuses")):
        statuses = {s["name"]: s for s in status.get(status_key, [])}
        for container in spec.get(spec_key, []):
            current = statuses.get(container["name"], {})
            result["containers"].append({"name": container["name"], "kind": spec_key,
                "resources": pick(container.get("resources", {}), ("requests", "limits")),
                **pick(current, ("ready", "restartCount", "started")),
                "state": state(current.get("state", {})), "last_state": state(current.get("lastState", {}))})
    return result


def quantity(text):
    match = re.fullmatch(r"([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)([numkKMGTEP]|[KMGTPE]i)?", str(text))
    if not match:
        raise ValueError(f"unsupported resource quantity: {text}")
    scales = {"": 1, "n": 1e-9, "u": 1e-6, "m": 1e-3, "k": 1e3, "K": 1e3}
    scales.update({s: 1000 ** i for i, s in enumerate("MGTPE", 2)})
    scales.update({s + "i": 1024 ** i for i, s in enumerate("KMGTPE", 1)})
    return float(match[1]) * scales[match[2] or ""]


def items(obj):
    return obj.get("items", []) if "items" in obj else [obj]


def collect_kubernetes(args, bundle):
    base = ["kubectl", "--context", args.context, "--namespace", args.namespace,
            f"--request-timeout={args.timeout}s"]

    def query(name, command, project):
        response = run([*base, *command], args.timeout + 1)
        # kubectl JSON may contain env literals/annotations. Only allowlisted
        # projections reach disk; raw stdout/stderr never enter the bundle.
        record = {k: v for k, v in response.items() if k not in ("stdout", "stderr")}
        record["artifact"] = f"raw/{name}.json"
        value = None
        if record["status"] == "ok":
            try:
                value = project(json.loads(response["stdout"]))
                bundle.write(record["artifact"], value)
            except (ValueError, KeyError, TypeError) as exc:
                record.update(status="invalid_data", error=type(exc).__name__)
        else:
            stderr = response.get("stderr", "").lower()
            record["error"] = next((code for token, code in (("forbidden", "rbac_denied"),
                ("unauthorized", "unauthorized"), ("notfound", "not_found"), ("not found", "not_found"),
                ("no matches", "api_unavailable"), ("doesn't have a resource type", "api_unavailable"))
                if token in stderr), "command_failed")
        bundle.sources.append(record)
        return value

    selector = args.selector
    if args.service:
        service = query("service", ["get", "service", args.service, "-o", "json"],
            lambda o: {"metadata": identity(o), "selector": o.get("spec", {}).get("selector", {}),
                       "type": o.get("spec", {}).get("type"),
                       "ports": [pick(p, ("name", "port", "targetPort", "protocol")) for p in o.get("spec", {}).get("ports", [])]})
        if not service or not service["selector"]:
            bundle.note("Service unavailable or has no selector; no namespace-wide fallback was attempted.")
            return
        selector = ",".join(f"{k}={v}" for k, v in sorted(service["selector"].items()))
        query("endpointslices", ["get", "endpointslices", "-l", f"kubernetes.io/service-name={args.service}", "-o", "json"],
            lambda o: [{"metadata": identity(s), "addressType": s.get("addressType"), "ports": s.get("ports") or [],
                        "endpoints": [{"conditions": e.get("conditions", {}), "nodeName": e.get("nodeName"),
                                       "targetRef": pick(e.get("targetRef", {}), ("kind", "name", "namespace", "uid"))}
                                      for e in (s.get("endpoints") or [])]} for s in items(o)])
    scope = [args.pod] if args.pod else (["-l", selector] if selector else [])

    def pods_project(obj):
        pods = items(obj)
        if len(pods) > args.max_pods:
            raise ValueError("pod limit exceeded; narrow selector")
        return [project_pod(p) for p in pods]

    pods = query("pods", ["get", "pods", *scope, "-o", "json"], pods_project)
    if pods is None:
        bundle.note("Pod inventory unavailable or above max-pods/output limit; narrow the target or check access.")
        return
    bundle.summary["facts"]["pods"] = pods
    names = {p["metadata"]["name"] for p in pods}
    uids = {p["metadata"]["uid"] for p in pods}
    if not pods:
        bundle.note("No matching pods at collection time; this is not a healthy-service verdict.")
        return
    query("events", ["get", "events", "--field-selector", "involvedObject.kind=Pod", "-o", "json"],
          lambda o: [{**pick(e, ("type", "reason", "count", "firstTimestamp", "lastTimestamp", "eventTime")),
                      "object": pick(e.get("involvedObject", {}), ("name", "namespace", "uid", "kind"))}
                     for e in items(o) if e.get("involvedObject", {}).get("uid") in uids])
    metrics = query("pod-metrics", ["get", "pods.metrics.k8s.io", *scope, "-o", "json"],
        lambda o: [{"metadata": identity(p), **pick(p, ("timestamp", "window")),
                    "containers": [{"name": c["name"], "usage": pick(c.get("usage", {}), ("cpu", "memory"))}
                                   for c in p.get("containers", [])]}
                   for p in items(o) if p.get("metadata", {}).get("name") in names])
    bundle.summary["facts"]["metrics_windows"] = []
    for pod in metrics or []:
        name = pod["metadata"]["name"]
        bundle.summary["facts"]["metrics_windows"].append({"pod": name, "timestamp": pod.get("timestamp"), "window": pod.get("window")})
        for container in pod["containers"]:
            for resource, unit in (("cpu", "cores"), ("memory", "bytes")):
                if resource in container["usage"]:
                    try:
                        value = quantity(container["usage"][resource])
                        bundle.metric(f"pod.{name}.{container['name']}.{resource}", value, unit, ["raw/pod-metrics.json"])
                    except ValueError:
                        bundle.note(f"Unsupported {resource} quantity in metrics for {name}.")
    missing_metrics = names - {p["metadata"]["name"] for p in metrics or []}
    if missing_metrics:
        bundle.note("Resource metrics unavailable for: " + ", ".join(sorted(missing_metrics)))
    for pod in pods:
        for container in pod["containers"]:
            prefix = f"pod.{pod['metadata']['name']}.{container['name']}"
            if "running" in container["state"]:
                missing = [r for r in ("cpu", "memory") if f"{prefix}.{r}" not in bundle.summary["metrics"]]
                if missing:
                    bundle.note(f"Missing resource metrics for {prefix}: {', '.join(missing)}.")
            if "restartCount" in container:
                bundle.metric(f"{prefix}.restarts", container["restartCount"], "count", ["raw/pods.json"])
    if args.include_nodes:
        nodes = sorted({p["node"] for p in pods if p["node"]})
        if nodes:
            query("nodes", ["get", "nodes", *nodes, "-o", "json"],
                lambda o: [{"metadata": identity(n),
                            "capacity": n.get("status", {}).get("capacity", {}),
                            "allocatable": n.get("status", {}).get("allocatable", {}),
                            "conditions": [pick(c, ("type", "status", "reason", "lastTransitionTime"))
                                           for c in n.get("status", {}).get("conditions", [])]}
                           for n in items(o)])
    bundle.note("Kubernetes files are allowlisted projections, not full API objects. Env, arguments, annotations, logs and event messages are omitted.")
    bundle.note("Metrics API CPU is an average over its reported window; memory is working set. These are not request latency or CPU throttling metrics.")
    bundle.note("API observations are not atomic; events expire, restart counts are cumulative, and missing metrics never mean zero.")


def positive(text):
    value = int(text)
    if value <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="kind", required=True)
    linux = sub.add_parser("linux", help="Two bounded procfs snapshots on the target Linux host")
    linux.add_argument("--out", required=True, help="New evidence directory; never overwritten")
    linux.add_argument("--duration", type=positive, default=10, help="Seconds between snapshots (1–60)")
    linux.add_argument("--pid", type=positive)
    linux.add_argument("--cgroup", type=Path, help="Explicit cgroup v2 directory beneath /sys/fs/cgroup")
    linux.add_argument("--perf", action="store_true", help="Also record a bounded user-space CPU profile; requires --pid")
    linux.add_argument("--perf-call-graph", choices=("dwarf", "fp"),
                       help="Stack recording with --perf: dwarf (default, captures stack bytes) or fp (requires frame pointers)")
    kube = sub.add_parser("kubernetes", help="Scoped read-only kubectl collection")
    kube.add_argument("--out", required=True)
    kube.add_argument("--context", required=True)
    kube.add_argument("--namespace", required=True)
    scope = kube.add_mutually_exclusive_group(required=True)
    scope.add_argument("--service")
    scope.add_argument("--selector")
    scope.add_argument("--pod")
    scope.add_argument("--all-pods", action="store_true", help="Explicitly collect this namespace's pods")
    kube.add_argument("--include-nodes", action="store_true", help="Read nodes hosting the selected pods")
    kube.add_argument("--timeout", type=positive, default=10, help="Per-request timeout in seconds (1–30)")
    kube.add_argument("--max-pods", type=positive, default=100)
    args = parser.parse_args()
    if args.kind == "linux":
        if sys.platform != "linux":
            parser.error("linux collection must run on the target Linux machine")
        if args.duration > 60:
            parser.error("duration must be at most 60 seconds")
        if args.perf and not args.pid:
            parser.error("--perf requires an explicit --pid; cgroup-only and host-wide profiling are not supported")
        if args.perf_call_graph and not args.perf:
            parser.error("--perf-call-graph requires --perf")
        args.perf_call_graph = "fp" if args.perf_call_graph == "fp" else "dwarf,4096"
        if args.cgroup:
            args.cgroup = args.cgroup.resolve()
            root = Path("/sys/fs/cgroup").resolve()
            if not args.cgroup.is_relative_to(root) or not (args.cgroup / "cgroup.controllers").is_file():
                parser.error("cgroup must be an existing cgroup v2 directory beneath /sys/fs/cgroup")
        target = {"hostname": platform.node(), "pid": args.pid, "cgroup": str(args.cgroup) if args.cgroup else None}
    else:
        if args.timeout > 30 or args.max_pods > 1000:
            parser.error("timeout must be at most 30 and max-pods at most 1000")
        for key in ("context", "namespace", "pod", "service", "selector"):
            value = getattr(args, key)
            if value is not None and (not value.strip() or value.startswith("-") or "\x00" in value):
                parser.error(f"invalid {key}")
        target = {key: getattr(args, key) for key in ("context", "namespace", "pod", "service", "selector", "all_pods")}
    os.umask(0o077)
    bundle = None
    try:
        bundle = Bundle(args.out, args.kind, target)
        (collect_linux if args.kind == "linux" else collect_kubernetes)(args, bundle)
        bundle.finish(complete=True)
    except (OSError, ValueError, KeyboardInterrupt) as exc:
        if bundle:
            bundle.note("Collection interrupted or failed: " + type(exc).__name__)
            bundle.finish()
        print(f"Collection failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({"bundle": str(bundle.path.resolve()), "status": bundle.summary["status"],
                      "metrics": len(bundle.summary["metrics"])}))
    return 0 if bundle.summary["metrics"] else 2


if __name__ == "__main__":
    sys.exit(main())
