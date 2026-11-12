"""Private evidence bundles and bounded command execution (standard library only)."""

import datetime as dt
import json
import os
from pathlib import Path
import selectors
import signal
import subprocess
import time

VERSION = "0.1.0"
MAX_BYTES = 2 * 1024 * 1024


def utc_now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def save_json(path, data):
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
    path.chmod(0o600)


def run(argv, timeout=10, limit=MAX_BYTES, *, env=None, file_limit=None):
    """Bound both streams together; terminate the process group on timeout/overflow."""
    def limit_files():
        # Used only by Linux profiling. Set the limit in the child, not the collector.
        import resource
        _, hard = resource.getrlimit(resource.RLIMIT_FSIZE)
        cap = file_limit if hard == resource.RLIM_INFINITY else min(file_limit, hard)
        resource.setrlimit(resource.RLIMIT_FSIZE, (cap, cap))

    result = {"command": argv, "started_at": utc_now(), "status": "ok"}
    start = time.monotonic()
    chunks = {"stdout": bytearray(), "stderr": bytearray()}
    try:
        process = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   start_new_session=True,
                                   env={**os.environ, **(env or {}), "LC_ALL": "C"},
                                   preexec_fn=limit_files if file_limit is not None else None)
    except (OSError, subprocess.SubprocessError) as exc:
        return {**result, "status": "unavailable", "error": str(exc),
                "stdout": "", "stderr": "", "elapsed_seconds": time.monotonic() - start}
    try:
        with selectors.DefaultSelector() as poll:
            for name in chunks:
                poll.register(getattr(process, name), selectors.EVENT_READ, name)
            while poll.get_map():
                remaining = timeout - (time.monotonic() - start)
                if remaining <= 0:
                    result["status"] = "timeout"
                    break
                for key, _ in poll.select(min(remaining, 0.1)):
                    data = os.read(key.fileobj.fileno(), 65536)
                    if not data:
                        poll.unregister(key.fileobj)
                        continue
                    room = limit - sum(map(len, chunks.values()))
                    chunks[key.data].extend(data[:room])
                    if len(data) > room:
                        result["status"] = "output_limit"
                        break
                if result["status"] != "ok":
                    break
            if result["status"] == "ok":
                try:
                    process.wait(timeout=max(0.001, timeout - (time.monotonic() - start)))
                except subprocess.TimeoutExpired:
                    result["status"] = "timeout"
    finally:
        # Also handles interruption and descendants retaining inherited pipe handles.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()
        process.stdout.close()
        process.stderr.close()
    result.update({name: data.decode("utf-8", errors="replace") for name, data in chunks.items()})
    result.update(returncode=process.returncode, elapsed_seconds=round(time.monotonic() - start, 6))
    if result["status"] == "ok" and process.returncode != 0:
        result["status"] = "error"
    return result


class Bundle:
    def __init__(self, path, kind, target):
        self.path = Path(path).expanduser()
        self.path.mkdir(mode=0o700, parents=True, exist_ok=False)
        self.sources = []
        self.summary = {"schema_version": 1, "collector_version": VERSION, "kind": kind,
                        "target": target, "started_at": utc_now(), "status": "incomplete",
                        "metrics": {}, "facts": {}, "limitations": []}
        self.finish()

    def write(self, name, data):
        save_json(self.path / name, data)

    def read(self, path, name):
        record = {"source": str(path), "artifact": name, "observed_at": utc_now()}
        try:
            with open(path, "rb") as stream:
                content = stream.read(MAX_BYTES + 1)
            if len(content) > MAX_BYTES:
                raise ValueError("file exceeds collection limit")
            text = content.decode("utf-8", errors="replace")
            dest = self.path / name
            dest.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            dest.write_text(text)
            dest.chmod(0o600)
            record["status"] = "ok"
        except (OSError, ValueError) as exc:
            text = None
            record.update(status="unavailable", error=str(exc))
        self.sources.append(record)
        return text

    def metric(self, name, value, unit, sources):
        if value is not None:
            self.summary["metrics"][name] = {"value": round(value, 6), "unit": unit, "sources": sources}

    def note(self, message):
        if message not in self.summary["limitations"]:
            self.summary["limitations"].append(message)

    def finish(self, complete=False):
        if complete:
            self.summary["status"] = "partial" if any(s["status"] != "ok" for s in self.sources) else "complete"
        self.summary["finished_at"] = utc_now()
        self.write("manifest.json", {"schema_version": 1, "sources": self.sources})
        self.write("summary.json", self.summary)
