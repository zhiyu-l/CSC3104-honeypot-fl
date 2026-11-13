"""Run the `flwr` CLI against a SuperLink whose TLS name does not resolve here.

The coordinator's certificate is issued for `coordinator.fl.internal`, and the
CLI checks the certificate against the host name it connects to. That name only
resolves inside the org VPCs, so inside the coordinator task (and in the daily
trigger task) there is nothing to connect to under that name, and connecting to
127.0.0.1 or the internal NLB by address fails the host-name check.

    FL_HOST_OVERRIDE="coordinator.fl.internal=127.0.0.1"

makes this process resolve that name to the given address (or DNS name), the way
an /etc/hosts entry would (non-root Fargate containers cannot edit /etc/hosts).
TLS is unchanged: the certificate chain AND the host name are still verified.
Several overrides can be separated by commas.

Use exactly like `flwr`:
    python -m fl_coordinator.flwr_cli supernode register key.pub local
"""

from __future__ import annotations

import os
import socket
import sys

ENV = "FL_HOST_OVERRIDE"


def parse_overrides(spec: str) -> dict[str, str]:
    """'a=1.2.3.4,b=host' -> {'a': '1.2.3.4', 'b': 'host'} (names lower-cased)."""
    out: dict[str, str] = {}
    for item in spec.split(","):
        item = item.strip()
        if not item:
            continue
        name, sep, target = item.partition("=")
        if not sep or not name.strip() or not target.strip():
            raise ValueError(f"{ENV}: expected name=address, got {item!r}")
        out[name.strip().lower()] = target.strip()
    return out


def install(overrides: dict[str, str]) -> None:
    """Resolve the given names to their targets for the rest of this process."""
    if not overrides:
        return
    original = socket.getaddrinfo

    def getaddrinfo(host, *args, **kwargs):
        if isinstance(host, bytes):
            host = host.decode()
        if isinstance(host, str):
            host = overrides.get(host.lower(), host)
        return original(host, *args, **kwargs)

    socket.getaddrinfo = getaddrinfo


def main() -> None:
    install(parse_overrides(os.environ.get(ENV, "")))
    from flwr.cli.app import app

    sys.argv[0] = "flwr"
    app()


if __name__ == "__main__":
    main()
