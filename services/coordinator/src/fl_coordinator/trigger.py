"""Start one cycle of the daily training loop, then exit.

This is the scheduled job. It does not train anything itself: it submits a run
to the always-on coordinator (SuperLink), the way you would type `flwr run` by
hand, with the settings that make it a continuation of the loop:

    init-from='latest'   start from the last promoted global model
    promote=true         move latest.json to the new model if it is not worse
    run-name='daily-<UTC timestamp>'

Then it exits; the coordinator and the already-connected clients do the rest.
It refuses to submit when a run is still pending or running, so two cycles never
overlap (e.g. the schedule fires while yesterday's run is stuck on a retry).

Settings (flag, or env var):
    --superlink   SUPERLINK_ADDRESS           coordinator HTTP API, host:port (default 127.0.0.1:8000)
    --app         FLWR_APP                    folder with the Flower app pyproject.toml
                                              (default /app/flower-app in the coordinator image)
    --insecure    FL_INSECURE=true            plain gRPC (local only); otherwise TLS with
    --root-certificates  FL_ROOT_CERTIFICATES CA certificate to trust
    --run-config  FL_RUN_CONFIG               extra settings, e.g. "num-server-rounds=10 eval-data='/models/test.csv'"
    --dry-run                                 print the command, submit nothing

Locally (stack from deploy/local running):
    python -m fl_coordinator.trigger --insecure --app deploy/flower-app
On AWS: EventBridge Scheduler runs the coordinator image as a one-off ECS task
with the command `python -m fl_coordinator.trigger`.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ACTIVE = ("pending", "starting", "running")
CONNECTION = "trigger"


def _env_bool(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes")


def write_flwr_config(home: Path, address: str, insecure: bool, root_certificates: str | None) -> None:
    """flwr reads its SuperLink connections from $FLWR_HOME/config.toml."""
    lines = [f"[superlink.{CONNECTION}]", f'address = "{address}"']
    if insecure:
        lines.append("insecure = true")
    elif root_certificates:
        lines.append(f'root-certificates = "{root_certificates}"')
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.toml").write_text("\n".join(lines) + "\n")


def active_runs(ls_json: str) -> list[dict]:
    """Runs from `flwr ls --format json` output that are not finished yet."""
    data = json.loads(ls_json)
    if not data.get("success", False):
        raise RuntimeError(f"flwr ls failed: {data}")
    return [r for r in data.get("runs", []) if str(r.get("status", "")).split(":")[0] in ACTIVE]


def build_run_config(run_name: str, extra: str) -> str:
    base = f"init-from='latest' promote=true run-name='{run_name}'"
    return f"{base} {extra}".strip()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--superlink", default=os.environ.get("SUPERLINK_ADDRESS", "127.0.0.1:8000"))
    ap.add_argument("--app", default=os.environ.get("FLWR_APP", "/app/flower-app"))
    ap.add_argument("--insecure", action="store_true", default=_env_bool("FL_INSECURE"))
    ap.add_argument("--root-certificates", default=os.environ.get("FL_ROOT_CERTIFICATES"))
    ap.add_argument("--run-config", default=os.environ.get("FL_RUN_CONFIG", ""))
    ap.add_argument("--run-name", help="default daily-<UTC timestamp>")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)

    if not a.insecure and not a.root_certificates:
        print("Need --root-certificates (TLS) or --insecure (local only).", file=sys.stderr)
        return 2
    run_name = a.run_name or time.strftime("daily-%Y%m%d-%H%M%S", time.gmtime())
    run_config = build_run_config(run_name, a.run_config)

    with tempfile.TemporaryDirectory() as home:
        write_flwr_config(Path(home), a.superlink, a.insecure, a.root_certificates)
        env = {**os.environ, "FLWR_HOME": home}

        ls = subprocess.run(["flwr", "ls", CONNECTION, "--format", "json"],
                            capture_output=True, text=True, env=env)
        if ls.returncode != 0:
            print(f"Cannot reach the coordinator at {a.superlink}:\n{ls.stdout}{ls.stderr}", file=sys.stderr)
            return 1
        busy = active_runs(ls.stdout)
        if busy:
            ids = ", ".join(f"{r.get('run-id')} ({r.get('status')})" for r in busy)
            print(f"Not starting a new cycle: run(s) still in progress: {ids}")
            return 0

        cmd = ["flwr", "run", a.app, CONNECTION, "--run-config", run_config, "--format", "json"]
        print("$ " + " ".join(cmd))
        if a.dry_run:
            return 0
        sub = subprocess.run(cmd, capture_output=True, text=True, env=env)
        print(sub.stdout + sub.stderr)
        if sub.returncode != 0:
            return sub.returncode
    print(f"Submitted cycle {run_name}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
