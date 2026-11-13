"""Split Jia Ler's replays into a held-out test set and a training stream (option B).

Jia Ler's replayer re-sends the 336 Kaggle "replay" sessions (20% split, see
split_orgs.py) through each org's Cowrie. Every replayed SSH session carries a
tag in its client version string:

    SSH-2.0-csc3104_full_<org>_<original session id>_<run id>

so each replay maps exactly to the original session it came from. The same
originals are replayed many times (several runs), so without care the same
attack would end up in both training and testing.

What this script does
  1. Splits the 336 originals ONCE into two halves, saved to <out>/halves.csv
     and reused on every later run so the split never moves:
       test   -> only ever used for evaluation
       stream -> allowed into training through the nightly batches
     Sessions sharing an attacker IP or identical behaviour (same logins and
     commands) always land in the same half, and each half keeps roughly 50% of
     every org/label cell.
  2. Per org, syncs s3://<bucket>/<org>/raw/by-pod/ to <out>/raw/<org>/ and
     finds every tagged session.
  3. Writes <out>/<org>/exclude/replay-sessions.txt (pod_uid:session_id per line,
     for FL_EXCLUDE_URI). Excluded are
       - every replay of a test-half original (any run),
       - every copy of a stream-half original except the latest complete one,
       - every other csc3104 tag (sim/recon test traffic, unknown formats).
  4. Builds the test set from the latest complete FINAL-run replay of each
     test-half original, using services/log-processor/process_replay.process()
     (the exact extractor behind the nightly training batches):
       <out>/<org>/test/{sessions.csv,manifest.json} and <out>/replay_test.csv
  5. Also processes the kept stream copies into <out>/<org>/stream-preview/
     (local report only; training gets them from the nightly batches).
  6. Re-reads what it wrote and checks it. Any failed check stops before upload.

With --upload it copies
  exclude list -> s3://<bucket>/<org>/processed/exclude/replay-sessions.txt
  test set     -> s3://<bucket>/<org>/processed/replay-test/{sessions.csv,manifest.json}
Nothing but the .txt goes under processed/exclude/, because the client parses
every .json/.txt it finds there.

Run it after the nightly processor and before every training cycle: replays
sent after the last run are not in the exclusion list yet.

Usage (repo root, anywhere with the aws CLI and credentials, e.g. CloudShell):
    python3 tools/cowrie_dataset/replay_testset.py inspect
    python3 tools/cowrie_dataset/replay_testset.py build             # local only
    python3 tools/cowrie_dataset/replay_testset.py build --no-sync --upload

Standard library only (plus the aws CLI for S3).
"""
from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
import re
import shutil
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools/cowrie_dataset"))
sys.path.insert(0, str(ROOT / "services/log-processor"))
sys.path.insert(0, str(ROOT / "libs/fl-common/src"))
import cowrie_sessions as extractor  # noqa: E402
from process_replay import POD_UID, ORG_NAMES, process  # noqa: E402

ORGS = ["orga", "orgb", "orgc"]
FINAL_RUNS = ["1249b5ca5b62408eb22b50a537a937c1", "4e5faba17f3241a28a81920e39a37312"]
BUCKET = "csc3104-cowrie-{org}-909891185696"
SPLIT_DIR = ROOT / "data/processed/kaggle/split"
SEED = "csc3104-replay-halves-v1"
TEST_RUN_ID = "replay-test"
STREAM_RUN_ID = "replay-stream"
MIN_TEST_PER_CLASS = 7

FULL_TAG = re.compile(r"csc3104_full_(org[abc])_([0-9a-f]{12})_([0-9a-f]{32})(?![0-9a-f])")
ANY_TAG = re.compile(r"csc3104_([a-z]+)_")
LOGINS = ("cowrie.login.success", "cowrie.login.failed")
HALVES_FIELDS = ["org", "original_id", "source_session_id", "src_ip", "label", "fingerprint", "group", "half"]


# ---- S3 ----------------------------------------------------------------------

def aws(*args: str) -> None:
    cmd = ["aws", *args]
    print("  $ " + " ".join(cmd), flush=True)
    subprocess.run(cmd, check=True)


def sync_raw(org: str, bucket: str, dest: Path, region: str) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    aws("s3", "sync", f"s3://{bucket}/{org}/raw/by-pod/", str(dest), "--region", region,
        "--only-show-errors", "--exclude", "*cowrie.json.pipeline-test*")


# ---- reading logs ------------------------------------------------------------

