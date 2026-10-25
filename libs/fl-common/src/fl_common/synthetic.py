"""Synthetic sessions that match the feature schema.

Stand-in data so the client and coordinator can be built and tested before
the log processor produces real features. Each org gets a different mix of
attack classes (non-IID), which is what makes FedAvg vs FedMedian vs FedProx
worth comparing.
"""

from __future__ import annotations

import numpy as np

from fl_common.schema import FEATURE_NAMES, NUM_CLASSES, AttackClass, label_session, validate_row


def _session(cls: AttackClass, rng: np.random.Generator) -> dict[str, float]:
    p = rng.poisson
    row = dict.fromkeys(FEATURE_NAMES, 0.0)
    row["client_version_len"] = float(rng.integers(0, 40))

    if cls is AttackClass.SCAN:
        row["duration_s"] = float(rng.exponential(2.0))
        return row

    attempts = 1 + p(15 if cls is AttackClass.BRUTE_FORCE else 3)
    row["login_attempts"] = float(attempts)
    row["unique_usernames"] = float(min(attempts, 1 + p(3)))
    row["unique_passwords"] = float(min(attempts, 1 + p(10)))

    if cls is AttackClass.BRUTE_FORCE:
        row["failed_logins"] = float(attempts)
        row["duration_s"] = float(rng.exponential(30.0))
        return row

    row["successful_logins"] = 1.0
    row["failed_logins"] = float(attempts - 1)
    cmds = 1 + p(8 if cls is AttackClass.RECON else 15)
    row["commands_count"] = float(cmds)
    row["unique_commands"] = float(min(cmds, 1 + p(5)))
    row["duration_s"] = float(rng.exponential(60.0 if cls is AttackClass.RECON else 120.0))
    if cls is AttackClass.MALWARE:
        row["download_count"] = float(1 + p(1))
    return row


def class_mix(org_index: int, num_orgs: int, alpha: float = 0.5, seed: int = 0) -> np.ndarray:
    """Per-org class proportions drawn from a Dirichlet. Lower alpha = more skewed."""
    rng = np.random.default_rng(seed)
    mixes = rng.dirichlet([alpha] * NUM_CLASSES, size=num_orgs)
    mixes = 0.05 / NUM_CLASSES + 0.95 * mixes  # every org sees a little of each class
    return mixes[org_index]


def make_org_dataset(
    org_index: int,
    num_orgs: int,
    num_sessions: int = 2000,
    alpha: float = 0.5,
    seed: int = 0,
) -> tuple[np.ndarray, np.ndarray]:
    """Return (X, y) for one org. X is raw (untransformed) features, float32."""
    mix = class_mix(org_index, num_orgs, alpha, seed)
    rng = np.random.default_rng(seed * 1_000 + org_index + 1)
    classes = rng.choice(NUM_CLASSES, size=num_sessions, p=mix)
    X = np.empty((num_sessions, len(FEATURE_NAMES)), dtype=np.float32)
    y = np.empty(num_sessions, dtype=np.int64)
    for i, c in enumerate(classes):
        row = _session(AttackClass(int(c)), rng)
        X[i] = validate_row(row)
        y[i] = int(label_session(row))  # same labelling rule as real data
    return X, y
