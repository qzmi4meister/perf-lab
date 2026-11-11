import copy
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
from collect import process_stat, project_pod, quantity, summarize_linux
from compare import compare


POD = {
    "metadata": {"name": "api-1", "namespace": "payments", "uid": "uid-1",
                 "annotations": {"private": "DO_NOT_STORE"}, "labels": {"app": "api"}},
    "spec": {"nodeName": "node-1", "containers": [{"name": "api", "args": ["DO_NOT_STORE"],
             "env": [{"name": "SECRET", "value": "DO_NOT_STORE"}],
             "resources": {"requests": {"cpu": "100m"}, "limits": {"cpu": "1", "memory": "128Mi"}}}]},
    "status": {"phase": "Running", "qosClass": "Burstable",
               "conditions": [{"type": "Ready", "status": "True", "message": "DO_NOT_STORE"}],
               "containerStatuses": [{"name": "api", "ready": True, "restartCount": 2,
                   "state": {"running": {"startedAt": "2026-11-11T12:00:00Z"}},
                   "lastState": {"terminated": {"reason": "OOMKilled", "exitCode": 137, "message": "DO_NOT_STORE"}}}]}
}


def snapshot(cpu, ticks, pressure_total):
    return {"stat": cpu, "monotonic": ticks, "observed_at": "2026-11-11T12:00:00Z",
            "meminfo": "MemTotal: 1000 kB\nMemAvailable: 200 kB\nSwapTotal: 50 kB\nSwapFree: 40 kB\n",
            "pressure/cpu": f"some avg10=1.00 avg60=0.00 avg300=0.00 total={pressure_total}\nfull avg10=0.00 total=0\n",
            "vmstat": "pgmajfault 0\npswpin 0\npswpout 0\noom_kill 0\n"}


def proc_stat(start, cpu=100):
    fields = ["S"] + ["0"] * 49
    fields[11], fields[12], fields[19], fields[21] = str(cpu), "0", str(start), "10"
    return "42 (name with ) spaces) " + " ".join(fields)


class LinuxTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.bundle = Bundle(Path(self.temp.name) / "bundle", "linux", {})

    def test_cpu_guest_is_not_double_counted_and_units_are_preserved(self):
        before = snapshot("cpu 10 0 10 80 0 0 0 0 5 0\n", 10, 0)
        after = snapshot("cpu 20 0 20 160 0 0 0 0 10 0\n", 20, 1_000_000)
        summarize_linux(self.bundle, before, after)
        metrics = self.bundle.summary["metrics"]
        self.assertEqual(metrics["cpu.executing"]["value"], 20)
        self.assertEqual(metrics["cpu.idle"]["value"], 80)
        self.assertEqual(metrics["memory.MemAvailable"]["value"], 200 * 1024)
        self.assertEqual(metrics["pressure/cpu.some"]["value"], 10)
        self.assertNotIn("pressure/cpu.full", metrics)

    def test_counter_reset_does_not_produce_negative_cpu_usage(self):
        before = snapshot("cpu 10 0 10 80 0 0 0 0\n", 10, 100)
        after = snapshot("cpu 1 0 1 8 0 0 0 0\n", 20, 50)
        summarize_linux(self.bundle, before, after)
        self.assertNotIn("cpu.executing", self.bundle.summary["metrics"])
        self.assertNotIn("pressure/cpu.some", self.bundle.summary["metrics"])
        self.assertTrue(any("decreased" in s for s in self.bundle.summary["limitations"]))

    def test_pid_reuse_is_not_a_cpu_sample(self):
        before, after = snapshot(None, 10, 0), snapshot(None, 20, 0)
        before["pid/stat"], after["pid/stat"] = proc_stat(100, 100), proc_stat(200, 200)
        summarize_linux(self.bundle, before, after)
        self.assertNotIn("process.cpu", self.bundle.summary["metrics"])
        self.assertEqual(process_stat(proc_stat(200))["start_ticks"], 200)

    def test_cgroup_throttling_is_period_fraction(self):
        before, after = snapshot(None, 10, 0), snapshot(None, 20, 0)
        before.update(cgroup_inode=1)
        after.update(cgroup_inode=1)
        before["cgroup/cpu.stat"] = "usage_usec 1000000\nnr_periods 100\nnr_throttled 50\n"
        after["cgroup/cpu.stat"] = "usage_usec 6000000\nnr_periods 120\nnr_throttled 55\n"
        summarize_linux(self.bundle, before, after)
        self.assertEqual(self.bundle.summary["metrics"]["cgroup.throttled_periods"]["value"], 25)
        self.assertEqual(self.bundle.summary["metrics"]["cgroup.cpu"]["value"], 0.5)

    def test_cgroup_replacement_invalidates_pressure_too(self):
        before, after = snapshot(None, 10, 0), snapshot(None, 20, 0)
        before["cgroup_inode"], after["cgroup_inode"] = 1, 2
        before["cgroup/cpu.pressure"] = "some total=100\n"
        after["cgroup/cpu.pressure"] = "some total=1000000\n"
        summarize_linux(self.bundle, before, after)
        self.assertFalse(any(k.startswith("cgroup") for k in self.bundle.summary["metrics"]))
        self.assertTrue(any("replaced" in s for s in self.bundle.summary["limitations"]))

    def test_bundle_refuses_overwrite_and_preserves_private_modes(self):
        with self.assertRaises(FileExistsError):
            Bundle(self.bundle.path, "linux", {})
        self.assertEqual(self.bundle.path.stat().st_mode & 0o777, 0o700)
        self.assertEqual((self.bundle.path / "summary.json").stat().st_mode & 0o777, 0o600)