def iter_events(path: Path):
    opener = gzip.open if path.name.endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue  # active file can end in a partial line
            if isinstance(e, dict) and isinstance(e.get("session"), str):
                yield e


def fingerprint(events: list[dict]) -> str:
    """Behaviour of a session: its login attempts and commands, in order."""
    steps = []
    for e in sorted(events, key=lambda e: e.get("timestamp", "")):
        ev = e.get("eventid")
        if ev in LOGINS:
            steps.append(["L", ev.endswith("success"), e.get("username"), e.get("password")])
        elif ev == "cowrie.command.input":
            steps.append(["C", e.get("input")])
    return hashlib.sha256(json.dumps(steps).encode()).hexdigest()[:16]


def scan_org(raw: Path) -> dict:
    """Every csc3104-tagged session in one org's raw logs.

    sessions[(pod_uid, session_id)] = {kind, tag_org, orig, run, version, complete, start}
    kind is "full" (Kaggle replay) or whatever word follows csc3104_ (sim, recon, ...).
    """
    sessions: dict[tuple[str, str], dict] = {}
    flags: dict[tuple[str, str], set] = defaultdict(set)
    start: dict[tuple[str, str], str] = {}
    other_versions = Counter()
    for pod in sorted(p for p in raw.iterdir() if p.is_dir() and POD_UID.fullmatch(p.name)):
        for path in map(Path, extractor.log_files(str(pod))):
            if path.name.startswith("cowrie.json.pipeline-test"):
                continue
            for e in iter_events(path):
                key = (pod.name, e["session"])
                ev = e.get("eventid")
                if ev == "cowrie.client.version":
                    v = str(e.get("version", ""))
                    if "csc3104_" not in v:
                        other_versions[v] += 1
                        continue
                    m = FULL_TAG.search(v)
                    if m:
                        info = {"kind": "full", "tag_org": m.group(1), "orig": m.group(2), "run": m.group(3)}
                    else:
                        k = ANY_TAG.search(v)
                        info = {"kind": k.group(1) if k else "unknown", "tag_org": None, "orig": None, "run": None}
                    info["version"] = v
                    sessions.setdefault(key, info)
                elif ev == "cowrie.session.connect":
                    flags[key].add("connect")
                    ts = e.get("timestamp", "")
                    start[key] = min(start.get(key, ts), ts)
                elif ev == "cowrie.session.closed":
                    flags[key].add("closed")
    for key, info in sessions.items():
        info["complete"] = flags[key] >= {"connect", "closed"}
        info["start"] = start.get(key, "")
    return {"sessions": sessions, "other_versions": other_versions}


# ---- originals and the test/stream split ---------------------------------------

def load_originals(split_dir: Path) -> dict:
    """The 336 Kaggle replay originals: (org, original_id) -> details + fingerprint."""
    originals = {}
    with (split_dir / "manifest.csv").open(newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if r["split"] != "replay":
                continue
            key = (r["org"].lower(), r["session_id"].split(":")[-1])
            originals[key] = {"source": r["session_id"], "src_ip": r["src_ip"], "label": r["label"]}
    events = defaultdict(list)
    for org in ORGS:
        for e in iter_events(split_dir / "replay" / ORG_NAMES[org] / "cowrie.json"):
            events[(org, e["session"])].append(e)
    missing = [k for k in originals if not events.get(k)]
    if missing:
        sys.exit(f"{len(missing)} originals have no raw events in {split_dir}/replay, e.g. {missing[:3]}")
    for key, o in originals.items():
        o["fp"] = fingerprint(events[key])
    return originals


def make_halves(originals: dict, seed: str) -> dict:
    """Assign every original to test or stream.

    Originals sharing an attacker IP or a fingerprint form one group, and a group
    always goes to one half (so neither the same attacker nor the same behaviour
    is on both sides). Groups are placed largest first into whichever half is
    further below 50% of the org/label cells the group belongs to.
    """
    parent = {k: k for k in originals}

    def find(k):
        while parent[k] != k:
            parent[k] = parent[parent[k]]
            k = parent[k]
        return k

    for field in ("src_ip", "fp"):
        first = {}
        for k in sorted(originals):
            v = originals[k][field]
            if v in first:
                parent[find(k)] = find(first[v])
            else:
                first[v] = k
    groups = defaultdict(list)
    for k in originals:
        groups[find(k)].append(k)

    def h(s: str) -> str:
        return hashlib.sha256(f"{seed}:{s}".encode()).hexdigest()

    cell = lambda k: (k[0], originals[k]["label"])  # noqa: E731
    target = Counter(cell(k) for k in originals)
    placed = {"test": Counter(), "stream": Counter()}
    rows = {}
    for g in sorted(groups.values(), key=lambda g: (-len(g), h(":".join(min(g))))):
        need = Counter(cell(k) for k in g)
        gap = {side: sum(n * (target[c] / 2 - placed[side][c]) for c, n in need.items()) for side in placed}
        gid = h(":".join(min(g)))[:8]
        if gap["test"] != gap["stream"]:
            side = "test" if gap["test"] > gap["stream"] else "stream"
        else:
            side = "test" if int(gid, 16) % 2 == 0 else "stream"
        placed[side].update(need)
        for k in g:
            rows[k] = {"group": gid, "half": side}
    return rows


def get_halves(path: Path, originals: dict, resplit: bool) -> dict:
    """Load halves.csv, or create it if it doesn't exist (or --resplit)."""
    if path.exists() and not resplit:
        with path.open(newline="", encoding="utf-8") as f:
            halves = {(r["org"], r["original_id"]): r["half"] for r in csv.DictReader(f)}
        if set(halves) != set(originals) or set(halves.values()) - {"test", "stream"}:
            sys.exit(f"{path} does not match the replay manifest. Fix it, or rebuild with --resplit "
                     "(only before anything was trained on the stream half).")
        print(f"Using existing split {path}")
        return halves
    rows = make_halves(originals, SEED)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=HALVES_FIELDS)
        w.writeheader()
        for (org, oid), o in sorted(originals.items()):
            w.writerow({"org": org, "original_id": oid, "source_session_id": o["source"],
                        "src_ip": o["src_ip"], "label": o["label"], "fingerprint": o["fp"],
                        "group": rows[(org, oid)]["group"], "half": rows[(org, oid)]["half"]})
    print(f"Created split {path}")
    return {k: v["half"] for k, v in rows.items()}


