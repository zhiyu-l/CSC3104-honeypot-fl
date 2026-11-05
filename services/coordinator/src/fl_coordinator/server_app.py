"""Flower ServerApp: the coordinator.

Run settings come from [tool.flwr.app.config] in deploy/flower-app/pyproject.toml
(or `flwr run ... --run-config "key=value"`).
"""

from __future__ import annotations

import numpy as np
import torch
from flwr.app import ArrayRecord, Context
from flwr.serverapp import Grid, ServerApp

from fl_common import HoneypotMLP, get_weights
from fl_coordinator.runner import FaultTolerantRunner
from fl_coordinator.strategies import make_strategy

app = ServerApp()


@app.main()
def main(grid: Grid, context: Context) -> None:
    cfg = context.run_config
    quorum = int(cfg.get("quorum", 2))

    torch.manual_seed(int(cfg.get("seed", 0)))
    initial = ArrayRecord(get_weights(HoneypotMLP()))

    strategy = make_strategy(
        str(cfg.get("strategy", "fedavg")),
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
    result = runner.run(grid, initial, num_rounds=int(cfg.get("num-server-rounds", 5)))

    if cfg.get("save-model", False):
        np.savez("final_model", *result.arrays.to_numpy_ndarrays())
        print("Saved final_model.npz")
