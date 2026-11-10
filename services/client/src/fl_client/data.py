"""Cumulative training data for one org: the Kaggle base CSV plus every nightly batch.

Where the data comes from (env var, or the node-config key in brackets):

    FL_DATA_PATH     (data-path)     base CSV: this org's Kaggle training split.
                                     On Kubernetes the init container copies it
                                     from s3://$S3_BUCKET/$S3_FEATURES_KEY.
    FL_BATCHES_URI   (batches-uri)   where the nightly log processor publishes
                                     batches: s3://<bucket>/<org>/processed/sessions/
                                     or a local folder with the same layout.
                                     Unset = train on the base CSV only.
    FL_EXCLUDE_URI   (exclude-uri)   sessions that must never be trained on (the
                                     held-out replay test set): a replay selection
                                     .json, a .txt with one session ID per line,
                                     or a folder / s3:// prefix of those files.
    FL_CACHE_DIR     (cache-dir)     where verified batches are kept between runs
                                     (default /tmp/fl-cache).

A batch is <batches-uri>/<batch-id>/sessions.csv + manifest.json (written by
services/log-processor/nightly.py). It is used only if the manifest exists, the
CSV's SHA-256 and row count match it, and its schema_version is ours; anything
else is skipped with a warning (half-uploaded or tampered batches never reach
training).

Once per training run the client fixes which batches it uses (a snapshot file
keyed by the Flower run id), so every round of a run trains on the same data
even if a new batch lands mid-run. If S3 can't be reached, it falls back to the
batches already verified in the cache (and to the base CSV alone if there are
none) instead of failing the round.

Rows from all sources are de-duplicated by session_id (first one wins), then go
through the same validate_row + label rules as before.
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

import numpy as np

from fl_common import FEATURE_NAMES, SCHEMA_VERSION, label_session, parse_label, validate_row

CSV_NAME, MANIFEST_NAME = "sessions.csv", "manifest.json"


@dataclass
class DataSummary:
    """What a client trained on. Sent to the coordinator and kept in history.json."""

    n_base: int = 0                  # usable rows from the base CSV
    n_batch_rows: int = 0            # usable rows from batches (after de-duplication)
    n_duplicates: int = 0
    n_excluded: int = 0              # held-out test sessions dropped
    n_skipped: int = 0               # invalid rows or scan/tunnel
    batches: list[str] = field(default_factory=list)
    bad_batches: list[str] = field(default_factory=list)
    source: str = "base-only"        # base-only | fresh | cache-fallback

    def to_config(self) -> dict[str, int | str]:
        return {
            "n-base": self.n_base,
            "n-batch-rows": self.n_batch_rows,
            "n-duplicates": self.n_duplicates,
            "n-excluded": self.n_excluded,
            "n-skipped": self.n_skipped,
            "n-batches": len(self.batches),
            "batches": ",".join(self.batches),
            "bad-batches": ",".join(self.bad_batches),
            "source": self.source,
        }


# ---- where files live: local folder or S3 ------------------------------------


class _Store:
    """Tiny read-only view over a local folder or an s3://bucket/prefix."""

    def __init__(self, uri: str) -> None:
        self.uri = uri.rstrip("/")
        self.s3 = self.uri.startswith("s3://")
        if self.s3:
            import boto3  # only needed for S3 sources

            self.bucket, _, prefix = self.uri[5:].partition("/")
            self.prefix = prefix.strip("/")
            self.client = boto3.client("s3")

    def list(self) -> list[str]:
        """Relative paths of every file under the uri."""
        if not self.s3:
            root = Path(self.uri)
            if root.is_file():
                return [root.name]
            if not root.is_dir():  # unreachable, not "empty": let the caller fall back
                raise FileNotFoundError(f"{root} does not exist")
            return sorted(str(p.relative_to(root)).replace(os.sep, "/") for p in root.rglob("*") if p.is_file())
        out, base = [], (self.prefix + "/") if self.prefix else ""
        for page in self.client.get_paginator("list_objects_v2").paginate(Bucket=self.bucket, Prefix=base):
            out += [o["Key"][len(base):] for o in page.get("Contents", [])]
        return sorted(out)

    def fetch(self, rel: str, dest: Path) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".part")
        if self.s3:
            key = f"{self.prefix}/{rel}" if self.prefix else rel
            self.client.download_file(self.bucket, key, str(tmp))
        else:
            root = Path(self.uri)
            shutil.copyfile(root if root.is_file() else root / rel, tmp)
        os.replace(tmp, dest)


