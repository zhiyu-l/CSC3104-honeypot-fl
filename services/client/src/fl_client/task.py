"""Local data loading, training and evaluation for one organisation.

Kept free of Flower imports so it can be unit tested on its own.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import torch
from torch import nn

from fl_common import FEATURE_NAMES, HoneypotMLP, label_session, parse_label, transform, validate_row
from fl_common.synthetic import make_org_dataset

from fl_client.data import DataSummary, load_cumulative


@dataclass
class OrgData:
    X_train: torch.Tensor
    y_train: torch.Tensor
    X_test: torch.Tensor
    y_test: torch.Tensor
    summary: DataSummary = field(default_factory=lambda: DataSummary(source="synthetic"))


def read_feature_csv(path: Path) -> tuple[np.ndarray, np.ndarray]:
    """Read a CSV written by the log processor (tools/cowrie_dataset).

    Needs the FEATURE_NAMES columns; other columns (honeypot, session_id,
    src_ip, start) are ignored. `label` may be text ("persistence") or a class
    number; if missing it is derived with label_session.

    Every row goes through validate_row, so malformed or hostile rows are
    skipped instead of reaching training. Scan/tunnel sessions are skipped too.
    """
    if not path.is_file():
        raise FileNotFoundError(f"data file not found: {path}")
    X, y = [], []
    with path.open(newline="") as f:
        for raw in csv.DictReader(f):
            try:
                row = {k: float(raw[k]) for k in FEATURE_NAMES}
                values = validate_row(row)
                label = raw.get("label")
                cls = parse_label(label) if label not in (None, "") else label_session(row)
            except (KeyError, TypeError, ValueError):  # SchemaError is a ValueError
                continue
            if cls is None:
                continue
            X.append(values)
            y.append(int(cls))
    if not X:
        raise ValueError(f"no valid rows in {path}")
    return np.asarray(X, dtype=np.float32), np.asarray(y, dtype=np.int64)


def load_org_data(
    org_index: int,
    num_orgs: int,
    data_path: str | None = None,
    test_fraction: float = 0.2,
    seed: int = 0,
    batches_uri: str | None = None,
    exclude_uri: str | None = None,
    cache_dir: str | None = None,
    run_key: str | None = None,
) -> OrgData:
    """Load this org's sessions, apply the shared transform and split train/test.

    With data_path and/or batches_uri: the base CSV plus every verified nightly
    batch, de-duplicated and minus held-out sessions (see fl_client.data).
    Otherwise synthetic sessions from fl-common.
    """
    if data_path or batches_uri:
        X, y, summary = load_cumulative(data_path, batches_uri, exclude_uri, cache_dir, run_key)
    else:
        X, y = make_org_dataset(org_index, num_orgs, seed=seed)
        summary = DataSummary(source="synthetic", n_base=len(y))

    X = np.asarray([transform(r) for r in X.tolist()], dtype=np.float32)
    rng = np.random.default_rng(seed + org_index)
    idx = rng.permutation(len(y))
    cut = int(len(y) * (1 - test_fraction))
    tr, te = idx[:cut], idx[cut:]
    return OrgData(
        X_train=torch.from_numpy(X[tr]),
        y_train=torch.from_numpy(y[tr]),
        X_test=torch.from_numpy(X[te]),
        y_test=torch.from_numpy(y[te]),
        summary=summary,
    )


def train_local(
    model: HoneypotMLP,
    X: torch.Tensor,
    y: torch.Tensor,
    epochs: int,
    lr: float,
    batch_size: int,
    proximal_mu: float = 0.0,
    seed: int = 0,
) -> float:
    """Train in place. Returns mean loss of the last epoch.

    proximal_mu > 0 adds the FedProx term (mu/2)*||w - w_global||^2, using the
    weights the model had when this function was called as w_global.
    """
    global_params = [p.detach().clone() for p in model.parameters()] if proximal_mu > 0 else None
    opt = torch.optim.Adam(model.parameters(), lr=lr)
    loss_fn = nn.CrossEntropyLoss()
    gen = torch.Generator().manual_seed(seed)
    model.train()
    last = 0.0
    for _ in range(epochs):
        perm = torch.randperm(len(y), generator=gen)
        total, n = 0.0, 0
        for i in range(0, len(y), batch_size):
            b = perm[i : i + batch_size]
            opt.zero_grad()
            loss = loss_fn(model(X[b]), y[b])
            if global_params is not None:
                prox = sum((p - g).pow(2).sum() for p, g in zip(model.parameters(), global_params))
                loss = loss + (proximal_mu / 2) * prox
            loss.backward()
            opt.step()
            total += loss.item() * len(b)
            n += len(b)
        last = total / max(n, 1)
    return last


@torch.no_grad()
def evaluate_local(model: HoneypotMLP, X: torch.Tensor, y: torch.Tensor) -> tuple[float, float]:
    """Return (loss, accuracy) on the given data."""
    model.eval()
    logits = model(X)
    loss = nn.functional.cross_entropy(logits, y).item()
    acc = (logits.argmax(1) == y).float().mean().item()
    return loss, acc
