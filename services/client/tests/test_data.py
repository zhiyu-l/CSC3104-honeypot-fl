"""Tests for cumulative training data: base CSV + verified nightly batches."""

import csv
import hashlib
import json

import pytest

from fl_client.data import load_cumulative, verify_batch
from fl_client.task import load_org_data
from fl_common import FEATURE_NAMES

HEADER = ["honeypot", "session_id", "src_ip", "start", "label", *FEATURE_NAMES]


def row(sid, label="brute_force", honeypot="orgA", **feat):
    r = {"honeypot": honeypot, "session_id": sid, "src_ip": "1.2.3.4", "start": "2026-11-08T00:00:00Z",
         "label": label, **{k: 0 for k in FEATURE_NAMES}}
    r["n_login_failed"] = 2
    r.update(feat)
    return r


def write_csv(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=HEADER)
        w.writeheader()
        w.writerows(rows)


def write_batch(root, bid, rows, *, manifest=True, schema=2, bad_hash=False, rows_in_manifest=None):
    d = root / bid
    write_csv(d / "sessions.csv", rows)
    if manifest:
        sha = hashlib.sha256((d / "sessions.csv").read_bytes()).hexdigest()
        (d / "manifest.json").write_text(json.dumps({
            "schema_version": schema,
            "csv_sha256": "0" * 64 if bad_hash else sha,
            "rows": len(rows) if rows_in_manifest is None else rows_in_manifest,
        }))
    return d


@pytest.fixture
def base(tmp_path):
    p = tmp_path / "base.csv"
    write_csv(p, [row(f"kaggle:{i}") for i in range(10)])
    return p


def test_base_only(base, tmp_path):
    X, y, s = load_cumulative(str(base), cache_dir=tmp_path / "cache")
    assert len(y) == 10 and s.n_base == 10 and s.batches == [] and s.source == "base-only"


def test_batches_are_added_and_duplicates_dropped(base, tmp_path):
    batches = tmp_path / "sessions"
    write_batch(batches, "b1", [row("orgA:pod:1"), row("orgA:pod:2", label="recon", n_commands=3)])
    # b2 repeats a session from b1 and one from the base CSV
    write_batch(batches, "b2", [row("orgA:pod:2"), row("kaggle:3"), row("orgA:pod:3")])
    X, y, s = load_cumulative(str(base), str(batches), cache_dir=tmp_path / "cache", run_key="1")
    assert s.batches == ["b1", "b2"]
    assert s.n_base == 10 and s.n_batch_rows == 3 and s.n_duplicates == 2
    assert len(y) == 13 and s.source == "fresh"


def test_incomplete_and_tampered_batches_never_used(base, tmp_path):
    batches = tmp_path / "sessions"
    write_batch(batches, "good", [row("orgA:pod:1")])
    write_batch(batches, "uploading", [row("orgA:pod:2")], manifest=False)
    write_batch(batches, "tampered", [row("orgA:pod:3")], bad_hash=True)
    write_batch(batches, "short", [row("orgA:pod:4")], rows_in_manifest=5)
    write_batch(batches, "oldschema", [row("orgA:pod:5")], schema=1)
    X, y, s = load_cumulative(str(base), str(batches), cache_dir=tmp_path / "cache", run_key="1")
    assert s.batches == ["good"]
    assert sorted(s.bad_batches) == ["oldschema", "short", "tampered"]   # "uploading" isn't a batch yet
    assert len(y) == 11


def test_held_out_sessions_are_excluded(base, tmp_path):
    batches = tmp_path / "sessions"
    write_batch(batches, "b1", [row("orgA:uid-1:aaa"), row("orgA:uid-1:bbb"), row("orgA:uid-2:ccc")])
    sel = tmp_path / "replay-selection.json"
    sel.write_text(json.dumps({"organisation": "orga", "sessions": [
        {"pod_uid": "uid-1", "session_id": "aaa"},          # Jia Ler's selection format
    ]}))
    txt = tmp_path / "more.txt"
    txt.write_text("# full CSV ids also work\norgA:uid-2:ccc\n")
    excl = tmp_path / "excl"
    excl.mkdir()
    sel.rename(excl / sel.name)
    txt.rename(excl / txt.name)
    X, y, s = load_cumulative(str(base), str(batches), str(excl), cache_dir=tmp_path / "cache", run_key="1")
    assert s.n_excluded == 2 and s.n_batch_rows == 1 and len(y) == 11


def test_same_batches_for_every_round_of_a_run(base, tmp_path):
    batches, cache = tmp_path / "sessions", tmp_path / "cache"
    write_batch(batches, "b1", [row("orgA:pod:1")])
    _, y1, s1 = load_cumulative(str(base), str(batches), cache_dir=cache, run_key="run-7")
    write_batch(batches, "b2", [row("orgA:pod:2")])  # lands mid-run
    _, y2, s2 = load_cumulative(str(base), str(batches), cache_dir=cache, run_key="run-7")
    assert s1.batches == s2.batches == ["b1"] and len(y1) == len(y2)
    _, y3, s3 = load_cumulative(str(base), str(batches), cache_dir=cache, run_key="run-8")
    assert s3.batches == ["b1", "b2"] and len(y3) == len(y1) + 1  # next run picks it up


