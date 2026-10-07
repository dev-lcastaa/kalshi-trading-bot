"""Append-only experiment registry (`registry.jsonl` in the event store root).

Every Phase 3 run is logged with the hypothesis, stage, data fingerprint and code commit. The registry is also how the
pre-registration is enforced: validation needs a passed screen, confirmation needs a passed validation and an explicit
flag, and every look at the holdout is recorded.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import subprocess
from pathlib import Path

STAGES = ("screen", "validate", "confirm")
# Which earlier stage must have passed first. H4 has no screen (its data does not exist before Oct 6); H1b is an
# extension of H1 and needs H1's screen.
PREREQUISITE = {
    ("H1", "validate"): ("H1", "screen"), ("H2", "validate"): ("H2", "screen"), ("H3", "validate"): ("H3", "screen"),
    ("H1b", "validate"): ("H1", "screen"),
    ("H5", "validate"): ("H5", "screen"), ("H6", "validate"): ("H6", "screen"),
    ("H5", "confirm"): ("H5", "validate"), ("H6", "confirm"): ("H6", "validate"),
    ("H7", "validate"): ("H7", "screen"), ("H7", "confirm"): ("H7", "validate"),
    ("H8", "confirm"): ("H8", "validate"),
    ("H1", "confirm"): ("H1", "validate"), ("H2", "confirm"): ("H2", "validate"), ("H3", "confirm"): ("H3", "validate"),
    ("H1b", "confirm"): ("H1b", "validate"), ("H4", "confirm"): ("H4", "validate"),
}


class RegistryError(RuntimeError):
    pass


def _path(root) -> Path:
    return Path(root) / "registry.jsonl"


def load(root) -> list[dict]:
    path = _path(root)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def spec_hash(spec: dict) -> str:
    return hashlib.sha256(json.dumps(spec, sort_keys=True, default=str).encode()).hexdigest()[:12]


def git_commit() -> str | None:
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, timeout=5)
        return out.stdout.strip() or None
    except Exception:
        return None


def passed(root, hypothesis: str, stage: str) -> bool:
    """True if the latest run of (hypothesis, stage) passed."""
    runs = [r for r in load(root) if r["hypothesis"] == hypothesis and r["stage"] == stage]
    return bool(runs) and runs[-1].get("passed") is True


def require_prerequisite(root, hypothesis: str, stage: str) -> None:
    need = PREREQUISITE.get((hypothesis, stage))
    if need and not passed(root, *need):
        raise RegistryError(f"{hypothesis} {stage} needs a passed {need[1]} for {need[0]} in the registry first "
                            f"(see docs/PHASE3_PREREGISTRATION.md)")


def log_run(root, hypothesis: str, stage: str, splits: list[str], spec: dict, results: dict,
            passed_: bool | None, data_fingerprints: dict | None = None) -> dict:
    if stage not in STAGES:
        raise ValueError(f"stage must be one of {STAGES}")
    record = {
        "ts": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"), "hypothesis": hypothesis, "stage": stage,
        "splits": splits, "spec_hash": spec_hash(spec), "spec": spec, "data": data_fingerprints or {},
        "commit": git_commit(), "results": results, "passed": passed_,
        "holdout_look": "holdout" in splits,
    }
    path = _path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, default=str) + "\n")
    return record


def holdout_looks(root) -> list[dict]:
    return [r for r in load(root) if r.get("holdout_look")]
