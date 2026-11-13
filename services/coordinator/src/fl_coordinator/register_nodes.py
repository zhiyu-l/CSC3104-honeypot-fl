"""Register the orgs' SuperNodes with the SuperLink when the coordinator starts.

The SuperLink runs with --enable-supernode-auth and keeps its state in memory,
so after every restart (crash, redeploy, ECS replacing the task) it has
forgotten which SuperNodes may connect, and every client is refused
(ApiError 2004) until it is registered again. Run in the background next to
`flower-superlink` in the coordinator task, this script waits until the
SuperLink answers and registers each org's PUBLIC key, so the clients
reconnect on their own. Keeping state in memory is deliberate: a run that was
interrupted by a crash does not stay "running" and block the daily trigger;
the run is resumed from its EFS checkpoint instead (resume-from).

Settings (env):
    FL_SUPERNODE_PUBLIC_KEYS   one OpenSSH public key per line, e.g.
                               "ecdsa-sha2-nistp384 AAAA... org-a"
                               (the comment is used as the node name)
    SUPERLINK_ADDRESS          Control API, default coordinator.fl.internal:8000
    FL_HOST_OVERRIDE           e.g. coordinator.fl.internal=127.0.0.1 (see flwr_cli.py)
    FL_ROOT_CERTIFICATES       CA certificate file (or FL_INSECURE=true locally)
    FL_REGISTER_TIMEOUT        seconds to wait for the SuperLink (default 300)

Public keys are not secret; the private keys never leave the org clusters.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from fl_coordinator.trigger import CONNECTION, FLWR, json_result, write_flwr_config


def parse_keys(text: str) -> list[tuple[str, str]]:
    """[(name, key line)] from one OpenSSH public key per line."""
    keys = []
    for i, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) < 2 or not parts[0].startswith("ecdsa-sha2-"):
            raise ValueError(f"line {i}: not an OpenSSH ECDSA public key")
        name = parts[2] if len(parts) > 2 else f"node-{i}"
        keys.append((name, line))
    return keys


def already_registered(output: str) -> bool:
    return "already" in output.lower()


def _flwr(args: list[str], env: dict) -> tuple[dict | None, str]:
    proc = subprocess.run(FLWR + args, capture_output=True, text=True, env=env)
    result = json_result(proc.stdout) if proc.returncode == 0 else None
    return result, proc.stdout + proc.stderr


def main() -> int:
    keys = parse_keys(os.environ.get("FL_SUPERNODE_PUBLIC_KEYS", ""))
    if not keys:
        print("register_nodes: FL_SUPERNODE_PUBLIC_KEYS is empty, nothing to register")
        return 0
    address = os.environ.get("SUPERLINK_ADDRESS", "coordinator.fl.internal:8000")
    insecure = os.environ.get("FL_INSECURE", "").lower() in ("1", "true", "yes")
    timeout = float(os.environ.get("FL_REGISTER_TIMEOUT", "300"))

    with tempfile.TemporaryDirectory() as home:
        write_flwr_config(Path(home), address, insecure, os.environ.get("FL_ROOT_CERTIFICATES"))
        env = {**os.environ, "FLWR_HOME": home}

        deadline = time.monotonic() + timeout
        while True:   # wait for the Control API
            ok, out = _flwr(["supernode", "list", CONNECTION, "--format", "json"], env)
            if ok is not None:
                break
            if time.monotonic() > deadline:
                print(f"register_nodes: SuperLink at {address} not reachable after {timeout:.0f}s:\n{out}",
                      file=sys.stderr)
                return 1
            time.sleep(3)

        failed = 0
        for name, line in keys:
            key_file = Path(home) / f"{name}.pub"
            key_file.write_text(line + "\n")
            ok, out = _flwr(["supernode", "register", str(key_file), CONNECTION,
                             "--name", name, "--format", "json"], env)
            if ok is not None:
                print(f"register_nodes: registered {name} (node_id {ok.get('node-id')})")
            elif already_registered(out):
                print(f"register_nodes: {name} was already registered")
            else:
                failed += 1
                print(f"register_nodes: FAILED to register {name}:\n{out}", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
