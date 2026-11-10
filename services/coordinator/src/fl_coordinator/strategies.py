"""Aggregation strategies, chosen by config name.

Owned by Amos. Strategies only combine weights: quorum, retries, stale and
duplicate rejection all live in runner.py so they apply to every strategy.

    fedavg     average weighted by each org's number of examples
    fedmedian  coordinate-wise median (resists one org sending bad weights)
    fedprox    FedAvg plus a proximal term on the clients; sends "proximal-mu"
               in the train config, which fl_client.task.train_local uses
    adaptive   FedAvg normally, median in rounds where an update is an outlier
               (see adaptive.py)
    adaptive-prox  adaptive, plus the FedProx term on the clients (sends
               "proximal-mu" like fedprox)

STRATEGY_SPECS below is the single list of strategies: STRATEGIES, make_strategy
and the error message all come from it. Adding a strategy = one entry there,
then add the name to tools/run_pipeline.py --strategy and the strategy comment in
deploy/flower-app/pyproject.toml (test_strategies.py fails until both match).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from flwr.serverapp.strategy import FedAvg, FedMedian, FedProx, Strategy

from fl_coordinator.adaptive import AdaptiveStrategy


@dataclass(frozen=True)
class StrategySpec:
    """How to build one strategy, and which of the shared run settings it uses."""

    build: Callable[..., Strategy]  # build(proximal_mu=..., outlier_factor=..., **config)
    uses_proximal_mu: bool = False
    uses_outlier_factor: bool = False


STRATEGY_SPECS: dict[str, StrategySpec] = {
    "fedavg": StrategySpec(lambda *, proximal_mu, outlier_factor, **c: FedAvg(**c)),
    "fedmedian": StrategySpec(lambda *, proximal_mu, outlier_factor, **c: FedMedian(**c)),
    "fedprox": StrategySpec(
        lambda *, proximal_mu, outlier_factor, **c: FedProx(proximal_mu=proximal_mu, **c),
        uses_proximal_mu=True,
    ),
    "adaptive": StrategySpec(
        lambda *, proximal_mu, outlier_factor, **c: AdaptiveStrategy(outlier_factor=outlier_factor, **c),
        uses_outlier_factor=True,
    ),
    "adaptive-prox": StrategySpec(
        lambda *, proximal_mu, outlier_factor, **c: AdaptiveStrategy(
            outlier_factor=outlier_factor, proximal_mu=proximal_mu, **c
        ),
        uses_proximal_mu=True,
        uses_outlier_factor=True,
    ),
}

STRATEGIES = tuple(STRATEGY_SPECS)


def make_strategy(
    name: str, *, proximal_mu: float = 0.1, outlier_factor: float = 5.0, **config
) -> Strategy:
    """Build the strategy called `name`. `config` goes to the Flower strategy as-is.

    `proximal_mu` is only used by fedprox and adaptive-prox, `outlier_factor`
    only by adaptive and adaptive-prox (see STRATEGY_SPECS); others ignore them.
    """
    spec = STRATEGY_SPECS.get(name.lower())
    if spec is None:
        raise ValueError(f"unknown strategy {name!r}; choose from {', '.join(STRATEGIES)}")
    if spec.uses_proximal_mu and proximal_mu < 0:
        raise ValueError(f"proximal-mu must be >= 0, got {proximal_mu}")
    return spec.build(proximal_mu=proximal_mu, outlier_factor=outlier_factor, **config)
