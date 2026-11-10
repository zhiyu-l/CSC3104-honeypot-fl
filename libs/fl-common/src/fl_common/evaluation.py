"""Scoring a global model on labelled session CSVs.

Used by tools/evaluate_model.py (by hand) and by the coordinator at the end of
every run (eval-data setting), so both report exactly the same numbers. Rows go
through the same validate_row + transform as training, and scan/tunnel sessions
are skipped, exactly like the clients do.
"""

from __future__ import annotations

import csv
import time
from collections import Counter
from pathlib import Path
from typing import Iterable

import numpy as np
import torch

from fl_common.schema import FEATURE_NAMES, AttackClass, label_session, parse_label, transform, validate_row

CLASSES = [c.name.lower() for c in AttackClass]


def read_rows(paths: Iterable[Path]) -> tuple[np.ndarray, np.ndarray, list[str], Counter]:
    """CSV rows -> (X transformed, y, group per row, skip counts). Raises ValueError if no usable rows."""
    X, y, groups = [], [], []
    skipped: Counter = Counter()
    for path in paths:
        path = Path(path)
        with path.open(newline="") as f:
            for raw in csv.DictReader(f):
                try:
                    row = {k: float(raw[k]) for k in FEATURE_NAMES}
                    values = validate_row(row)
                    label = raw.get("label")
                    cls = parse_label(label) if label not in (None, "") else label_session(row)
                except (KeyError, TypeError, ValueError):
                    skipped["invalid"] += 1
                    continue
                if cls is None:
                    skipped[str(raw.get("label") or "excluded")] += 1
                    continue
                X.append(transform(values))
                y.append(int(cls))
                groups.append(raw.get("honeypot") or path.stem)
    if not X:
        raise ValueError("no usable rows in the given CSVs")
    return np.asarray(X, dtype=np.float32), np.asarray(y, dtype=np.int64), groups, skipped


def metrics(y_true: np.ndarray, y_pred: np.ndarray) -> dict:
    k = len(CLASSES)
    cm = np.zeros((k, k), dtype=int)
    np.add.at(cm, (y_true, y_pred), 1)
    per_class = {}
    for i, name in enumerate(CLASSES):
        tp = cm[i, i]
        support = int(cm[i].sum())
        predicted = int(cm[:, i].sum())
        precision = tp / predicted if predicted else 0.0
        recall = tp / support if support else 0.0
        f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
        per_class[name] = {"precision": precision, "recall": recall, "f1": f1, "support": support}
    present = [c for c in per_class.values() if c["support"]]
    return {
        "n": int(len(y_true)),
        "accuracy": float((y_true == y_pred).mean()),
        "macro_f1": float(np.mean([c["f1"] for c in present])) if present else 0.0,
        "per_class": per_class,
        "confusion_matrix": {"rows_true_cols_pred": CLASSES, "matrix": cm.tolist()},
    }


def evaluate(model: torch.nn.Module, paths: Iterable[Path]) -> dict:
    """Score `model` on the CSVs. Returns the report dict (overall + per org)."""
    paths = [Path(p) for p in paths]
    X, y, groups, skipped = read_rows(paths)
    model.eval()
    with torch.no_grad():
        pred = model(torch.from_numpy(X)).argmax(dim=1).numpy()
    report = {
        "data": [str(p) for p in paths],
        "evaluated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "skipped_rows": dict(skipped),
        "overall": metrics(y, pred),
        "per_org": {},
    }
    g = np.asarray(groups)
    for org in sorted(set(groups)):
        mask = g == org
        report["per_org"][org] = metrics(y[mask], pred[mask])
    return report


__all__ = ["CLASSES", "evaluate", "metrics", "read_rows"]
