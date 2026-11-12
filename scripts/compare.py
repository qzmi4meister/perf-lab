#!/usr/bin/env python3
"""Compare two evidence summaries descriptively; never infer causality or a speedup."""

import argparse
import json
from pathlib import Path
import sys


def read_summary(path):
    path = Path(path)
    if path.is_dir():
        path = path / "summary.json"
    with path.open() as stream:
        data = json.load(stream)
    if data.get("schema_version") != 1 or not isinstance(data.get("metrics"), dict):
        raise ValueError(f"unsupported summary: {path}")
    return data


def compare(before, after):
    reasons = []
    if before.get("kind") != after.get("kind"):
        reasons.append("Collection kinds differ.")
    if before.get("target") != after.get("target"):
        reasons.append("Targets differ; establish workload equivalence before comparison.")
    if before.get("status") == "incomplete" or after.get("status") == "incomplete":
        reasons.append("At least one collection is incomplete.")
    old, new = before.get("facts", {}), after.get("facts", {})
    profile_keys = ("event", "frequency_hz", "call_graph", "scope", "version")
    old_profile, new_profile = old.get("perf", {}), new.get("perf", {})
    if bool(old_profile) != bool(new_profile) or any(old_profile.get(k) != new_profile.get(k) for k in profile_keys):
        reasons.append("CPU profiling settings differ; observer overhead may affect baseline metrics.")
    for key in ("kernel", "machine", "visible_cpus", "boot_id", "process_start_ticks", "cgroup_inode"):
        if old.get(key) != new.get(key):
            reasons.append(f"Environment/identity changed: {key}.")
    if before.get("kind") == "kubernetes":
        def pod_ids(facts):
            return {p["metadata"]["name"]: p["metadata"].get("uid") for p in facts.get("pods", [])}
        if pod_ids(old) != pod_ids(new):
            reasons.append("Pod identities changed; pod samples and cumulative restarts may not be comparable.")
        old_windows = {w["pod"]: w for w in old.get("metrics_windows", [])}
        for window in new.get("metrics_windows", []):
            previous = old_windows.get(window["pod"])
            if previous:
                if not previous.get("timestamp") or not window.get("timestamp") or window["timestamp"] == previous["timestamp"]:
                    reasons.append(f"Reused or missing Metrics API timestamp for {window['pod']}.")
                if previous.get("window") != window.get("window"):
                    reasons.append(f"Metrics API averaging windows differ for {window['pod']}.")
    a, b = before["metrics"], after["metrics"]
    rows = []
    for key in sorted(a.keys() & b.keys()):
        if a[key].get("unit") != b[key].get("unit"):
            reasons.append(f"Units differ: {key}.")
            continue
        baseline, candidate = a[key]["value"], b[key]["value"]
        difference = candidate - baseline
        rows.append({"metric": key, "unit": a[key]["unit"], "before": baseline, "after": candidate,
                     "delta": difference, "relative_change_percent": difference * 100 / baseline if baseline else None,
                     "before_sources": a[key].get("sources", []), "after_sources": b[key].get("sources", [])})
    if not rows:
        reasons.append("No common metrics with matching units.")
    return {"schema_version": 1, "assessment": "descriptive_only", "comparability_issues": reasons,
            "before_window_seconds": before.get("window_seconds"), "after_window_seconds": after.get("window_seconds"),
            "metrics": rows, "only_before": sorted(a.keys() - b.keys()), "only_after": sorted(b.keys() - a.keys()),
            "limitations": ["Two observations cannot establish a causal performance improvement.",
                            "Match workload, traffic, resource limits and sampling windows; measure repetitions and application correctness.",
                            *before.get("limitations", []), *after.get("limitations", [])]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("before")
    parser.add_argument("after")
    args = parser.parse_args()
    try:
        result = compare(read_summary(args.before), read_summary(args.after))
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"Cannot compare: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