# ---- batches -----------------------------------------------------------------


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def verify_batch(folder: Path) -> str | None:
    """None if the batch folder is complete and intact, else the reason it isn't."""
    csv_path, man_path = folder / CSV_NAME, folder / MANIFEST_NAME
    if not man_path.is_file():
        return "no manifest (incomplete batch)"
    if not csv_path.is_file():
        return "manifest without sessions.csv"
    try:
        man = json.loads(man_path.read_text())
    except json.JSONDecodeError:
        return "unreadable manifest"
    if man.get("schema_version") != SCHEMA_VERSION:
        return f"schema_version {man.get('schema_version')} != {SCHEMA_VERSION}"
    if man.get("csv_sha256", "").lower() != _sha256(csv_path):
        return "CSV checksum mismatch"
    with csv_path.open(newline="") as f:
        rows = sum(1 for _ in csv.DictReader(f))
    if rows != man.get("rows"):
        return f"row count {rows} != manifest {man.get('rows')}"
    return None


def _batch_ids(files: Iterable[str]) -> list[str]:
    """Batch folders that have BOTH files (others are still uploading or broken)."""
    have: dict[str, set[str]] = {}
    for rel in files:
        parts = rel.split("/")
        if len(parts) == 2 and parts[1] in (CSV_NAME, MANIFEST_NAME):
            have.setdefault(parts[0], set()).add(parts[1])
    return sorted(b for b, names in have.items() if names == {CSV_NAME, MANIFEST_NAME})


def sync_batches(uri: str, cache_dir: Path, summary: DataSummary) -> list[str]:
    """Download (once) and verify every complete batch. Returns verified batch ids.

    Raises on connection errors so the caller can fall back to the cache.
    """
    store = _Store(uri)
    ids = _batch_ids(store.list())
    good = []
    for bid in ids:
        dest = cache_dir / "batches" / bid
        if verify_batch(dest) is None:   # already downloaded and verified earlier
            good.append(bid)
            continue
        for name in (MANIFEST_NAME, CSV_NAME):
            store.fetch(f"{bid}/{name}", dest / name)
        problem = verify_batch(dest)
        if problem is None:
            good.append(bid)
        else:
            print(f"WARNING: skipping batch {bid}: {problem}")
            summary.bad_batches.append(bid)
            shutil.rmtree(dest, ignore_errors=True)
    return good


def cached_batches(cache_dir: Path) -> list[str]:
    root = cache_dir / "batches"
    if not root.is_dir():
        return []
    return sorted(p.name for p in root.iterdir() if p.is_dir() and verify_batch(p) is None)


# ---- held-out sessions -------------------------------------------------------


def _ids_from_file(path: Path) -> set[str]:
    """Session IDs from a replay selection .json or a .txt (one ID per line)."""
    if path.suffix == ".json":
        data = json.loads(path.read_text())
        items = data.get("sessions", []) if isinstance(data, dict) else data
        out = set()
        for it in items:
            if isinstance(it, str):
                out.add(it)
            elif isinstance(it, dict):
                if it.get("output_session_id"):
                    out.add(str(it["output_session_id"]))
                if it.get("pod_uid") and it.get("session_id"):
                    out.add(f"{it['pod_uid']}:{it['session_id']}")
        return out
    return {ln.strip() for ln in path.read_text().splitlines() if ln.strip() and not ln.startswith("#")}


def load_exclusions(uri: str | None, cache_dir: Path) -> set[str]:
    """All excluded session IDs. Fails loudly: training on the test set must not
    happen silently, so a configured but unreadable exclusion list stops the round."""
    if not uri:
        return set()
    if uri.startswith("s3://") and uri.endswith((".json", ".txt")):
        parent, name = uri.rsplit("/", 1)   # one object: list its folder, fetch just it
        store, files = _Store(parent), [name]
    else:
        store = _Store(uri)
        files = store.list()
    ids: set[str] = set()
    for rel in files:
        if not rel.endswith((".json", ".txt")):
            continue
        dest = cache_dir / "exclude" / rel
        store.fetch(rel, dest)
        ids |= _ids_from_file(dest)
    return ids