def check_halves(originals: dict, halves: dict) -> list[str]:
    fails = []
    for field in ("src_ip", "fp"):
        sides = defaultdict(set)
        for k, side in halves.items():
            sides[originals[k][field]].add(side)
        both = [v for v, s in sides.items() if len(s) > 1]
        if both:
            fails.append(f"{len(both)} {field} values appear in both halves")
    return fails


# ---- commands ----------------------------------------------------------------

def prepare(args) -> dict:
    results = {}
    for org in args.orgs:
        raw = args.out / "raw" / org
        if args.sync:
            print(f"[{org}] syncing raw logs", flush=True)
            sync_raw(org, args.bucket.format(org=org), raw, args.region)
        if not raw.is_dir():
            sys.exit(f"{raw} does not exist; run without --no-sync first")
        print(f"[{org}] scanning {raw}", flush=True)
        results[org] = scan_org(raw)
    return results


def cmd_inspect(args) -> None:
    originals = load_originals(args.split_dir)
    results = prepare(args)
    for org, r in results.items():
        s = r["sessions"]
        full = [v for v in s.values() if v["kind"] == "full"]
        mapped = [v for v in full if v["tag_org"] == org and (org, v["orig"]) in originals]
        final = {v["orig"] for v in mapped if v["run"] in args.final_runs and v["complete"]}
        n_orig = sum(1 for k in originals if k[0] == org)
        print(f"\n== {org}: {len(s)} tagged sessions by kind {dict(Counter(v['kind'] for v in s.values()))}")
        print(f"  full replays mapped to an original: {len(mapped)}/{len(full)}, "
              f"complete {sum(v['complete'] for v in mapped)}")
        print(f"  originals covered by a complete final-run replay: {len(final)}/{n_orig}")
        for run, n in Counter(v["run"] for v in full).most_common():
            print(f"  run {run}: {n}{'  <- final' if run in args.final_runs else ''}")
        print("  most common UNtagged version strings (should be real clients, not replays):")
        for v, n in r["other_versions"].most_common(5):
            print(f"    {n:>6}  {v}")


def run_process(org, raw, picks, originals, org_dir, run_id, out_dir) -> dict | None:
    if out_dir.exists():
        shutil.rmtree(out_dir)  # only our own generated output folder
    if not picks:
        return None
    selection = {
        "manifest_version": 1, "organisation": org, "run_id": run_id,
        "sessions": [{"pod_uid": u, "session_id": s,
                      "source_session_id": originals[(org, oid)]["source"],
                      "expected_label": originals[(org, oid)]["label"]}
                     for (u, s), oid in sorted(picks.items())],
    }
    sel_path = org_dir / f"{run_id}-selection.json"
    sel_path.write_text(json.dumps(selection, indent=2) + "\n", encoding="utf-8")
    return process(raw, sel_path, out_dir, org, run_id)


