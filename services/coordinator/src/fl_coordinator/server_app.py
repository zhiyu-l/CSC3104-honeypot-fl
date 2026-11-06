"""Flower ServerApp: the coordinator.

Run settings come from [tool.flwr.app.config] in deploy/flower-app/pyproject.toml
(or `flwr run ... --run-config "key=value"`).

The global model is saved after every successful round and at the end of the
run (see checkpoints.py). Set save-model=false to turn that off, and
resume-from=<run folder> to continue a run that stopped part-way.
"""

from __future__ import annotations

from pathlib import Path

import torch
from flwr.app import ArrayRecord, Context
from flwr.serverapp import Grid, ServerApp

from fl_common import HoneypotMLP, get_weights
from fl_coordinator.checkpoints import RunStore, default_model_dir
from fl_coordinator.runner import FaultTolerantRunner
from fl_coordinator.strategies import make_strategy

app = ServerApp()


@app.main()
def main(grid: Grid, context: Context) -> None:
    cfg = context.run_config
    quorum = int(cfg.get("quorum", 2))
    total_rounds = int(cfg.get("num-server-rounds", 5))
    strategy_name = str(cfg.get("strategy", "fedavg"))

    torch.manual_seed(int(cfg.get("seed", 0)))
    initial = ArrayRecord(get_weights(HoneypotMLP()))

    strategy = make_strategy(
        strategy_name,
        proximal_mu=float(cfg.get("proximal-mu", 0.1)),
        outlier_factor=float(cfg.get("outlier-factor", 5.0)),
        fraction_train=1.0,
        fraction_evaluate=1.0,
        min_train_nodes=quorum,
        min_evaluate_nodes=quorum,
        min_available_nodes=quorum,
    )
    runner = FaultTolerantRunner(
        strategy,
        quorum=quorum,
        max_retries=int(cfg.get("max-retries", 2)),
        round_timeout=float(cfg.get("round-timeout", 120.0)),
        abort_on_failed_round=bool(cfg.get("abort-on-failed-round", False)),
    )

    # Where the global model goes, and whether we continue an earlier run.
    store: RunStore | None = None
    start_round = 1
    if bool(cfg.get("save-model", True)):
        settings = {k: v for k, v in cfg.items()}
        resume_from = str(cfg.get("resume-from", "")).strip()
        if resume_from:
            store, weights, last_round, model_version = RunStore.resume(Path(resume_from), settings)
            initial = ArrayRecord(weights)
            runner.model_version = model_version
            start_round = last_round + 1
        else:
            model_dir = Path(str(cfg.get("model-dir", "")).strip() or default_model_dir())
            store = RunStore.new(model_dir, str(cfg.get("run-name", "")).strip(), settings)
            print(f"Saving global model checkpoints to {store.run_dir.resolve()}")

    remaining = total_rounds - start_round + 1
    if remaining <= 0:
        print(f"Run already has {start_round - 1} of {total_rounds} rounds; nothing to train.")
    result = runner.run(
        grid,
        initial,
        num_rounds=max(remaining, 0),
        start_round=start_round,
        on_round_end=store.save_round if store else None,
    )

    if store is not None:
        failed = [log.round for log in runner.history if not log.succeeded]
        store.save_final(result.arrays, failed)
