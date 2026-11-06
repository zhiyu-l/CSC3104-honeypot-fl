"""Adaptive aggregation: average normally, switch to the median when an update looks off.

Each org sees different attacks, so their updates legitimately differ; averaging
them (FedAvg) is what lets every org benefit from the others. But one org can
also send weights that are badly off (poisoned, buggy, trained on garbage) while
still passing the runner's checks (right shapes, no NaN). Averaging lets that
one update drag the global model away; the coordinate-wise median does not.

So every round this strategy looks at the accepted updates:

    1. Take the coordinate-wise median of all updates.
    2. Measure each update's distance to it.
    3. If some update is more than `outlier_factor` times the typical
       (median) distance away, it is an outlier: aggregate with the median.
       Otherwise use FedAvg's example-weighted average.

Detection needs at least 3 updates (with 2 there is no majority to compare
against), so with fewer it always averages.

Limits (measured on synthetic data, 3 orgs, 12 runs x 6 rounds of real local
training):

- Large attacks are caught: an update scaled by -5 scored >= 6.3 every time,
  so the default factor of 5 flags all of them.
- Honest orgs are sometimes flagged (2 of 72 rounds at factor 5, 5 of 72 at
  3). This happens when two orgs have converged and barely move while the
  third is still learning an attack type only it sees. From the weights alone,
  with 3 orgs and no data on the coordinator, that looks the same as an
  attacker; in those rounds the median drops that org's update. Trying
  step-size ratios or norm clipping instead did not do measurably better.
- Subtle attacks (e.g. reversing the update, small noise) are not detected;
  the median only limits them when detection fires.

As with every strategy, accepting/rejecting updates stays in runner.py; this
only decides HOW to combine the updates the runner accepted.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from logging import INFO

import numpy as np
from flwr.app import ArrayRecord, Message, MetricRecord
from flwr.common.logger import log
from flwr.serverapp.strategy import FedAvg
from flwr.serverapp.strategy.strategy_utils import aggregate_arrayrecords

MEAN = "mean"
MEDIAN = "median"
MIN_UPDATES_FOR_DETECTION = 3


def _flatten(update: Sequence[np.ndarray]) -> np.ndarray:
    return np.concatenate([np.asarray(a, dtype=np.float64).ravel() for a in update])


def choose_mode(
    updates: Sequence[Sequence[np.ndarray]], outlier_factor: float = 5.0
) -> tuple[str, list[int]]:
    """Return (MEAN or MEDIAN, indices of the updates judged to be outliers)."""
    if len(updates) < MIN_UPDATES_FOR_DETECTION:
        return MEAN, []

    flat = np.stack([_flatten(u) for u in updates])
    centre = np.median(flat, axis=0)
    dists = np.linalg.norm(flat - centre, axis=1)

    # Typical distance; the floor stops near-identical updates (typical ~ 0)
    # from flagging tiny float differences as outliers.
    typical = max(float(np.median(dists)), 1e-6 * (float(np.linalg.norm(centre)) + 1.0))
    flagged = [i for i, d in enumerate(dists) if d > outlier_factor * typical]
    return (MEDIAN if flagged else MEAN), flagged


def median_aggregate(updates: Sequence[Sequence[np.ndarray]]) -> list[np.ndarray]:
    """Coordinate-wise median, layer by layer, keeping each layer's dtype."""
    return [
        np.median(np.stack([u[i] for u in updates]), axis=0).astype(updates[0][i].dtype)
        for i in range(len(updates[0]))
    ]


@dataclass
class RoundDecision:
    round: int
    mode: str
    flagged: list[int]
    num_updates: int


class AdaptiveStrategy(FedAvg):
    """FedAvg that switches to the coordinate-wise median when an update is an outlier."""

    def __init__(self, *, outlier_factor: float = 5.0, **kwargs) -> None:
        if outlier_factor <= 1.0:
            raise ValueError(f"outlier-factor must be > 1, got {outlier_factor}")
        super().__init__(**kwargs)
        self.outlier_factor = outlier_factor
        self.decisions: list[RoundDecision] = []

    def aggregate_train(
        self, server_round: int, replies: Iterable[Message]
    ) -> tuple[ArrayRecord | None, MetricRecord | None]:
        valid, _ = self._check_and_log_replies(list(replies), is_train=True)
        if not valid:
            return None, None

        contents = [msg.content for msg in valid]
        updates = [c[self.arrayrecord_key].to_numpy_ndarrays() for c in contents]
        mode, flagged = choose_mode(updates, self.outlier_factor)

        # Aggregate directly rather than via super().aggregate_train, which would
        # re-run (and re-log) the reply check above.
        if mode == MEAN:
            arrays = aggregate_arrayrecords(contents, self.weighted_by_key)
        else:
            arrays = ArrayRecord(median_aggregate(updates))
        metrics = self.train_metrics_aggr_fn(contents, self.weighted_by_key)

        self.decisions.append(RoundDecision(server_round, mode, flagged, len(updates)))
        log(
            INFO,
            "adaptive: round %s used %s of %s updates%s",
            server_round, mode, len(updates),
            f" (outliers at positions {flagged})" if flagged else "",
        )

        metrics = metrics if metrics is not None else MetricRecord()
        metrics["agg-robust"] = int(mode == MEDIAN)
        metrics["agg-flagged"] = len(flagged)
        return arrays, metrics


__all__ = ["AdaptiveStrategy", "MEAN", "MEDIAN", "RoundDecision", "choose_mode", "median_aggregate"]
