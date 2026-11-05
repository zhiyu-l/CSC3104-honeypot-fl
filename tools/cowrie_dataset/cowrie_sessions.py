"""Turn raw Cowrie JSON logs into one labelled feature row per session.

The same code must run on the Kaggle training data AND on the logs produced by
the replayed attacks, so both come out with identical columns and labels.

Usage (from the repo root):
    python tools/cowrie_dataset/cowrie_sessions.py label \
        --logs data/raw/kaggle-cowrie/logs \
        --out  data/processed/kaggle/sessions.csv

Each sub-folder of --logs is treated as one honeypot (sensor). Every
cowrie.json* file inside it is read in time order.

Labels (one per session, most serious rule wins):
    malware_download > persistence > recon > brute_force
Sessions that are not attacks we classify are kept in the CSV with label
"scan" (connected, never tried to log in) or "tunnel" (only used SSH
port-forwarding), so they can be counted and then excluded from training.

FEATURES and the label rules must match libs/fl-common/src/fl_common/schema.py
(FEATURE_NAMES and label_session); keep them in sync when changing either.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import os
import re
import sys
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime

LABELS = ["malware_download", "persistence", "recon", "brute_force"]
EXCLUDED = ["scan", "tunnel"]

# Command patterns. Matched against each command line the attacker typed.
RE_DOWNLOAD = re.compile(r"\b(wget|curl|tftp|ftpget|busybox\s+(wget|tftp))\b|/dev/tcp/|\bscp\b")
RE_PERSIST = re.compile(
    r"authorized_keys|\bcrontab\b|/etc/cron|\bchpasswd\b|(?<!/etc/)\bpasswd\b|\buseradd\b|\badduser\b"
    r"|/etc/rc\.local|systemctl\s+enable|/etc/init\.d|\.bashrc|\.profile"
)
RE_RECON = re.compile(
    r"\buname\b|/proc/cpuinfo|\bnproc\b|\bfree\b|\bwhoami\b|\bid\b|\bw\b|\blscpu\b|\bhostname\b"
    r"|/etc/\S*release|\bifconfig\b|\bip\s+a|\bps\b|\btop\b|\bdf\b|\bls\b|\bcat\b"
)
RE_EXEC = re.compile(r"chmod\s+\+?[0-7]*x|chmod\s+777|\./\S+|\bsh\s+\S+\.sh|\bbash\s+\S+\.sh")
RE_CLEANUP = re.compile(r"\bpkill\b|\bkillall\b|\bkill\s+-9\b|history\s+-c|rm\s+-rf|chattr\b")

FEATURES = [
    "n_login_failed",
    "n_login_success",
    "n_commands",
    "n_commands_failed",
    "n_unique_commands",
    "duration_s",
    "time_to_first_cmd_s",
    "n_url_downloads",
    "n_file_uploads",
    "n_files_written",
    "n_tcpip_requests",
    "cmd_download",
    "cmd_persistence",
    "cmd_recon",
    "cmd_exec",
    "cmd_cleanup",
]
META = ["honeypot", "session_id", "src_ip", "start", "label"]


def _ts(s: str) -> float:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def _new_session(honeypot: str, sid: str) -> dict:
    return {
        "honeypot": honeypot,
        "session_id": f"{honeypot}:{sid}",
        "src_ip": "",
        "start": None,
        "end": None,
        "first_cmd": None,
        "n_login_failed": 0,
        "n_login_success": 0,
        "n_commands": 0,
        "n_commands_failed": 0,
        "cmds": set(),
        "duration": None,
        "n_url_downloads": 0,
        "n_file_uploads": 0,
        "n_files_written": 0,
        "n_tcpip_requests": 0,
        "cmd_download": 0,
        "cmd_persistence": 0,
        "cmd_recon": 0,
        "cmd_exec": 0,
        "cmd_cleanup": 0,
    }


def update(s: dict, e: dict) -> None:
    """Fold one Cowrie event into its session state."""
    ev = e.get("eventid", "")
    t = _ts(e["timestamp"]) if "timestamp" in e else None
    if t is not None:
        s["start"] = t if s["start"] is None else min(s["start"], t)
        s["end"] = t if s["end"] is None else max(s["end"], t)
    if e.get("src_ip") and not s["src_ip"]:
        s["src_ip"] = e["src_ip"]

    if ev == "cowrie.login.failed":
        s["n_login_failed"] += 1
    elif ev == "cowrie.login.success":
        s["n_login_success"] += 1
    elif ev == "cowrie.command.input":
        cmd = e.get("input", "")
        s["n_commands"] += 1
        s["cmds"].add(cmd)
        if s["first_cmd"] is None and t is not None:
            s["first_cmd"] = t
        s["cmd_download"] |= bool(RE_DOWNLOAD.search(cmd))
        s["cmd_persistence"] |= bool(RE_PERSIST.search(cmd))
        s["cmd_recon"] |= bool(RE_RECON.search(cmd))
        s["cmd_exec"] |= bool(RE_EXEC.search(cmd))
        s["cmd_cleanup"] |= bool(RE_CLEANUP.search(cmd))
    elif ev == "cowrie.command.failed":
        s["n_commands_failed"] += 1
    elif ev == "cowrie.session.file_download":
        # Cowrie also logs files created by shell redirection (echo > file) as
        # "file_download" with no url. Only a url means a real download.
        if e.get("url"):
            s["n_url_downloads"] += 1
        else:
            s["n_files_written"] += 1
    elif ev == "cowrie.session.file_upload":
        s["n_file_uploads"] += 1
    elif ev == "cowrie.direct-tcpip.request":
        s["n_tcpip_requests"] += 1
    elif ev == "cowrie.session.closed":
        if e.get("duration") is not None:
            s["duration"] = float(e["duration"])


def label(s: dict) -> str:
    logins = s["n_login_failed"] + s["n_login_success"]
    if s["n_url_downloads"] or s["n_file_uploads"] or s["cmd_download"]:
        return "malware_download"
    if s["cmd_persistence"]:
        return "persistence"
    if s["n_commands"]:
        return "recon"
    if s["n_tcpip_requests"]:
        return "tunnel"
    if logins:
        return "brute_force"
    return "scan"


def finish(s: dict) -> dict:
    """Session state -> flat CSV row."""
    start = s["start"] or 0.0
    duration = s["duration"] if s["duration"] is not None else (s["end"] or start) - start
    row = {
        "honeypot": s["honeypot"],
        "session_id": s["session_id"],
        "src_ip": s["src_ip"],
        "start": datetime.utcfromtimestamp(start).isoformat(timespec="seconds") + "Z",
        "label": label(s),
        "duration_s": round(duration, 3),
        "time_to_first_cmd_s": round(s["first_cmd"] - start, 3) if s["first_cmd"] else 0.0,
        "n_unique_commands": len(s["cmds"]),
    }
    for k in FEATURES:
        if k not in row:
            row[k] = int(s[k])
    return row


def log_files(folder: str) -> list[str]:
    """cowrie.json.<date>[.gz] in date order, then the active cowrie.json last."""
    names = [n for n in os.listdir(folder) if n.startswith("cowrie.json")]
    rotated = sorted(n for n in names if n not in ("cowrie.json", "cowrie.json.gz"))
    active = [n for n in names if n in ("cowrie.json", "cowrie.json.gz")]
    return [os.path.join(folder, n) for n in rotated + active]


def iter_events(path: str, bad: Counter):
    opener = gzip.open if path.endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                bad["bad_json_lines"] += 1
                continue
            if "session" in e:
                yield e


def process_honeypot(folder: str, out_path: str) -> dict:
    """Read one honeypot folder and write its rows to out_path. Returns stats."""
    honeypot = os.path.basename(folder.rstrip("/\\"))
    open_sessions: dict[str, dict] = {}
    stats: Counter = Counter()
    with open(out_path, "w", newline="") as fo:
        w = csv.DictWriter(fo, fieldnames=META + FEATURES)
        w.writeheader()
        for path in log_files(folder):
            for e in iter_events(path, stats):
                sid = e["session"]
                s = open_sessions.get(sid)
                if s is None:
                    s = open_sessions[sid] = _new_session(honeypot, sid)
                update(s, e)
                if e.get("eventid") == "cowrie.session.closed":
                    w.writerow(finish(open_sessions.pop(sid)))
                    stats["sessions"] += 1
        for s in open_sessions.values():  # sessions still open at end of logs
            w.writerow(finish(s))
            stats["sessions"] += 1
            stats["unclosed_sessions"] += 1
    stats["honeypot"] = honeypot
    return dict(stats)


def cmd_label(args) -> None:
    folders = sorted(
        os.path.join(args.logs, d) for d in os.listdir(args.logs) if os.path.isdir(os.path.join(args.logs, d))
    )
    if not folders:
        sys.exit(f"No honeypot folders found in {args.logs}")
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    part_dir = args.out + ".parts"
    os.makedirs(part_dir, exist_ok=True)
    parts = [os.path.join(part_dir, os.path.basename(f) + ".csv") for f in folders]

    print(f"Processing {len(folders)} honeypots with {args.workers} workers...", flush=True)
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        for st in pool.map(process_honeypot, folders, parts):
            print(f"  done {st['honeypot']}: {st.get('sessions', 0):,} sessions"
                  f" ({st.get('bad_json_lines', 0)} bad lines, {st.get('unclosed_sessions', 0)} unclosed)", flush=True)

    # Merge parts and count labels.
    counts: dict[str, Counter] = defaultdict(Counter)
    ips: dict[str, set] = defaultdict(set)
    with open(args.out, "w", newline="") as fo:
        w = csv.DictWriter(fo, fieldnames=META + FEATURES)
        w.writeheader()
        for p in parts:
            with open(p, newline="") as fi:
                for row in csv.DictReader(fi):
                    w.writerow(row)
                    counts[row["label"]][row["honeypot"]] += 1
                    ips[row["label"]].add(row["src_ip"])
    write_summary(counts, ips, args.out.rsplit(".", 1)[0] + "_label_counts.txt")


def write_summary(counts, ips, path: str) -> None:
    honeypots = sorted({h for c in counts.values() for h in c})
    lines = [f"{'label':<18}{'sessions':>12}{'unique IPs':>12}"]
    for lab in LABELS + EXCLUDED:
        lines.append(f"{lab:<18}{sum(counts[lab].values()):>12,}{len(ips[lab]):>12,}")
    lines.append("")
    lines.append("Sessions per honeypot (4 attack labels only):")
    lines.append(f"{'honeypot':<32}" + "".join(f"{l[:12]:>14}" for l in LABELS))
    for h in honeypots:
        lines.append(f"{h:<32}" + "".join(f"{counts[l][h]:>14,}" for l in LABELS))
    text = "\n".join(lines)
    print("\n" + text)
    with open(path, "w") as f:
        f.write(text + "\n")
    print(f"\nSummary written to {path}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("label", help="label every session and print counts per label")
    p.add_argument("--logs", default="data/raw/kaggle-cowrie/logs")
    p.add_argument("--out", default="data/processed/kaggle/sessions.csv")
    p.add_argument("--workers", type=int, default=min(4, os.cpu_count() or 1))
    p.set_defaults(func=cmd_label)
    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
