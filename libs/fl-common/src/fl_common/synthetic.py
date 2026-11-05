"""Synthetic sessions that match the feature schema.

Stand-in data so the client and coordinator can be built and tested without
the real dataset (e.g. on a teammate's machine, or in CI). The shapes roughly
follow what real Cowrie bots do (see tools/cowrie_dataset). Each org gets a
different mix of attack classes (non-IID), which is what makes FedAvg vs
FedMedian vs FedProx worth comparing.
"""

from __future__ import annotations

import numpy as np

from fl_common.schema import FEATURE_NAMES, NUM_CLASSES, AttackClass, label_session, validate_row


def _session(cls: AttackClass, rng: np.random.Generator) -> dict[str, float]:
    p, u = rng.poisson, rng.random
    row = dict.fromkeys(FEATURE_NAMES, 0.0)

    if cls is AttackClass.BRUTE_FORCE:
        # Bots open a new connection per guess, so mostly 1 failed login.
        row["n_login_failed"] = float(1 + p(0.3))
        row["n_login_success"] = float(u() < 0.1)
        row["duration_s"] = float(rng.exponential(2.0))
        return row

    row["n_login_success"] = 1.0
    row["n_login_failed"] = float(p(0.5))
    row["time_to_first_cmd_s"] = float(rng.exponential(1.0))

    if cls is AttackClass.RECON:
        cmds = 1 + p(1.5)
        row["cmd_recon"] = float(u() < 0.6)
        row["duration_s"] = float(rng.exponential(5.0))
    elif cls is AttackClass.PERSISTENCE:
        cmds = 3 + p(4)
        row["cmd_persistence"] = 1.0
        row["cmd_recon"] = float(u() < 0.8)
        row["cmd_cleanup"] = float(u() < 0.8)
        row["n_files_written"] = float(1 + p(0.5))
        row["duration_s"] = float(rng.exponential(20.0))
    else:  # MALWARE_DOWNLOAD
        cmds = 2 + p(5)
        if u() < 0.5:
            row["cmd_download"] = 1.0
            row["n_url_downloads"] = float(p(1.0))
        else:
            row["n_file_uploads"] = float(1 + p(1.0))
        row["cmd_exec"] = float(u() < 0.8)
        row["cmd_recon"] = float(u() < 0.5)
        row["cmd_cleanup"] = float(u() < 0.5)
        row["duration_s"] = float(rng.exponential(60.0))

    row["n_commands"] = float(cmds)
    row["n_unique_commands"] = float(min(cmds, 1 + p(3)))
    row["n_commands_failed"] = float(min(cmds, p(0.5)))
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