def label_report(report: dict | None) -> dict:
    if not report:
        return {"rows": 0, "labels": {}, "matches_original": 0, "changed": {}}
    changed = Counter(f"{s['expected_label']}->{s['observed_label']}" for s in report["sessions"]
                      if not s.get("label_matches_expectation"))
    return {"rows": report["rows"], "labels": report["label_counts"],
            "matches_original": sum(1 for s in report["sessions"] if s.get("label_matches_expectation")),
            "changed": dict(changed)}


def verify_org(org: str, sessions: dict, halves: dict, excl_path: Path, keep: dict, test: dict) -> list[str]:
    """Re-read the written exclusion list and check it against the rules."""
    excluded = {line.strip() for line in excl_path.read_text(encoding="utf-8").splitlines()
                if line.strip() and not line.startswith("#")}
    fails = []
    allowed = {f"{u}:{s}" for (u, s) in sessions} - excluded
    for key in allowed:
        v = sessions[tuple(key.split(":", 1))]
        if v["kind"] != "full":
            fails.append(f"{org}: non-Kaggle tagged session {key} is not excluded")
        elif halves.get((org, v["orig"])) != "stream":
            fails.append(f"{org}: {key} (original {v['orig']}, not stream) is not excluded")
    per_orig = Counter(sessions[tuple(k.split(":", 1))]["orig"] for k in allowed)
    dup = [o for o, n in per_orig.items() if n > 1]
    if dup:
        fails.append(f"{org}: {len(dup)} stream originals have more than one training copy")
    for (u, s), oid in test.items():
        if f"{u}:{s}" not in excluded or halves[(org, oid)] != "test":
            fails.append(f"{org}: test session {u}:{s} is trainable or not a test original")
    if set(keep) & set(test):
        fails.append(f"{org}: a session is both kept for training and in the test set")
    return fails


