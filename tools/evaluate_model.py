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
import json
import sys
from pathlib import Path

from fl_common import load_model
from fl_common.evaluation import CLASSES, evaluate


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

    try:
        report = evaluate(model, args.data)
    except ValueError as exc:
        sys.exit(f"Cannot evaluate: {exc}")
    report = {"model": str(model_path), "model_info": info, **report}
    print_block("overall", report["overall"])
    for org, m in report["per_org"].items():
        print_block(org, m)
    if report["skipped_rows"]:
        print(f"\nSkipped rows: {report['skipped_rows']}")

    out = args.out or model_path.with_name(
        "eval_report.json" if model_path.stem == "model" else f"eval_report_{model_path.stem}.json"
    )
    out.write_text(json.dumps(report, indent=2, default=str))
    print(f"\nReport written to {out}")


if __name__ == "__main__":
    main()
