"""Test a saved global model on labelled session CSVs (pipeline stage 5).

The CSVs come from tools/cowrie_dataset/cowrie_sessions.py label, normally run
on the logs produced by replaying the held-out attacks. Rows go through the
same validate_row + transform as training, and scan/tunnel sessions are
skipped, exactly like the clients do.

Usage (from the repo root, in the venv with fl-common installed):
    python tools/evaluate_model.py --model models/<run-name> \
        --data data/processed/replay/sessions.csv

--model takes a run folder (uses its model.npz), or a specific checkpoint
such as models/<run-name>/round_003.npz.

Prints accuracy, per-class precision/recall/F1 and a confusion matrix, overall
and per org (the CSV's `honeypot` column: orgA, orgB, orgC for replay logs),
and writes the same numbers to eval_report.json next to the model
(or to --out).
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from collections import Counter
from pathlib import Path

import numpy as np
import torch

from fl_common import FEATURE_NAMES, AttackClass, label_session, load_model, parse_label, transform, validate_row

CLASSES = [c.name.lower() for c in AttackClass]


def read_rows(paths: list[Path]) -> tuple[np.ndarray, np.ndarray, list[str], Counter]:
    """CSV rows -> (X transformed, y, group per row, skip counts)."""
    X, y, groups = [], [], []
    skipped: Counter = Counter()
    for path in paths:
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
        sys.exit("No usable rows in the given CSVs.")
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


def print_block(title: str, m: dict) -> None:
    print(f"\n=== {title}: {m['n']} sessions, accuracy {m['accuracy']:.3f}, macro F1 {m['macro_f1']:.3f}")
    print(f"{'class':<18}{'precision':>10}{'recall':>10}{'f1':>10}{'support':>10}")
    for name, c in m["per_class"].items():
        print(f"{name:<18}{c['precision']:>10.3f}{c['recall']:>10.3f}{c['f1']:>10.3f}{c['support']:>10}")
    print("confusion matrix (rows = true, columns = predicted):")
    short = [n[:10] for n in CLASSES]
    print(" " * 18 + "".join(f"{s:>12}" for s in short))
    for name, row in zip(CLASSES, m["confusion_matrix"]["matrix"]):
        print(f"{name:<18}" + "".join(f"{v:>12}" for v in row))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model", required=True, help="run folder, or a .npz checkpoint")
    ap.add_argument("--data", required=True, nargs="+", type=Path, help="labelled session CSV(s)")
    ap.add_argument("--out", type=Path, help="report path (default: eval_report.json next to the model)")
    args = ap.parse_args()

    model_path = Path(args.model)
    if model_path.is_dir():
        model_path = model_path / "model.npz"
    model, info = load_model(model_path)
    print(f"Model {model_path} (run {info.get('run')}, round {info.get('round')}, "
          f"v{info.get('model_version')}, {info.get('strategy')})")

    X, y, groups, skipped = read_rows(args.data)
    with torch.no_grad():
        pred = model(torch.from_numpy(X)).argmax(dim=1).numpy()

    report = {
        "model": str(model_path),
        "model_info": info,
        "data": [str(p) for p in args.data],
        "evaluated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "skipped_rows": dict(skipped),
        "overall": metrics(y, pred),
        "per_org": {},
    }
    print_block("overall", report["overall"])
    g = np.asarray(groups)
    for org in sorted(set(groups)):
        mask = g == org
        report["per_org"][org] = metrics(y[mask], pred[mask])
        print_block(org, report["per_org"][org])
    if skipped:
        print(f"\nSkipped rows: {dict(skipped)}")

    out = args.out or model_path.with_name(
        "eval_report.json" if model_path.stem == "model" else f"eval_report_{model_path.stem}.json"
    )
    out.write_text(json.dumps(report, indent=2, default=str))
    print(f"\nReport written to {out}")


if __name__ == "__main__":
    main()
