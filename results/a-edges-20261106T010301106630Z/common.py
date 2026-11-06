"""Small, auditable helpers for CPU-only feasibility probes."""
import datetime as dt
import json
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
IMAGE = "course-feasibility:2026-11-06"


def new_run(kind):
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    path = ROOT / "results" / f"{kind}-{stamp}"
    path.mkdir(parents=True)
    return path


class Commands:
    def __init__(self, directory):
        self.directory = directory
        self.events = []

    def run(self, argv, *, timeout=30, check=True, cwd=None):
        start = time.monotonic()
        try:
            result = subprocess.run(argv, text=True, capture_output=True, timeout=timeout,
                                    cwd=cwd)
            event = dict(argv=list(map(str, argv)), returncode=result.returncode,
                         stdout=result.stdout, stderr=result.stderr, timed_out=False)
        except subprocess.TimeoutExpired as exc:
            def decode(value):
                return value.decode(errors="replace") if isinstance(value, bytes) else value or ""
            event = dict(argv=list(map(str, argv)), returncode=124,
                         stdout=decode(exc.stdout), stderr=decode(exc.stderr), timed_out=True)
        event["seconds"] = time.monotonic() - start
        event["utc"] = dt.datetime.now(dt.timezone.utc).isoformat()
        self.events.append(event)
        with (self.directory / "commands.jsonl").open("a") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
        if check and event["returncode"] != 0:
            raise RuntimeError(f"Command failed: {argv!r}: {event['stderr'][-2000:]}")
        return event

    def docker(self, *args, **kwargs):
        return self.run(["docker", *map(str, args)], **kwargs)


def save_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
