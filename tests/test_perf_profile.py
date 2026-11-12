import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from common import Bundle, run
from perf_profile import record_perf, report_perf


class PerfTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.calls = self.root / "calls.jsonl"
        fake = self.root / "perf"
        fake.write_text("#!" + sys.executable + "\n" + r'''
import json, os, resource, sys, time
from pathlib import Path
args = sys.argv[1:]
scenario = os.environ.get("PERFLAB_PERF_SCENARIO", "ok")
with open(os.environ["PERFLAB_PERF_CALLS"], "a") as stream:
    stream.write(json.dumps({"args": args, "config": os.environ.get("PERF_CONFIG"),
                            "debuginfod": os.environ.get("DEBUGINFOD_URLS"),
                            "file_limit": resource.getrlimit(resource.RLIMIT_FSIZE)[0]}) + "\n")
if args == ["--version"]:
    print("perf version test")
elif args[0] == "record":
    if scenario == "denied":
        sys.stderr.write("perf_event_open: Operation not permitted")
        sys.exit(255)
    if scenario == "timeout":
        time.sleep(10)
    if scenario != "missing_data":
        output = Path(args[args.index("--output") + 1])
        with output.open("wb") as stream:
            stream.write(b"PERFILE2fake-profile")
            if scenario == "file_limit":
                stream.write(b"x" * 8192)
    sys.stderr.write("Recorded profile\n")
elif args[0] == "report":
    if scenario == "bad_report":
        sys.stderr.write("failed to read perf data")
        sys.exit(1)
    if scenario == "no_samples":
        print("# Samples: 0 of event 'cpu-clock:u'")
    else:
        print("# Samples: 4 of event 'cpu-clock:u'")
        print("# Overhead Samples Command Shared Object Symbol")
        print("   75.00%        3     app     app.so     [.] encode")
        print("             |--- handler")
        print("   25.00%        1     app     [unknown]  [.] 0x1234")
    if scenario == "output_limit":
        print("x" * 10000)
else:
    raise SystemExit(9)
''')
        fake.chmod(0o700)
        self.env = {"PATH": str(self.root) + os.pathsep + os.environ.get("PATH", ""),
                    "PERFLAB_PERF_CALLS": str(self.calls), "PERFLAB_PERF_SCENARIO": "ok",
                    "PERF_CONFIG": "must-not-be-used", "DEBUGINFOD_URLS": "https://must-not-be-used.invalid"}

    def collect(self, scenario="ok", extra=("--perf",)):
        dest = self.root / scenario
        env = {**os.environ, **self.env, "PERFLAB_PERF_SCENARIO": scenario}
        if scenario == "missing_tool":
            env["PATH"] = str(self.root / "empty-path")
        result = subprocess.run([sys.executable, str(ROOT / "scripts/collect.py"), "linux",
                                 "--pid", str(os.getpid()), "--duration", "1", "--out", str(dest),
                                 *extra], env=env, capture_output=True, text=True, timeout=15)
        summary = json.loads((dest / "summary.json").read_text())
        manifest = json.loads((dest / "manifest.json").read_text())
        calls = [json.loads(line) for line in self.calls.read_text().splitlines()] if self.calls.exists() else []
        return result, summary, manifest, dest, calls

    def test_cli_records_scoped_profile_and_readable_report(self):
        result, summary, manifest, dest, calls = self.collect()
        self.assertEqual(result.returncode, 0, result.stderr)
        profile = summary["facts"]["perf"]
        self.assertEqual(profile["status"], "complete")
        self.assertEqual(profile["reported_samples"], 4)
        self.assertEqual(profile["version"], "perf version test")
        self.assertEqual(profile["process_start_ticks"], profile["process_end_ticks"])
        self.assertGreaterEqual(summary["window_seconds"], 0.99)
        self.assertIn("process.cpu", summary["metrics"])
        self.assertFalse(any(name.startswith("perf.") for name in summary["metrics"]))
        self.assertEqual([c["args"][0] for c in calls], ["--version", "record", "report"])
        command = calls[1]["args"]
        self.assertEqual(command[command.index("--pid") + 1], str(os.getpid()))
        self.assertEqual(command[command.index("--event") + 1], "cpu-clock:u")
        self.assertEqual(command[command.index("--call-graph") + 1], "dwarf,4096")
        self.assertIn("--no-buildid-cache", command)
        self.assertNotIn("--all-cpus", command)
        self.assertEqual(calls[1]["file_limit"], 64 * 1024 * 1024)
        for call in calls:
            self.assertEqual(call["config"], "/dev/null")
            self.assertEqual(call["debuginfod"], "")
        for key in ("data", "version_source", "record_source", "report_source", "report"):
            path = dest / profile[key]
            self.assertTrue(path.is_file(), key)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        report = (dest / profile["report"]).read_text()
        self.assertIn("encode", report)
        self.assertIn("handler", report)
        self.assertIn("[unknown]", report)
        self.assertTrue(all(s["status"] == "ok" for s in manifest["sources"] if "/perf/" in s["artifact"]))

    def test_fp_mode_and_report_time_are_outside_baseline_window(self):
        from argparse import Namespace
        from collect import collect_linux

        bundle = Bundle(self.root / "fp", "linux", {})
        args = Namespace(pid=os.getpid(), duration=1, cgroup=None, perf=True, perf_call_graph="fp")
        captured = {}

        def report_after_snapshot(*args):
            captured["window"] = bundle.summary["window_seconds"]
            captured["sample_times"] = list(bundle.summary["facts"]["sample_times"])
            report_perf(*args)

        with patch.dict(os.environ, self.env), patch("collect.report_perf", side_effect=report_after_snapshot):
            collect_linux(args, bundle)
        self.assertEqual(bundle.summary["facts"]["perf"]["call_graph"], "fp")
        self.assertGreaterEqual(captured["window"], .99)
        self.assertEqual(captured["sample_times"], bundle.summary["facts"]["sample_times"])
        self.assertEqual(captured["window"], bundle.summary["window_seconds"])

    def test_default_does_not_invoke_perf(self):
        result, summary, _, _, calls = self.collect(extra=())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("perf", summary["facts"])
        self.assertEqual(calls, [])

    def test_missing_or_denied_perf_preserves_baseline(self):
        for scenario in ("denied", "missing_tool"):
            with self.subTest(scenario=scenario):
                result, summary, manifest, _, calls = self.collect(scenario)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(summary["status"], "partial")
                self.assertIn("process.cpu", summary["metrics"])
                self.assertGreaterEqual(summary["window_seconds"], 0.99)
                self.assertNotIn("report", summary["facts"]["perf"])
                self.assertFalse(any(c["args"][0] == "report" for c in calls))
                failures = [s for s in manifest["sources"] if "/perf/" in s["artifact"] and s["status"] != "ok"]
                self.assertTrue(failures)
                if scenario == "denied":
                    self.assertEqual(failures[0]["error"], "permission_denied")

    def test_profile_and_report_failures_are_not_successful_profiles(self):
        cases = {"missing_data": "invalid_data", "no_samples": "no_samples", "bad_report": "error"}
        for scenario, status in cases.items():
            with self.subTest(scenario=scenario):
                bundle = Bundle(self.root / scenario, "linux", {"pid": os.getpid()})
                with patch.dict(os.environ, {**self.env, "PERFLAB_PERF_SCENARIO": scenario}):
                    record = record_perf(bundle, os.getpid(), 1, "fp", 100)
                    report_perf(bundle, record, 100)
                bundle.finish(complete=True)
                self.assertEqual(bundle.summary["facts"]["perf"]["status"], status)
                self.assertEqual(bundle.summary["status"], "partial")
                self.assertNotIn("reported_samples", bundle.summary["facts"]["perf"])

    def test_file_limit_is_enforced_and_partial_data_is_not_reported(self):
        bundle = Bundle(self.root / "limited", "linux", {})
        with patch.dict(os.environ, {**self.env, "PERFLAB_PERF_SCENARIO": "file_limit"}), \
             patch("perf_profile.MAX_PERF_BYTES", 4096):
            record = record_perf(bundle, os.getpid(), 1, "fp", 100)
            report_perf(bundle, record, 100)
        profile = bundle.summary["facts"]["perf"]
        self.assertEqual(profile["status"], "file_limit")
        self.assertEqual((bundle.path / profile["data"]).stat().st_size, 4096)
        self.assertNotIn("report", profile)

    def test_timeout_and_report_overflow_are_incomplete_evidence(self):
        for scenario, expected in (("timeout", "timeout"), ("output_limit", "output_limit")):
            with self.subTest(scenario=scenario):
                bundle = Bundle(self.root / scenario, "linux", {})

                def bounded_run(argv, **kwargs):
                    if argv[1] == "record" and scenario == "timeout":
                        kwargs["timeout"] = 0.1
                    return run(argv, limit=512, **kwargs)

                with patch.dict(os.environ, {**self.env, "PERFLAB_PERF_SCENARIO": scenario}), \
                     patch("perf_profile.run", side_effect=bounded_run):
                    record = record_perf(bundle, os.getpid(), 1, "fp", 100)
                    report_perf(bundle, record, 100)
                self.assertEqual(bundle.summary["facts"]["perf"]["status"], expected)
                self.assertNotIn("reported_samples", bundle.summary["facts"]["perf"])

    def test_replaced_or_exited_pid_invalidates_profile(self):
        for end_ticks in (101, None):
            with self.subTest(end_ticks=end_ticks):
                bundle = Bundle(self.root / str(end_ticks), "linux", {})
                with patch.dict(os.environ, self.env):
                    record = record_perf(bundle, os.getpid(), 1, "fp", 100)
                    report_perf(bundle, record, end_ticks)
                bundle.finish(complete=True)
                self.assertEqual(bundle.summary["facts"]["perf"]["status"], "invalid_identity")
                self.assertEqual(bundle.summary["status"], "partial")
                self.assertNotIn("report", bundle.summary["facts"]["perf"])

    def test_cli_rejects_implicit_targets_and_unused_options(self):
        for options in (("--perf",), ("--perf-call-graph", "fp")):
            with self.subTest(options=options):
                dest = self.root / "invalid"
                result = subprocess.run([sys.executable, str(ROOT / "scripts/collect.py"), "linux",
                                         "--out", str(dest), *options], capture_output=True, text=True)
                self.assertEqual(result.returncode, 2)
                self.assertFalse(dest.exists())
                self.assertIn("requires", result.stderr)


if __name__ == "__main__":
    unittest.main()
