"""Saving the global model during and after a run, and resuming a run.

Only the GLOBAL model is saved. Client weights are intermediate: they are
replaced by the next global model one round later, and keeping them would
hold on to per-org information that federated learning is meant to keep local.

Layout of one run (model-dir defaults to $FL_MODEL_DIR, else ./models):

    <model-dir>/<run-name>/
        round_001.npz / .json   global model after each successful round
        round_002.npz / .json
        ...
        model.npz / model.json  final global model (the latest successful round)
        history.json            run settings + per-round outcomes, eval metrics and the
                                data each org trained on; at the end also where the run
                                started from, the fixed-test-set score and whether it
                                was promoted to latest (see publish.py)
        eval_report.json        fixed-test-set score of model.npz (when eval-data is set)

Every .npz/.json pair is written with fl_common.save_model, so it carries the
schema/architecture versions and a SHA-256 that fl_common.load_model checks.

Resuming: set resume-from to an existing run folder. The coordinator loads the
newest round_NNN checkpoint, continues from the next round with the same model
version, and keeps writing into the same folder.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any, Mapping

import numpy as np
from flwr.app import ArrayRecord, MetricRecord

from fl_common import load_weights, save_model
from fl_coordinator.runner import RoundLog

_ROUND_FILE = re.compile(r"^round_(\d+)\.npz$")


def default_model_dir() -> Path:
    return Path(os.environ.get("FL_MODEL_DIR") or "models")


def _metrics(m: MetricRecord | None) -> dict[str, float]:
    return {k: float(v) for k, v in dict(m).items()} if m is not None else {}


class RunStore:
    def __init__(self, run_dir: Path, settings: Mapping[str, Any]) -> None:
        self.run_dir = run_dir
        self.settings = dict(settings)
        self.rounds: list[dict[str, Any]] = []
        self.last_saved: dict[str, Any] | None = None
        self.failed_rounds: list[int] = []
        self.extra: dict[str, Any] = {}  # run-level facts added to history.json (init_from, evaluation, ...)
        history = run_dir / "history.json"
        if history.is_file():  # resumed run: keep the earlier rounds
            old = json.loads(history.read_text())
            self.rounds = old.get("rounds", [])
            self.extra = {k: old[k] for k in ("init_from", "bootstrap") if k in old}

    # ---- creating / resuming ----------------------------------------------------

    @classmethod
    def new(cls, model_dir: Path, run_name: str, settings: Mapping[str, Any]) -> "RunStore":
        name = run_name or time.strftime("%Y%m%d-%H%M%S", time.gmtime()) + f"-{settings.get('strategy', 'run')}"
        run_dir = model_dir / name
        if run_dir.exists() and any(run_dir.iterdir()):
            raise FileExistsError(
                f"{run_dir} already has files. Pick another run-name, or set resume-from to continue it."
            )
        run_dir.mkdir(parents=True, exist_ok=True)
        return cls(run_dir, settings)

    @classmethod
    def resume(cls, run_dir: Path, settings: Mapping[str, Any]) -> tuple["RunStore", list[np.ndarray], int, int]:
        """Returns (store, weights, last_round, model_version) from the newest checkpoint."""
        rounds = sorted(
            int(m.group(1)) for p in run_dir.glob("round_*.npz") if (m := _ROUND_FILE.match(p.name))
        )
        if not rounds:
            raise FileNotFoundError(f"no round_NNN.npz checkpoints in {run_dir}")
        last = rounds[-1]
        weights, info = load_weights(run_dir / f"round_{last:03d}")
        store = cls(run_dir, settings)
        store.rounds = [r for r in store.rounds if r["round"] <= last]
        store.last_saved = info
        print(f"Resuming {run_dir} from round {last} (model v{info['model_version']})")
        return store, weights, last, int(info["model_version"])

    # ---- called by the runner after each successful round --------------------

    def save_round(self, log: RoundLog, arrays: ArrayRecord, eval_metrics: MetricRecord | None) -> None:
        info = {
            "run": self.run_dir.name,
            "round": log.round,
            "model_version": log.model_version,
            "strategy": self.settings.get("strategy"),
            "eval_metrics": _metrics(eval_metrics),
        }
        save_model(self.run_dir / f"round_{log.round:03d}", arrays.to_numpy_ndarrays(), info)
        self.last_saved = info
        self.rounds.append(
            {
                "round": log.round,
                "attempts": log.attempts,
                "model_version": log.model_version,
                "outcomes": dict(log.outcomes),
                "eval_metrics": info["eval_metrics"],
                "data": log.data,
            }
        )
        self._write_history(finished=False)
        print(f"Saved checkpoint {self.run_dir / f'round_{log.round:03d}.npz'}")

    # ---- end of run ----------------------------------------------------------

    def save_final(self, arrays: ArrayRecord, failed_rounds: list[int]) -> dict[str, Any] | None:
        """Write model.npz/.json. Returns its info, or None if no round succeeded."""
        self.failed_rounds = failed_rounds
        if self.last_saved is None:
            print("No round succeeded, so there is no global model to save.")
            self._write_history(finished=True, failed_rounds=failed_rounds)
            return None
        info = {**self.last_saved, "final": True}
        path = save_model(self.run_dir / "model", arrays.to_numpy_ndarrays(), info)
        self._write_history(finished=True, failed_rounds=failed_rounds)
        print(f"Saved final global model {path.resolve()} (round {info['round']}, v{info['model_version']})")
        return info

    def add_to_history(self, **facts: Any) -> None:
        """Record run-level facts (evaluation, promotion, ...) in history.json."""
        self.extra.update(facts)
        self._write_history(finished=True, failed_rounds=self.failed_rounds)

    def _write_history(self, finished: bool, failed_rounds: list[int] | None = None) -> None:
        data = {
            "run": self.run_dir.name,
            "settings": self.settings,
            "finished": finished,
            "failed_rounds": failed_rounds or [],
            **self.extra,
            "rounds": self.rounds,
        }
        tmp = self.run_dir / "history.json.tmp"
        tmp.write_text(json.dumps(data, indent=2, default=str))
        os.replace(tmp, self.run_dir / "history.json")