def is_excluded(session_id: str, excluded: set[str]) -> bool:
    """Match the full CSV id (orgA:<pod-uid>:<cowrie-id>) or the id without the org prefix."""
    if not excluded or not session_id:
        return False
    return session_id in excluded or session_id.split(":", 1)[-1] in excluded


# ---- reading rows ------------------------------------------------------------


def _read(path: Path, seen: set[str], excluded: set[str], summary: DataSummary, X: list, y: list) -> int:
    used = 0
    with path.open(newline="") as f:
        for raw in csv.DictReader(f):
            sid = (raw.get("session_id") or "").strip()
            if sid and sid in seen:
                summary.n_duplicates += 1
                continue
            if is_excluded(sid, excluded):
                summary.n_excluded += 1
                continue
            try:
                row = {k: float(raw[k]) for k in FEATURE_NAMES}
                values = validate_row(row)
                label = raw.get("label")
                cls = parse_label(label) if label not in (None, "") else label_session(row)
            except (KeyError, TypeError, ValueError):
                summary.n_skipped += 1
                continue
            if cls is None:
                summary.n_skipped += 1
                continue
            if sid:
                seen.add(sid)
            X.append(values)
            y.append(int(cls))
            used += 1
    return used


def _run_batches(uri: str, cache_dir: Path, run_key: str | None, summary: DataSummary) -> list[str]:
    """Batch ids for this run: fixed at the first call of a run, reused after."""
    snap = cache_dir / "runs" / f"{run_key}.json" if run_key else None
    if snap is not None and snap.is_file():
        data = json.loads(snap.read_text())
        summary.source = data["source"]
        summary.bad_batches = data.get("bad_batches", [])
        return [b for b in data["batches"] if verify_batch(cache_dir / "batches" / b) is None]
    try:
        ids = sync_batches(uri, cache_dir, summary)
        summary.source = "fresh"
    except Exception as exc:  # noqa: BLE001  (network, credentials, throttling...)
        ids = cached_batches(cache_dir)
        summary.source = "cache-fallback"
        print(f"WARNING: could not read batches from {uri} ({exc}); "
              f"using {len(ids)} batch(es) already in the cache")
    if snap is not None:
        snap.parent.mkdir(parents=True, exist_ok=True)
        snap.write_text(json.dumps({"batches": ids, "source": summary.source, "bad_batches": summary.bad_batches}))
    return ids


def load_cumulative(
    base_path: str | None,
    batches_uri: str | None = None,
    exclude_uri: str | None = None,
    cache_dir: str | os.PathLike | None = None,
    run_key: str | None = None,
) -> tuple[np.ndarray, np.ndarray, DataSummary]:
    """Base CSV + verified batches, de-duplicated, minus excluded sessions."""
    cache = Path(cache_dir or os.environ.get("FL_CACHE_DIR") or "/tmp/fl-cache")
    summary = DataSummary()
    excluded = load_exclusions(exclude_uri, cache)
    X: list = []
    y: list = []
    seen: set[str] = set()
    if base_path:
        p = Path(base_path)
        if not p.is_file():
            raise FileNotFoundError(f"data file not found: {p}")
        summary.n_base = _read(p, seen, excluded, summary, X, y)
    if batches_uri:
        summary.batches = _run_batches(batches_uri, cache, run_key, summary)
        for bid in summary.batches:
            summary.n_batch_rows += _read(cache / "batches" / bid / CSV_NAME, seen, excluded, summary, X, y)
    if not X:
        raise ValueError("no valid training rows (base CSV and batches are empty or all excluded)")
    return np.asarray(X, dtype=np.float32), np.asarray(y, dtype=np.int64), summary


__all__ = ["DataSummary", "is_excluded", "load_cumulative", "load_exclusions", "sync_batches", "verify_batch"]