class CommandTests(unittest.TestCase):
    def test_bounded_output(self):
        result = run([sys.executable, "-c", "import sys; sys.stdout.write('x' * 10000)"], limit=100)
        self.assertEqual(result["status"], "output_limit")
        self.assertEqual(len(result["stdout"]), 100)

    def test_timeout(self):
        result = run([sys.executable, "-c", "import time; time.sleep(10)"], timeout=0.05)
        self.assertEqual(result["status"], "timeout")
        self.assertLess(result["elapsed_seconds"], 2)

    def test_exit_failure_and_missing_tool(self):
        self.assertEqual(run([sys.executable, "-c", "raise SystemExit(7)"])["status"], "error")
        self.assertEqual(run(["/does/not/exist"])["status"], "unavailable")


class KubernetesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.fake = self.root / "kubectl"
        # This executable replaces kubectl in PATH; tests never contact a cluster.
        self.fake.write_text("#!" + sys.executable + "\n" + "POD = " + repr(POD) + "\n" + r'''
import json, os, sys
args = sys.argv[1:]
scenario = os.environ.get("PERFLAB_TEST_SCENARIO", "ok")
with open(os.environ["PERFLAB_TEST_CALLS"], "a") as f:
    f.write(json.dumps(args) + "\n")
resource = args[args.index("get") + 1]
if scenario == "rbac" and resource == "pods":
    sys.stderr.write("Forbidden: DO_NOT_STORE")
    sys.exit(1)
if resource == "service":
    result = {"metadata": {"name": "api"}, "spec": {"selector": {} if scenario == "selectorless" else {"app": "api"}, "type": "ClusterIP", "ports": []}}
elif resource == "endpointslices":
    result = {"items": [{"metadata": {"name": "api-slice"}, "endpoints": [{"conditions": {"ready": True}, "addresses": ["DO_NOT_STORE"]}]}]}
elif resource == "pods":
    result = {"items": [POD] * (2 if scenario == "too_many" else 1)}
elif resource == "events":
    result = {"items": [{"type": "Warning", "reason": "BackOff", "message": "DO_NOT_STORE", "involvedObject": {"uid": "uid-1", "name": "api-1"}}]}
elif resource == "pods.metrics.k8s.io":
    if scenario == "missing_metrics":
        sys.stderr.write("the server doesn't have a resource type DO_NOT_STORE")
        sys.exit(1)
    result = {"items": [{"metadata": {"name": "api-1", "namespace": "payments"}, "timestamp": "2026-11-11T12:00:00Z", "window": "30s", "containers": [{"name": "api", "usage": {"cpu": "250m", "memory": "64Mi"}}]}]}
elif resource == "nodes":
    result = {"items": [{"metadata": {"name": "node-1", "annotations": {"private": "DO_NOT_STORE"}}, "status": {"capacity": {"cpu": "4"}, "allocatable": {"cpu": "3"}, "conditions": [{"type": "MemoryPressure", "status": "False"}]}}]}
else:
    raise SystemExit(9)
print(json.dumps(result))
''')
        self.fake.chmod(0o700)

    def collect(self, scenario="ok", extra=()):
        dest = self.root / scenario
        calls = self.root / (scenario + "-calls")
        env = {**os.environ, "PATH": str(self.root) + os.pathsep + os.environ.get("PATH", ""),
               "PERFLAB_TEST_SCENARIO": scenario, "PERFLAB_TEST_CALLS": str(calls)}
        result = subprocess.run([sys.executable, str(ROOT / "scripts/collect.py"), "kubernetes",
                                 "--context", "test-only", "--namespace", "payments", "--service", "api",
                                 "--out", str(dest), *extra], env=env, capture_output=True, text=True, timeout=15)
        summary = json.loads((dest / "summary.json").read_text())
        commands = [json.loads(line) for line in calls.read_text().splitlines()]
        for path in dest.rglob("*"):
            if path.is_file():
                self.assertNotIn("DO_NOT_STORE", path.read_text())
        return result, summary, commands, dest

    def test_projection_excludes_credentials_and_preserves_evidence(self):
        pod = project_pod(POD)
        self.assertNotIn("DO_NOT_STORE", json.dumps(pod))
        self.assertEqual(pod["containers"][0]["last_state"]["terminated"]["reason"], "OOMKilled")

    def test_units(self):
        for text, expected in (("250m", 0.25), ("500000000n", 0.5), ("64Mi", 64 * 1024**2), ("1G", 1e9), ("1e3", 1000)):
            self.assertEqual(quantity(text), expected)
        with self.assertRaises(ValueError):
            quantity("not-a-quantity")

    def test_full_scoped_collection(self):
        result, summary, commands, dest = self.collect(extra=("--include-nodes",))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(summary["status"], "complete")
        self.assertEqual(summary["metrics"]["pod.api-1.api.cpu"]["value"], 0.25)
        self.assertEqual(summary["metrics"]["pod.api-1.api.memory"]["value"], 64 * 1024**2)
        self.assertTrue((dest / "raw/endpointslices.json").exists())
        for command in commands:
            self.assertEqual(command[command.index("--context") + 1], "test-only")
            self.assertEqual(command[command.index("--namespace") + 1], "payments")
            self.assertIn("get", command)
            self.assertNotIn("-A", command)
        for metric in summary["metrics"].values():
            for path in metric["sources"]:
                self.assertTrue((dest / path).is_file())

    def test_missing_metrics_is_partial_not_zero(self):
        result, summary, _, dest = self.collect("missing_metrics")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(summary["status"], "partial")
        self.assertNotIn("pod.api-1.api.cpu", summary["metrics"])
        self.assertIn("pod.api-1.api.restarts", summary["metrics"])
        manifest = json.loads((dest / "manifest.json").read_text())
        self.assertTrue(any(s.get("error") == "api_unavailable" for s in manifest["sources"]))

    def test_selectorless_service_does_not_expand_scope(self):
        result, summary, commands, _ = self.collect("selectorless")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(len(commands), 1)
        self.assertFalse(summary["metrics"])

    def test_rbac_and_pod_limit_are_honored(self):
        for scenario, extra in (("rbac", ()), ("too_many", ("--max-pods", "1"))):
            with self.subTest(scenario=scenario):
                result, summary, commands, _ = self.collect(scenario, extra)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertEqual(summary["status"], "partial")
                self.assertFalse(any("pods.metrics.k8s.io" in c for c in commands))


