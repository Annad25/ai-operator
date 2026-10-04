"""Run directory: step log (JSONL), screenshots, final report."""
import json
import time
from pathlib import Path

from .config import RUNS_DIR


def run_dir(run_id: str) -> Path:
    d = RUNS_DIR / run_id
    (d / "screens").mkdir(parents=True, exist_ok=True)
    (d / "files").mkdir(parents=True, exist_ok=True)
    return d


def log(run_id: str, event: str, **data):
    rec = {"ts": time.strftime("%H:%M:%S"), "event": event, **data}
    with open(run_dir(run_id) / "steps.jsonl", "a") as f:
        f.write(json.dumps(rec, default=str) + "\n")
    short = {k: (str(v)[:120]) for k, v in data.items()}
    print(f"  [{rec['ts']}] {event} {short}")
