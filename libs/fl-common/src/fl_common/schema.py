"""Feature schema shared by the log processor, the client and the coordinator.

One row = one Cowrie SSH session. The log processor
(tools/cowrie_dataset/cowrie_sessions.py) turns Cowrie JSON events into rows
with exactly these columns, in exactly this order. The client feeds them to the
model. If you change this file, bump SCHEMA_VERSION, because old models will no
longer match and the coordinator rejects updates with a different version.

Cowrie events each feature comes from:
    n_login_failed       cowrie.login.failed
    n_login_success      cowrie.login.success
    n_commands           cowrie.command.input
    n_commands_failed    cowrie.command.failed
    n_unique_commands    distinct command lines
    duration_s           cowrie.session.closed (duration)
    time_to_first_cmd_s  first cowrie.command.input minus session start (0 if none)
    n_url_downloads      cowrie.session.file_download with a url
    n_file_uploads       cowrie.session.file_upload (scp/sftp)
    n_files_written      cowrie.session.file_download without a url (shell redirection)
    n_tcpip_requests     cowrie.direct-tcpip.request (port forwarding)
    cmd_download         1 if any command used wget/curl/tftp/ftpget/scp//dev/tcp
    cmd_persistence      1 if any command touched authorized_keys/cron/passwords/startup files
    cmd_recon            1 if any command was system discovery (uname, /proc/cpuinfo, ...)
    cmd_exec             1 if any command made a file executable or ran a script
    cmd_cleanup          1 if any command killed processes, wiped history or rm -rf'd

IP addresses, locations and SSH client versions are deliberately not features:
they change when attacks are replayed and would not generalise.

Schema history:
    1  10 features; classes scan / brute force / recon / malware (synthetic only)
    2  16 features above; classes brute force / recon / persistence / malware download
"""

from __future__ import annotations

import math
from enum import IntEnum
from typing import Mapping, Sequence

SCHEMA_VERSION = 2

FEATURE_NAMES: tuple[str, ...] = (
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
)
NUM_FEATURES = len(FEATURE_NAMES)

_FLAGS = ("cmd_download", "cmd_persistence", "cmd_recon", "cmd_exec", "cmd_cleanup")

# Values above these are clipped. Logs are attacker controlled, so a session
# claiming 10 million commands must not blow up training.
FEATURE_CAPS: Mapping[str, float] = {
    "n_login_failed": 10_000.0,
    "n_login_success": 10_000.0,
    "n_commands": 10_000.0,
    "n_commands_failed": 10_000.0,
    "n_unique_commands": 10_000.0,
    "duration_s": 86_400.0,
    "time_to_first_cmd_s": 86_400.0,
    "n_url_downloads": 1_000.0,
    "n_file_uploads": 1_000.0,
    "n_files_written": 1_000.0,
    "n_tcpip_requests": 10_000.0,
    **{f: 1.0 for f in _FLAGS},
}


class AttackClass(IntEnum):
    """What the session was trying to do. Labelled by the log processor."""

    BRUTE_FORCE = 0       # tried to log in, ran no commands
    RECON = 1             # logged in and ran commands, nothing below
    PERSISTENCE = 2       # planted SSH keys, cron jobs or changed passwords
    MALWARE_DOWNLOAD = 3  # downloaded or uploaded a file (bots, miners)


NUM_CLASSES = len(AttackClass)

# Text labels used in the log processor's CSVs.
LABEL_NAMES: Mapping[str, AttackClass] = {
    "brute_force": AttackClass.BRUTE_FORCE,
    "recon": AttackClass.RECON,
    "persistence": AttackClass.PERSISTENCE,
    "malware_download": AttackClass.MALWARE_DOWNLOAD,
}

# Sessions the log processor keeps but that are not one of the 4 classes.
EXCLUDED_LABELS: frozenset[str] = frozenset({"scan", "tunnel"})


def parse_label(raw: str | int) -> AttackClass | None:
    """Text ("persistence") or numeric ("2") label -> AttackClass.

    Returns None for excluded labels (scan, tunnel). Raises SchemaError for
    anything else.
    """
    text = str(raw).strip().lower()
    if text in EXCLUDED_LABELS:
        return None
    if text in LABEL_NAMES:
        return LABEL_NAMES[text]
    try:
        return AttackClass(int(text))
    except ValueError:
        raise SchemaError(f"unknown label {raw!r}") from None


def label_session(row: Mapping[str, float]) -> AttackClass | None:
    """Rule-based label so every org labels sessions the same way.

    Same rules as tools/cowrie_dataset/cowrie_sessions.py, most serious first.
    Returns None for sessions that are not one of the 4 classes (scan, tunnel).
    """
    g = lambda k: row.get(k, 0) or 0  # noqa: E731
    if g("n_url_downloads") or g("n_file_uploads") or g("cmd_download"):
        return AttackClass.MALWARE_DOWNLOAD
    if g("cmd_persistence"):
        return AttackClass.PERSISTENCE
    if g("n_commands"):
        return AttackClass.RECON
    if g("n_tcpip_requests"):
        return None  # tunnel
    if g("n_login_failed") or g("n_login_success"):
        return AttackClass.BRUTE_FORCE
    return None  # scan


class SchemaError(ValueError):
    """Raised when a row does not match the schema."""


def validate_row(row: Mapping[str, object]) -> list[float]:
    """Check a raw row and return its values in FEATURE_NAMES order.

    Rejects missing or unknown columns, non-numbers, NaN/inf and negatives.
    Clips values to FEATURE_CAPS. Never evaluates anything from the input.
    """
    missing = [n for n in FEATURE_NAMES if n not in row]
    if missing:
        raise SchemaError(f"missing features: {missing}")
    extra = [k for k in row if k not in FEATURE_NAMES and k != "label"]
    if extra:
        raise SchemaError(f"unknown features: {extra}")

    values: list[float] = []
    for name in FEATURE_NAMES:
        raw = row[name]
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise SchemaError(f"{name} must be a number, got {type(raw).__name__}")
        value = float(raw)
        if not math.isfinite(value):
            raise SchemaError(f"{name} is not finite")
        if value < 0:
            raise SchemaError(f"{name} is negative")
        values.append(min(value, FEATURE_CAPS[name]))
    return values


def transform(values: Sequence[float]) -> list[float]:
    """Model input transform: log1p, since counts are heavily skewed.

    Every client must apply this identically, otherwise averaging weights
    across orgs is meaningless.
    """
    if len(values) != NUM_FEATURES:
        raise SchemaError(f"expected {NUM_FEATURES} values, got {len(values)}")
    return [math.log1p(v) for v in values]