class ComparisonTests(unittest.TestCase):
    def sample(self):
        return {"schema_version": 1, "kind": "linux", "target": {"pid": 1}, "status": "complete", "facts": {},
                "metrics": {"a": {"value": 0, "unit": "cores", "sources": ["raw/a"]},
                            "b": {"value": 100, "unit": "bytes", "sources": ["raw/b"]}}}

    def test_descriptive_delta_and_zero_baseline(self):
        a, b = self.sample(), self.sample()
        b["metrics"]["a"]["value"] = 1
        b["metrics"]["b"]["value"] = 50
        report = compare(a, b)
        self.assertEqual(report["assessment"], "descriptive_only")
        self.assertIsNone(report["metrics"][0]["relative_change_percent"])
        self.assertEqual(report["metrics"][1]["relative_change_percent"], -50)
        self.assertNotIn("improved", report)

    def test_pod_replacement_and_reused_metrics_are_flagged(self):
        a = self.sample()
        a.update(kind="kubernetes", facts={"pods": [{"metadata": {"name": "api", "uid": "old"}}],
                                          "metrics_windows": [{"pod": "api", "timestamp": "same", "window": "30s"}]})
        b = copy.deepcopy(a)
        b["facts"]["pods"][0]["metadata"]["uid"] = "new"
        report = compare(a, b)
        self.assertTrue(any("identities changed" in s for s in report["comparability_issues"]))
        self.assertTrue(any("Reused" in s for s in report["comparability_issues"]))


if __name__ == "__main__":
    unittest.main()
