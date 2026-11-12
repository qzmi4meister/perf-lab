"""Optional, bounded user-space CPU profiling of an explicitly selected Linux PID."""

import re

from common import run, utc_now

MAX_PERF_BYTES = 64 * 1024 * 1024
PERF_FREQUENCY = 99
PERF_EVENT = "cpu-clock:u"
PERF_ENV = {"PERF_CONFIG": "/dev/null", "DEBUGINFOD_URLS": ""}


def perf_command(bundle, stage, argv, timeout, file_limit=None):
    result = run(argv, timeout=timeout, env=PERF_ENV, file_limit=file_limit)
    result["finished_at"] = utc_now()
    artifact = f"raw/perf/{stage}.json"
    bundle.write(artifact, result)
    record = {k: v for k, v in result.items() if k not in ("stdout", "stderr")}
    record["artifact"] = artifact
    if result["status"] == "error" and any(word in result["stderr"].lower() for word in
            ("permission", "operation not permitted", "access denied", "perf_event_paranoid")):
        record["error"] = "permission_denied"
    bundle.sources.append(record)
    return record, result["stdout"]


def record_perf(bundle, pid, duration, call_graph, start_ticks):
    profile = {"status": "incomplete", "pid": pid, "process_start_ticks": start_ticks,
               "event": PERF_EVENT, "frequency_hz": PERF_FREQUENCY,
               "call_graph": call_graph, "requested_seconds": duration,
               "max_data_bytes": MAX_PERF_BYTES, "scope": "process_and_inherited_children"}
    bundle.summary["facts"]["perf"] = profile
    bundle.note("Perf samples user-space CPU work, not kernel execution, off-CPU waits or request latency. "
                "Its sample percentages are not predicted speedups; profiling adds overhead.")
    bundle.note("Perf data contains process names, mapped paths and addresses; DWARF mode also captures "
                "user-stack bytes that may contain application secrets. Keep profiles local.")
    if start_ticks is None:
        profile["status"] = "unavailable"
        bundle.note("Perf skipped: the selected process identity was unreadable before collection.")
        return None

    version, output = perf_command(bundle, "version", ["perf", "--version"], 3)
    profile["version_source"] = version["artifact"]
    if version["status"] != "ok":
        profile["status"] = version["status"]
        bundle.note("Perf unavailable; see raw/perf/version.json. Baseline collection was retained.")
        return None
    profile["version"] = output.strip()
    data = bundle.path / "raw/perf/perf.data"
    command = ["perf", "record", "--pid", str(pid), "--event", PERF_EVENT,
               "--freq", str(PERF_FREQUENCY), "--call-graph", call_graph,
               "--mmap-pages", "128", "--no-buildid-cache", "--output", str(data.resolve()),
               "--", "sleep", str(duration)]
    record, _ = perf_command(bundle, "record", command, duration + 5, MAX_PERF_BYTES)
    profile.update(record_source=record["artifact"], started_at=record["started_at"],
                   finished_at=record["finished_at"], elapsed_seconds=record["elapsed_seconds"])
    if data.exists():
        data.chmod(0o600)
        profile.update(data="raw/perf/perf.data", data_bytes=data.stat().st_size)
        if profile["data_bytes"] >= MAX_PERF_BYTES:
            record.update(status="file_limit", error="profile_size_limit")
    if record["status"] == "ok" and not profile.get("data_bytes"):
        record.update(status="invalid_data", error="missing_or_empty_profile")
    profile["status"] = "recorded" if record["status"] == "ok" else record["status"]
    if record["status"] != "ok":
        bundle.note("Perf recording failed or reached a limit; see raw/perf/record.json and manifest.json. "
                    "The profile is not used for diagnosis; baseline collection was retained.")
    return record


def report_perf(bundle, record, end_ticks):
    profile = bundle.summary["facts"]["perf"]
    if record is None or record["status"] != "ok":
        return
    profile["process_end_ticks"] = end_ticks
    if end_ticks != profile["process_start_ticks"]:
        record.update(status="invalid_identity", error="process_exited_or_replaced")
        profile["status"] = "invalid_identity"
        bundle.note("Perf target exited, was replaced or became unreadable; profile identity cannot be "
                    "validated. No report was generated.")
        return
    data = bundle.path / profile["data"]
    command = ["perf", "report", "--input", str(data.resolve()), "--stdio", "--stdio-color", "never",
               "--header", "--show-nr-samples", "--no-children", "--percent-limit", "0",
               "--sort", "comm,dso,symbol", "--call-graph", "graph,0.5,caller", "--max-stack", "64"]
    report, output = perf_command(bundle, "report", command, 15)
    profile["report_source"] = report["artifact"]
    if output:
        artifact = "raw/perf/report.txt"
        path = bundle.path / artifact
        path.write_text(output)
        path.chmod(0o600)
        profile["report"] = artifact
    if report["status"] == "ok":
        # --no-children and --show-nr-samples give one self-sample count per row.
        samples = sum(int(n) for n in re.findall(r"(?m)^\s*\d+(?:\.\d+)?%\s+(\d+)\s+", output))
        if samples:
            profile["reported_samples"] = samples
        else:
            report.update(status="no_samples", error="no_recognized_sample_rows")
    profile["status"] = "complete" if report["status"] == "ok" else report["status"]
    if report["status"] != "ok":
        bundle.note("Perf report unavailable, incomplete or has no recognized samples; inspect "
                    "raw/perf/report.json and manifest.json. This does not establish zero CPU usage.")
    else:
        bundle.note("Read raw/perf/report.txt with record/report diagnostics: lost samples, unresolved "
                    "symbols and truncated stacks limit attribution. The report shows self overhead "
                    "and caller chains (0.5% call-chain threshold, at most 64 frames).")
