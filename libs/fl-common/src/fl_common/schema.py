"""Feature schema shared by the log processor, the client and the coordinator.

One row = one Cowrie SSH session. The log processor turns Cowrie JSON events
into rows with exactly these columns, in exactly this order. The client feeds
them to the model. If you change this file, bump SCHEMA_VERSION, because old
models will no longer match.

Cowrie events each feature comes from:
    duration_s          cowrie.session.closed (duration)
    login_attempts      cowrie.login.success + cowrie.login.failed
    failed_logins       cowrie.login.failed
    successful_logins   cowrie.login.success
    unique_usernames    distinct username across login events
    unique_passwords    distinct password across login events
    commands_count      cowrie.command.input (+ cowrie.command.failed)
    unique_commands     distinct first token of each command
    download_count      cowrie.session.file_download (+ file_upload)
    client_version_len  length of cowrie.client.version string (0 if missing)
"""

from __future__ import annotations

import math
from enum import IntEnum
from typing import Mapping, Sequence

SCHEMA_VERSION = 1

FEATURE_NAMES: tuple[str, ...] = (
    "duration_s",
    "login_attempts",
    "failed_logins",
    "successful_logins",
    "unique_usernames",
    "unique_passwords",
    "commands_count",
    "unique_commands",
    "download_count",
    "client_version_len",
)
NUM_FEATURES = len(FEATURE_NAMES)

# Values above these are clipped. Logs are attacker controlled, so a session
# claiming 10 million commands must not blow up training.
FEATURE_CAPS: Mapping[str, float] = {
    "duration_s": 86_400.0,
    "login_attempts": 10_000.0,
    "failed_logins": 10_000.0,
    "successful_logins": 10_000.0,
    "unique_usernames": 10_000.0,
    "unique_passwords": 10_000.0,
    "commands_count": 10_000.0,
    "unique_commands": 1_000.0,
    "download_count": 1_000.0,
    "client_version_len": 256.0,
}


class AttackClass(IntEnum):
    """What the session was trying to do. Labelled by the log processor."""

    SCAN = 0         # connected, never tried to log in
    BRUTE_FORCE = 1  # only failed logins
    RECON = 2        # logged in and ran commands, no downloads
    MALWARE = 3      # downloaded or uploaded a file


NUM_CLASSES = len(AttackClass)


def label_session(row: Mapping[str, float]) -> AttackClass:
    """Rule-based label so every org labels sessions the same way."""
    if row.get("download_count", 0) > 0:
        return AttackClass.MALWARE
    if row.get("successful_logins", 0) > 0 and row.get("commands_count", 0) > 0:
        return AttackClass.RECON
    if row.get("login_attempts", 0) > 0:
        return AttackClass.BRUTE_FORCE
    return AttackClass.SCAN


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