def cmd_build(args) -> None:
    originals = load_originals(args.split_dir)
    halves = get_halves(args.out / "halves.csv", originals, args.resplit)
    failures = check_halves(originals, halves)
    test_cells = Counter((k[0], originals[k]["label"]) for k, side in halves.items() if side == "test")
    warnings = [f"{org}/{label}: only {n} test originals" for (org, label), n in sorted(test_cells.items())
                if n < MIN_TEST_PER_CLASS]

    results = prepare(args)
    summary = {"final_runs": args.final_runs, "split_file": str(args.out / "halves.csv"),
               "halves": dict(Counter(halves.values())),
               "test_cells": {f"{o}/{lab}": n for (o, lab), n in sorted(test_cells.items())},
               "orgs": {}}
    test_csvs = []
    for org, r in results.items():
        S = r["sessions"]
        org_dir = args.out / org
        org_dir.mkdir(parents=True, exist_ok=True)
        problems, copies = Counter(), defaultdict(list)
        for k, v in S.items():
            if v["kind"] != "full":
                problems[f"other tag: {v['kind']}"] += 1
            elif v["tag_org"] != org:
                problems["tag org mismatch"] += 1
            elif (org, v["orig"]) not in originals:
                problems["unknown original"] += 1
            elif not v["complete"]:
                problems["incomplete session"] += 1
            else:
                copies[v["orig"]].append(k)

        def latest(keys):
            return max(keys, key=lambda k: (S[k]["start"], k))

        keep, test, no_stream, no_test = {}, {}, [], []
        for (o, oid), side in sorted(halves.items()):
            if o != org:
                continue
            if side == "stream":
                if copies.get(oid):
                    keep[latest(copies[oid])] = oid
                else:
                    no_stream.append(oid)
            else:
                final = [k for k in copies.get(oid, []) if S[k]["run"] in args.final_runs]
                if final:
                    test[latest(final)] = oid
                else:
                    no_test.append(oid)

        excl_dir = org_dir / "exclude"
        excl_dir.mkdir(parents=True, exist_ok=True)
        excl = excl_dir / "replay-sessions.txt"
        lines = sorted(f"{u}:{s}" for (u, s) in S if (u, s) not in keep)
        excl.write_text(f"# {org}: replay-tagged sessions never to train on (pod_uid:session_id)\n"
                        + "\n".join(lines) + "\n", encoding="utf-8")
        failures += verify_org(org, S, halves, excl, keep, test)

        raw = args.out / "raw" / org
        test_report = run_process(org, raw, test, originals, org_dir, TEST_RUN_ID, org_dir / "test")
        stream_report = run_process(org, raw, keep, originals, org_dir, STREAM_RUN_ID, org_dir / "stream-preview")
        if test_report:
            test_csvs.append(org_dir / "test" / "sessions.csv")
        n_test_orig = sum(1 for (o, _), side in halves.items() if o == org and side == "test")
        if no_test:
            warnings.append(f"{org}: {len(no_test)} test originals have no complete final-run replay")
        summary["orgs"][org] = {
            "tagged_sessions": len(S),
            "by_kind": dict(Counter(v["kind"] for v in S.values())),
            "by_run": dict(Counter(v["run"] for v in S.values() if v["kind"] == "full")),
            "not_usable": dict(problems),
            "excluded": len(lines),
            "kept_for_training": len(keep),
            "stream_originals_without_replay": no_stream,
            "test_originals": n_test_orig,
            "test_originals_without_final_replay": no_test,
            "test": label_report(test_report),
            "stream_preview": label_report(stream_report),
            "exclude_file": str(excl),
        }
        print(f"[{org}] tagged {len(S)}, excluded {len(lines)}, kept for training {len(keep)}, "
              f"test rows {summary['orgs'][org]['test']['rows']}/{n_test_orig}", flush=True)

    merged = args.out / "replay_test.csv"
    with merged.open("w", newline="", encoding="utf-8") as fo:
        w = csv.DictWriter(fo, fieldnames=extractor.META + extractor.FEATURES)
        w.writeheader()
        for p in test_csvs:
            with p.open(newline="", encoding="utf-8") as fi:
                w.writerows(csv.DictReader(fi))
    labels = Counter()
    for o in summary["orgs"].values():
        labels.update(o["test"]["labels"])
    summary["test_rows_total"] = sum(o["test"]["rows"] for o in summary["orgs"].values())
    summary["test_label_counts_total"] = dict(labels)
    summary["checks_failed"], summary["warnings"] = failures, warnings
    (args.out / "replay_test_summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")

    print(f"\nSplit: {summary['halves']}")
    print(f"Test set: {summary['test_rows_total']} rows -> {merged}")
    print("Test labels: " + ", ".join(f"{k} {v}" for k, v in labels.most_common()))
    for w_ in warnings:
        print(f"WARNING: {w_}")
    if failures:
        for f_ in failures:
            print(f"CHECK FAILED: {f_}")
        sys.exit("Checks failed; nothing uploaded. See replay_test_summary.json.")
    print("All checks passed.")

    if args.upload:
        print("\nUploading", flush=True)
        for org in results:
            base = f"s3://{args.bucket.format(org=org)}/{org}/processed"
            aws("s3", "cp", str(args.out / org / "exclude" / "replay-sessions.txt"),
                f"{base}/exclude/replay-sessions.txt", "--region", args.region, "--only-show-errors")
            for name in ("sessions.csv", "manifest.json"):
                p = args.out / org / "test" / name
                if p.exists():
                    aws("s3", "cp", str(p), f"{base}/replay-test/{name}",
                        "--region", args.region, "--only-show-errors")
        print("\nSet FL_EXCLUDE_URI per org to s3://<bucket>/<org>/processed/exclude/")
    else:
        print("\nDry run: nothing uploaded. Re-run with --no-sync --upload when the numbers look right.")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, fn in (("inspect", cmd_inspect), ("build", cmd_build)):
        p = sub.add_parser(name)
        p.add_argument("--orgs", nargs="+", default=ORGS, choices=list(ORG_NAMES))
        p.add_argument("--out", type=Path, default=Path("data/replay-final"))
        p.add_argument("--split-dir", type=Path, default=SPLIT_DIR,
                       help="split_orgs.py output with manifest.csv and replay/<org>/cowrie.json")
        p.add_argument("--bucket", default=BUCKET, help="bucket name template, {org} is filled in")
        p.add_argument("--region", default="ap-southeast-1")
        p.add_argument("--final-runs", nargs="+", default=FINAL_RUNS)
        p.add_argument("--no-sync", dest="sync", action="store_false", help="reuse <out>/raw from a previous sync")
        p.set_defaults(func=fn)
        if name == "build":
            p.add_argument("--upload", action="store_true", help="copy exclude lists and test sets to S3")
            p.add_argument("--resplit", action="store_true",
                           help="rebuild halves.csv (only before anything trained on the stream half)")
    args = ap.parse_args()
    args.final_runs = [r.lower() for r in args.final_runs]
    args.func(args)


if __name__ == "__main__":
    main()
