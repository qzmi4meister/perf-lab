# Evidence bundle v1

Collectors create a new directory (0700) and files (0600). They refuse an existing output directory. Files are not uploaded. Names, paths, internal workload identities and resource information remain potentially sensitive.

```text
bundle/
├── summary.json
├── manifest.json
└── raw/
```

`summary.json` contains:

- `schema_version`: `1`; `collector_version` identifies the implementation.
- `kind`: `linux` or `kubernetes`; `target`: explicitly selected target.
- `started_at`, `finished_at`: UTC ISO timestamps. Individual sources have observation times. Files are not atomic snapshots.
- `status`: `complete` means requested sources were captured; `partial` means a source failed; `incomplete` means the collection did not finish. None implies system health or complete diagnostic coverage.
- `window_seconds`: actual elapsed interval for Linux, including snapshot overhead. Kubernetes uses per-pod Metrics API `timestamp` and `window` in `facts.metrics_windows` instead.
- `metrics`: name → `{value, unit, sources}`. `sources` are paths relative to this bundle. Missing values are omitted, never filled with zero. CPU usage may use `cores` or `%`; these are different units and scopes.
- `facts`: environment and selected state, including pod identities and resource settings.
- `limitations`: interpretation boundaries and missing evidence.

`manifest.json` records every source attempt, artifact path, time, status, and either file path or command arguments. Command output is bounded to 2 MiB across stdout and stderr per invocation. Kubernetes command errors are categorized rather than retaining arbitrary stderr. File reads are limited to 2 MiB per file. An over-limit response is unusable, not parsed as a complete response.

Linux `raw/before` and `raw/after` contain selected procfs/cgroup text exactly as read (UTF-8 replacement for invalid bytes). The bundle records clock-tick/page units. `raw/cgroup-ancestors` preserves available ancestor limits. Only explicit cgroup v2 selection is supported; no cross-namespace path inference is attempted.

Optional Linux `--perf` adds `facts.perf`, without changing schema version 1 or adding profile percentages to `metrics`. It records PID/start ticks, perf version, event, requested frequency/duration, stack mode, scope, actual command times and artifact paths. Recording occurs between the procfs snapshots; report generation occurs afterward. The profile's command interval includes perf setup/finalization and is not an exact sampling window.

- `raw/perf/version.json`, `record.json`, `report.json`: bounded command stdout/stderr, exit status, arguments and timing. The manifest adds validation results such as `invalid_identity`, `file_limit`, `invalid_data` and `no_samples`; these may override a successful command exit. Entries and files exist only for attempted stages.
- `raw/perf/perf.data`: native binary recording, with a hard 64 MiB per-file limit. A failed/interrupted recording may leave an unusable file; its existence does not imply a valid profile. No report is generated for a failed recording or an exited/replaced target.
- `raw/perf/report.txt`: text report, possibly partial if report generation failed or hit the 2 MiB combined stdout/stderr limit. Use only when `facts.perf.status` is `complete`; inspect diagnostics for lost samples and unresolved symbols even then. `reported_samples` counts the recognized self-sample rows, not lost samples or complete application coverage.

The default event is `cpu-clock:u` at a requested 99 Hz. DWARF mode captures up to 4096 bytes of user stack per sample; `fp` requires frame pointers. Profiles cover the selected process and inherited child tasks, not all pre-existing children or an entire cgroup. The report keeps unresolved symbols, uses a 0.5% caller-chain display threshold and a maximum depth of 64 frames. Native data retains the recorded information for later local analysis. **DWARF stack bytes can contain secrets or application data**; the baseline field filtering does not sanitize the binary recording. Profiling remains local, with no automatic symbol downloads, permission changes or build-ID cache updates.

Kubernetes `raw/*.json` contains projections of API objects. Full Pod specs can include credentials in literal env values, so those responses are parsed in bounded memory and only selected diagnostic fields are written. Environment, annotations, command arguments, application logs, event/condition messages, secret/config-map contents and kubeconfig are excluded. This is not a full-cluster backup. Service endpoints retain readiness and target identity, not IP addresses.

Exit codes: `0` completed with numeric metrics (may be partial), `1` failed/interrupted, `2` completed without numeric metrics. Always inspect status and manifest. A pod with missing resource metrics may still have useful restart/state data.

`compare.py` accepts bundle directories or summary files and emits JSON to stdout. It retains absolute/relative deltas and provenance; baseline zero produces a null relative delta. Comparison never labels lower values as automatically better. Its exit code indicates whether comparison could be generated, not whether a change passed.
