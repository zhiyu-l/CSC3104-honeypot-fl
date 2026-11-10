"""Carrying the global model from one run to the next.

Each training run writes a new folder under model-dir (see checkpoints.py).
This module keeps a pointer to the model that the NEXT run should start from:

    <model-dir>/latest.json   {"run": "<run-name>", "model_version": 12, "score": 0.94, ...}

How a daily cycle uses it:

    1. The trigger submits a run with init-from='latest'.
    2. resolve_init() turns 'latest' into <model-dir>/<run>/model; the run
       starts from those weights (first ever run: no latest yet, so it starts
       from a fresh model).
    3. At the end the coordinator scores the new model on the fixed test set
       (eval-data) and calls should_promote(). Only a model that is not worse
       than the current latest (within promote-tolerance) moves the pointer.
       Every run's folder is kept either way, so any older model can be
       restored with:

           python -m fl_coordinator.publish --model-dir models --set <run-name>

Optionally (publish-s3-uri) the promoted model and latest.json are copied to
S3, e.g. s3://bucket/models/. The pointer on model-dir stays the source of
truth for the coordinator; the S3 copy is for anyone else who needs the model.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path
from typing import Any, Mapping

LATEST = "latest.json"
SCORE_KEY = "macro_f1"   # what the promotion gate compares (from fl_common.evaluation)


def read_latest(model_dir: Path) -> dict[str, Any] | None:
    path = Path(model_dir) / LATEST
    if not path.is_file():
        return None
    return json.loads(path.read_text())


def write_latest(model_dir: Path, record: Mapping[str, Any]) -> Path:
    """Atomically replace <model-dir>/latest.json."""
    model_dir = Path(model_dir)
    model_dir.mkdir(parents=True, exist_ok=True)
    data = {**record, "promoted_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    tmp = model_dir / (LATEST + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, default=str))
    os.replace(tmp, model_dir / LATEST)
    return model_dir / LATEST


def resolve_init(spec: str, model_dir: Path) -> Path | None:
    """init-from value -> model path stem to load, or None to start fresh.

    'latest'              the model latest.json points to (None if there is none yet)
    <run folder>          that folder's final model.npz
    <path>.npz            that exact checkpoint
    """
    spec = spec.strip()
    if not spec:
        return None
    if spec == "latest":
        rec = read_latest(model_dir)
        if rec is None:
            return None
        return Path(model_dir) / rec["run"] / "model"
    p = Path(spec)
    if p.is_dir():
        return p / "model"
    if p.suffix == ".npz" or p.with_suffix(".npz").is_file():
        return p.with_suffix("")
    raise FileNotFoundError(f"init-from={spec!r}: not 'latest', a run folder or a .npz checkpoint")


def should_promote(
    new_score: float | None, current: Mapping[str, Any] | None, tolerance: float = 0.0
) -> tuple[bool, str]:
    """Gate for moving latest.json. Returns (promote?, reason)."""
    if current is None:
        return True, "no previous latest model"
    old = current.get("score")
    if new_score is None:
        return True, "no eval-data configured, promoting without a check"
    if old is None:
        return True, "previous latest has no score"
    if new_score >= float(old) - tolerance:
        return True, f"{SCORE_KEY} {new_score:.4f} >= previous {float(old):.4f} - {tolerance}"
    return False, f"{SCORE_KEY} {new_score:.4f} < previous {float(old):.4f} - {tolerance}; keeping {current['run']}"


def upload_to_s3(uri: str, files: list[Path], latest: Path | None) -> list[str]:
    """Copy files to s3://bucket/prefix/<run>/ and latest.json to s3://bucket/prefix/latest.json."""
    import boto3  # only needed when publish-s3-uri is set

    if not uri.startswith("s3://"):
        raise ValueError(f"publish-s3-uri must start with s3://, got {uri!r}")
    bucket, _, prefix = uri[5:].partition("/")
    prefix = prefix.strip("/")
    s3 = boto3.client("s3")
    keys = []
    for f in files:
        key = "/".join(x for x in (prefix, f.parent.name, f.name) if x)
        s3.upload_file(str(f), bucket, key)
        keys.append(key)
    if latest is not None:  # last, so readers never see a pointer to files not yet uploaded
        key = "/".join(x for x in (prefix, LATEST) if x)
        s3.upload_file(str(latest), bucket, key)
        keys.append(key)
    return [f"s3://{bucket}/{k}" for k in keys]


def main() -> None:
    ap = argparse.ArgumentParser(description="Show or move the latest-model pointer.")
    ap.add_argument("--model-dir", type=Path, default=Path(os.environ.get("FL_MODEL_DIR") or "models"))
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--show", action="store_true", help="print latest.json")
    g.add_argument("--set", metavar="RUN", help="point latest at this run folder (rollback)")
    a = ap.parse_args()
    if a.show:
        print(json.dumps(read_latest(a.model_dir), indent=2))
        return
    run_dir = a.model_dir / a.set
    meta = run_dir / "model.json"
    if not meta.is_file():
        raise SystemExit(f"{run_dir} has no final model.json")
    info = json.loads(meta.read_text())
    report = run_dir / "eval_report.json"
    score = json.loads(report.read_text())["overall"][SCORE_KEY] if report.is_file() else None
    path = write_latest(a.model_dir, {"run": a.set, "model_version": info.get("model_version"),
                                      "score": score, "reason": "set by hand"})
    print(f"{path} now points to {a.set}")


if __name__ == "__main__":
    main()
