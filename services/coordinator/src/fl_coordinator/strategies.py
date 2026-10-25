"""Aggregation strategies, chosen by config name.

Owned by Amos. Strategies only combine weights: quorum, retries, stale and
duplicate rejection all live in runner.py so they apply to every strategy.

Only `fedavg` is wired in so far. To add the others, map the names to
flwr.serverapp.strategy.FedMedian and FedProx (FedProx sends "proximal-mu"
to clients in the train config; the client already uses that key).
"""

from __future__ import annotations

from flwr.serverapp.strategy import FedAvg, Strategy


def make_strategy(name: str, **config) -> Strategy:
    name = name.lower()
    if name == "fedavg":
        return FedAvg(**config)
    raise ValueError(f"unknown strategy {name!r}")