def test_falls_back_to_cache_when_source_unreachable(base, tmp_path):
    batches, cache = tmp_path / "sessions", tmp_path / "cache"
    write_batch(batches, "b1", [row("orgA:pod:1")])
    load_cumulative(str(base), str(batches), cache_dir=cache, run_key="1")
    _, y, s = load_cumulative(str(base), str(tmp_path / "gone"), cache_dir=cache, run_key="2")
    assert s.source == "cache-fallback" and s.batches == ["b1"] and len(y) == 11


def test_cache_is_reverified(base, tmp_path):
    batches, cache = tmp_path / "sessions", tmp_path / "cache"
    write_batch(batches, "b1", [row("orgA:pod:1")])
    load_cumulative(str(base), str(batches), cache_dir=cache, run_key="1")
    with (cache / "batches" / "b1" / "sessions.csv").open("a") as f:  # corrupted on disk
        f.write("orgA,orgA:pod:9,x,x,brute_force," + ",".join("0" for _ in FEATURE_NAMES) + "\n")
    assert verify_batch(cache / "batches" / "b1") is not None
    _, y, s = load_cumulative(str(base), str(batches), cache_dir=cache, run_key="2")
    assert s.batches == ["b1"] and len(y) == 11  # downloaded again, not trusted


def test_load_org_data_reports_summary(base, tmp_path):
    batches = tmp_path / "sessions"
    write_batch(batches, "b1", [row("orgA:pod:1"), row("orgA:pod:2")])
    d = load_org_data(0, 3, str(base), batches_uri=str(batches), cache_dir=str(tmp_path / "c"), run_key="1")
    assert len(d.y_train) + len(d.y_test) == 12
    cfg = d.summary.to_config()
    assert cfg["n-base"] == 10 and cfg["n-batches"] == 1 and cfg["batches"] == "b1"


def test_s3_source(base, tmp_path, monkeypatch):
    """Same layout as the nightly job publishes: s3://<bucket>/<org>/processed/sessions/<batch>/."""
    moto = pytest.importorskip("moto")
    boto3 = pytest.importorskip("boto3")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "ap-southeast-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    with moto.mock_aws():
        s3 = boto3.client("s3")
        s3.create_bucket(Bucket="cowrie-orga", CreateBucketConfiguration={"LocationConstraint": "ap-southeast-1"})
        local = tmp_path / "staging"
        for bid, rows, kw in [("b1", [row("orgA:u:1"), row("orgA:u:2")], {}),
                              ("b2", [row("orgA:u:3")], {"bad_hash": True}),
                              ("b3", [row("orgA:u:4")], {"manifest": False})]:
            d = write_batch(local, bid, rows, **kw)
            for f in d.iterdir():
                s3.upload_file(str(f), "cowrie-orga", f"orga/processed/sessions/{bid}/{f.name}")
        (tmp_path / "sel.json").write_text(json.dumps({"sessions": [{"pod_uid": "u", "session_id": "2"}]}))
        s3.upload_file(str(tmp_path / "sel.json"), "cowrie-orga", "orga/processed/replay/selection.json")

        X, y, s = load_cumulative(
            str(base), "s3://cowrie-orga/orga/processed/sessions/",
            "s3://cowrie-orga/orga/processed/replay/selection.json",
            cache_dir=tmp_path / "cache", run_key="1",
        )
    assert s.batches == ["b1"] and s.bad_batches == ["b2"]
    assert s.n_excluded == 1 and s.n_batch_rows == 1 and len(y) == 11


def test_bootstrap_run_ignores_batches(base, tmp_path, monkeypatch):
    """A bootstrap request from the coordinator means: base CSV only, even if batches exist."""
    from types import SimpleNamespace

    from fl_client.client_app import _load

    batches = tmp_path / "sessions"
    write_batch(batches, "b1", [row("orgA:pod:1"), row("orgA:pod:2")])
    monkeypatch.setenv("FL_BATCHES_URI", str(batches))
    monkeypatch.setenv("FL_CACHE_DIR", str(tmp_path / "cache"))
    ctx = SimpleNamespace(run_config={"seed": 0}, node_config={}, run_id=1)

    boot = _load(ctx, 0, 3, str(base), {"bootstrap": True})
    assert boot.summary.source == "bootstrap" and boot.summary.batches == []
    assert len(boot.y_train) + len(boot.y_test) == 10

    normal = _load(ctx, 0, 3, str(base), {"model-version": 3})
    assert normal.summary.batches == ["b1"] and len(normal.y_train) + len(normal.y_test) == 12
