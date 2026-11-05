"""Split labelled Kaggle sessions into 3 orgs x (train, replay).

Run after `cowrie_sessions.py label`. From the repo root:
    python tools/cowrie_dataset/split_orgs.py

What it does
1. Keeps the 4 attack labels (drops scan / tunnel).
2. Puts every attacker IP on ONE side, globally: ~80% of IPs -> train, ~20% -> replay.
   The same IP can never be in training (any org) and in replay (any org).
3. Builds 3 equal-sized orgs on each side. Each org is 60% its main attack and
   40% split evenly over the other 3 labels:
       orgA: brute_force   orgB: malware_download   orgC: persistence
   Org size is set by the scarcest label (capped at --max-per-org).
   Sessions are picked spread across IPs first, so one bot can't fill a class.
4. Writes:
     <out>/train/orgX.csv           feature rows for training (Zhi Yu)
     <out>/replay/orgX/cowrie.json  raw Cowrie events of the replay sessions (Jia Ler)
     <out>/manifest.csv             every chosen session: org, split, label, IP
   and fails loudly if any session or IP appears on both sides.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import random
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cowrie_sessions import FEATURES, LABELS, META, iter_events, log_files  # noqa: E402

ORG_MAIN = {"orgA": "brute_force", "orgB": "malware_download", "orgC": "persistence"}
MAIN_SHARE = 0.6


def ip_side(ip: str, replay_pct: int, salt: str) -> str:
    h = int(hashlib.sha256(f"{salt}:{ip}".encode()).hexdigest(), 16)
    return "replay" if h % 100 < replay_pct else "train"


def quotas(n: int) -> dict[str, dict[str, int]]:
    """Per org, how many sessions of each label for an org of size n."""
    q = {}
    for org, main in ORG_MAIN.items():
        q[org] = {main: round(n * MAIN_SHARE)}
        others = [l for l in LABELS if l != main]
        rest = n - q[org][main]
        for i, l in enumerate(others):
            q[org][l] = rest // len(others) + (1 if i < rest % len(others) else 0)
    return q


def org_size(pool: dict[str, list], cap: int) -> int:
    """Largest equal org size the pool can fill (binary search on quotas)."""
    lo, hi = 0, cap
    while lo < hi:
        mid = (lo + hi + 1) // 2
        need = Counter()
        for q in quotas(mid).values():
            need.update(q)
        if all(need[l] <= len(pool[l]) for l in LABELS):
            lo = mid
        else:
            hi = mid - 1
    return lo


def spread_order(rows: list[dict], rng: random.Random) -> list[dict]:
    """Shuffle, then order so each IP's 1st session comes before any IP's 2nd, etc."""
    rows = rows[:]
    rng.shuffle(rows)
    rank: Counter = Counter()
    keyed = []
    for r in rows:
        keyed.append((rank[r["src_ip"]], len(keyed), r))
        rank[r["src_ip"]] += 1
    keyed.sort(key=lambda t: (t[0], t[1]))
    return [r for _, _, r in keyed]


def assign(pool: dict[str, list], n: int, rng: random.Random) -> dict[str, list]:
    """Take sessions from the pool to fill each org's quota."""
    ordered = {l: spread_order(pool[l], rng) for l in LABELS}
    taken = {l: 0 for l in LABELS}
    orgs = {org: [] for org in ORG_MAIN}
    # Fill main attacks first so they get the most diverse sessions.
    plan = sorted(
        ((org, l, k) for org, q in quotas(n).items() for l, k in q.items()),
        key=lambda t: ORG_MAIN[t[0]] != t[1],
    )
    for org, l, k in plan:
        chunk = ordered[l][taken[l]: taken[l] + k]
        taken[l] += k
        orgs[org].extend(chunk)
    for org in orgs:
        rng.shuffle(orgs[org])
    return orgs


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sessions", default="data/processed/kaggle/sessions.csv")
    ap.add_argument("--logs", default="data/raw/kaggle-cowrie/logs")
    ap.add_argument("--out", default="data/processed/kaggle/split")
    ap.add_argument("--replay-pct", type=int, default=20)
    ap.add_argument("--max-per-org", type=int, default=4000)
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()
    rng = random.Random(args.seed)
    salt = f"csc3104-{args.seed}"

    pools = {"train": defaultdict(list), "replay": defaultdict(list)}
    with open(args.sessions, newline="") as f:
        for r in csv.DictReader(f):
            if r["label"] in LABELS:
                pools[ip_side(r["src_ip"], args.replay_pct, salt)][r["label"]].append(r)

    result = {}
    for side in ("train", "replay"):
        n = org_size(pools[side], args.max_per_org)
        result[side] = assign(pools[side], n, rng)
        print(f"{side}: {n} sessions per org")
        for org, rows in result[side].items():
            c = Counter(r["label"] for r in rows)
            print(f"  {org} (main {ORG_MAIN[org]}): " + ", ".join(f"{l}={c[l]}" for l in LABELS))

    # Leakage check: no session and no IP on both sides.
    sess = {s: {r["session_id"] for rows in result[s].values() for r in rows} for s in result}
    ips = {s: {r["src_ip"] for rows in result[s].values() for r in rows} for s in result}
    all_sess = [r["session_id"] for s in result for rows in result[s].values() for r in rows]
    overlap_s, overlap_ip = sess["train"] & sess["replay"], ips["train"] & ips["replay"]
    dup = len(all_sess) - len(set(all_sess))
    if overlap_s or overlap_ip or dup:
        sys.exit(f"LEAK: {len(overlap_s)} sessions, {len(overlap_ip)} IPs shared; {dup} duplicates")
    print(f"Overlap check passed: 0 shared sessions, 0 shared IPs "
          f"({len(ips['train'])} train IPs, {len(ips['replay'])} replay IPs)")

    # Write train feature files and the manifest.
    os.makedirs(os.path.join(args.out, "train"), exist_ok=True)
    with open(os.path.join(args.out, "manifest.csv"), "w", newline="") as fm:
        mw = csv.writer(fm)
        mw.writerow(["session_id", "honeypot", "src_ip", "label", "org", "split"])
        for side in result:
            for org, rows in result[side].items():
                for r in rows:
                    mw.writerow([r["session_id"], r["honeypot"], r["src_ip"], r["label"], org, side])
    for org, rows in result["train"].items():
        with open(os.path.join(args.out, "train", f"{org}.csv"), "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=META + FEATURES)
            w.writeheader()
            w.writerows(rows)

    # Pull the raw events of every replay session out of the original logs.
    want = {r["session_id"]: org for org, rows in result["replay"].items() for r in rows}
    by_hp = defaultdict(set)
    for sid in want:
        hp, s = sid.split(":", 1)
        by_hp[hp].add(s)
    events = defaultdict(list)
    for hp, sids in by_hp.items():
        for path in log_files(os.path.join(args.logs, hp)):
            for e in iter_events(path, Counter()):
                if e["session"] in sids:
                    e["source_honeypot"] = hp
                    events[want[f"{hp}:{e['session']}"]].append(e)
    for org, evs in events.items():
        d = os.path.join(args.out, "replay", org)
        os.makedirs(d, exist_ok=True)
        evs.sort(key=lambda e: e.get("timestamp", ""))
        with open(os.path.join(d, "cowrie.json"), "w") as f:
            for e in evs:
                f.write(json.dumps(e) + "\n")
        print(f"  replay/{org}/cowrie.json: {len(evs):,} events")
    print(f"Done. Output in {args.out}")


if __name__ == "__main__":
    main()
