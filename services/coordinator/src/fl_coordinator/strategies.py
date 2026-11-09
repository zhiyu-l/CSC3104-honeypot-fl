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
"""

from __future__ import annotations

from flwr.serverapp.strategy import FedAvg, FedMedian, FedProx, Strategy

from fl_coordinator.adaptive import AdaptiveStrategy

STRATEGIES = ("fedavg", "fedmedian", "fedprox", "adaptive", "adaptive-prox")


def make_strategy(
    name: str, *, proximal_mu: float = 0.1, outlier_factor: float = 5.0, **config
) -> Strategy:
    """Build the strategy called `name`. `config` goes to the Flower strategy as-is.

    `proximal_mu` is only used by fedprox and adaptive-prox, `outlier_factor`
    only by adaptive and adaptive-prox.
    """
    name = name.lower()
    if name == "fedavg":
        return FedAvg(**config)
    if name == "fedmedian":
        return FedMedian(**config)
    if name == "fedprox":
        if proximal_mu < 0:
            raise ValueError(f"proximal-mu must be >= 0, got {proximal_mu}")
        return FedProx(proximal_mu=proximal_mu, **config)
    if name == "adaptive":
        return AdaptiveStrategy(outlier_factor=outlier_factor, **config)
    if name == "adaptive-prox":
        return AdaptiveStrategy(outlier_factor=outlier_factor, proximal_mu=proximal_mu, **config)
    raise ValueError(f"unknown strategy {name!r}; choose from {', '.join(STRATEGIES)}")
