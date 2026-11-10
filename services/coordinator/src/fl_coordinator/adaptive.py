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
against), so with fewer it always averages. Such a round is marked unchecked
(RoundDecision.checked=False, metric agg-checked=0) and logged as a warning:
with quorum 2, one crashed client is enough to switch protection off for a round.

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

With `proximal_mu > 0` (strategy name `adaptive-prox`) it also sends
"proximal-mu" in the train config, exactly like Flower's FedProx, so clients
add the FedProx term (mu/2)*||w - w_global||^2 to their loss. That works on
the client side (it limits how far each org drifts on its own non-IID data)
and is independent of the server-side average/median choice above.

As with every strategy, accepting/rejecting updates stays in runner.py; this
only decides HOW to combine the updates the runner accepted.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from logging import INFO, WARN

import numpy as np
from flwr.app import ArrayRecord, ConfigRecord, Message, MetricRecord
from flwr.common.logger import log
from flwr.serverapp import Grid
from flwr.serverapp.strategy import FedAvg
from flwr.serverapp.strategy.strategy_utils import aggregate_arrayrecords

class Mode(StrEnum):
    """How a round's updates were combined. A StrEnum, so Mode.MEAN == "mean"."""

    MEAN = "mean"
    MEDIAN = "median"


MEAN = Mode.MEAN
MEDIAN = Mode.MEDIAN
MIN_UPDATES_FOR_DETECTION = 3

# Each round ends in exactly one of three outcomes (RoundDecision / train metrics):
#
#   updates < MIN_UPDATES_FOR_DETECTION --> MEAN,   checked=False   agg-checked=0 agg-robust=0
#       (too few to compare; protection is OFF this round, logged as a warning)
#   checked, no outlier                 --> MEAN,   checked=True    agg-checked=1 agg-robust=0
#   checked, >= 1 outlier               --> MEDIAN, checked=True    agg-checked=1 agg-robust=1
#
# No state carries over between rounds: each round is decided from its own updates.


def _flatten(update: Sequence[np.ndarray]) -> np.ndarray:
    return np.concatenate([np.asarray(a, dtype=np.float64).ravel() for a in update])


def can_check(num_updates: int) -> bool:
    """Whether a round with this many updates can be checked for outliers."""
    return num_updates >= MIN_UPDATES_FOR_DETECTION


def choose_mode(
    updates: Sequence[Sequence[np.ndarray]], outlier_factor: float = 5.0
) -> tuple[Mode, list[int]]:
    """Return (MEAN or MEDIAN, indices of the updates judged to be outliers).

    With fewer than MIN_UPDATES_FOR_DETECTION updates nothing is checked and the
    result is (MEAN, []); use can_check() to tell that apart from a clean round.
    """
    if not can_check(len(updates)):
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
    """What adaptive did in one round (see the outcome table above)."""

    round: int
    mode: Mode
    flagged: list[int]
    num_updates: int
    checked: bool = True  # False: too few updates to look for outliers


class AdaptiveStrategy(FedAvg):
    """FedAvg that switches to the coordinate-wise median when an update is an outlier.

    `proximal_mu > 0` also makes clients train with the FedProx term.
    """

    def __init__(self, *, outlier_factor: float = 5.0, proximal_mu: float = 0.0, **kwargs) -> None:
        if outlier_factor <= 1.0:
            raise ValueError(f"outlier-factor must be > 1, got {outlier_factor}")
        if proximal_mu < 0:
            raise ValueError(f"proximal-mu must be >= 0, got {proximal_mu}")
        super().__init__(**kwargs)
        self.outlier_factor = outlier_factor
        self.proximal_mu = proximal_mu
        self.decisions: list[RoundDecision] = []

    def configure_train(
        self, server_round: int, arrays: ArrayRecord, config: ConfigRecord, grid: Grid
    ) -> Iterable[Message]:
        # Same key and behaviour as Flower's FedProx; with mu = 0 nothing is sent,
        # so clients train exactly as under plain FedAvg.
        if self.proximal_mu > 0:
            config["proximal-mu"] = self.proximal_mu
        return super().configure_train(server_round, arrays, config, grid)

    def aggregate_train(
        self, server_round: int, replies: Iterable[Message]
    ) -> tuple[ArrayRecord | None, MetricRecord | None]:
        valid, _ = self._check_and_log_replies(list(replies), is_train=True)
        if not valid:
            return None, None

        contents = [msg.content for msg in valid]
        updates = [c[self.arrayrecord_key].to_numpy_ndarrays() for c in contents]
        mode, flagged = choose_mode(updates, self.outlier_factor)
        checked = can_check(len(updates))

        # Aggregate directly rather than via super().aggregate_train, which would
        # re-run (and re-log) the reply check above.
        if mode is Mode.MEAN:
            arrays = aggregate_arrayrecords(contents, self.weighted_by_key)
        else:
            arrays = ArrayRecord(median_aggregate(updates))
        metrics = self.train_metrics_aggr_fn(contents, self.weighted_by_key)

        self.decisions.append(RoundDecision(server_round, mode, flagged, len(updates), checked))
        if checked:
            log(
                INFO,
                "adaptive: round %s used %s of %s updates%s",
                server_round, mode, len(updates),
                f" (outliers at positions {flagged})" if flagged else " (no outliers)",
            )
        else:
            log(
                WARN,
                "adaptive: round %s used mean of %s updates UNCHECKED: outlier detection "
                "needs at least %s updates, so this round has no protection",
                server_round, len(updates), MIN_UPDATES_FOR_DETECTION,
            )

        metrics = metrics if metrics is not None else MetricRecord()
        metrics["agg-robust"] = int(mode is Mode.MEDIAN)
        metrics["agg-flagged"] = len(flagged)
        metrics["agg-checked"] = int(checked)
        return arrays, metrics


__all__ = [
    "AdaptiveStrategy", "MEAN", "MEDIAN", "MIN_UPDATES_FOR_DETECTION", "Mode", "RoundDecision",
    "can_check", "choose_mode", "median_aggregate",
]
