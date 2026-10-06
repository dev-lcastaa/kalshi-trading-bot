"""Docker healthcheck: exit 1 unless the collector wrote a fresh status file and nothing required is down.

    python -m aqlabs.collector.healthcheck [status_path]
"""
from __future__ import annotations

import json
import os
import sys
import time


def check(path: str, max_age_ms: int = 60_000) -> tuple[bool, str]:
    try:
        status = json.load(open(path))
    except (OSError, ValueError) as exc:
        return False, f"no readable status file: {exc}"
    age = int(time.time() * 1000) - status.get("ts_ms", 0)
    if age > max_age_ms:
        return False, f"status file is {age / 1000:.0f}s old"
    if status.get("dropped_rows", 0) > 0:
        return False, f"{status['dropped_rows']} rows dropped"
    down = [n for n, f in status.get("feeds", {}).items() if f.get("down")]
    if down:
        return False, "feeds down: " + ", ".join(down)
    return True, "ok"


if __name__ == "__main__":
    ok, why = check(sys.argv[1] if len(sys.argv) > 1 else os.environ.get("COLLECTOR_ROOT", "/data/eventstore") + "/collector_status.json")
    print(why)
    sys.exit(0 if ok else 1)
