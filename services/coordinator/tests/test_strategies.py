"""Tests for make_strategy and the aggregation each strategy performs (no Flower network)."""

import re
from pathlib import Path

import numpy as np
import pytest
from flwr.app import ArrayRecord, ConfigRecord, MetricRecord, RecordDict
from flwr.serverapp.strategy import FedAvg, FedMedian, FedProx

from fl_common import HoneypotMLP, get_weights
from fl_coordinator.adaptive import MEAN, MEDIAN, AdaptiveStrategy, Mode, choose_mode
from fl_coordinator.strategies import STRATEGIES, STRATEGY_SPECS, make_strategy


class FakeReply:
    """Duck-typed stand-in for a Flower reply Message."""

    def __init__(self, content):
        self.content = content

    def has_error(self):
        return False


def reply(weights, num_examples=100):
    return FakeReply(RecordDict({
        "arrays": ArrayRecord(weights),
        "metrics": MetricRecord({"num-examples": num_examples, "train_loss": 0.5}),
    }))


def shifted(base, delta):
    """Copy of `base` with `delta` added to every weight."""
    return [w + np.float32(delta) for w in base]


def aggregate(strategy, weight_sets, num_examples=None):
    num_examples = num_examples or [100] * len(weight_sets)
    replies = [reply(w, n) for w, n in zip(weight_sets, num_examples)]
    arrays, metrics = strategy.aggregate_train(1, replies)
    return arrays.to_numpy_ndarrays(), metrics


BASE = get_weights(HoneypotMLP())


@pytest.mark.parametrize("name, cls", [
    ("fedavg", FedAvg), ("fedmedian", FedMedian), ("fedprox", FedProx), ("FedMedian", FedMedian),
    ("adaptive", AdaptiveStrategy), ("adaptive-prox", AdaptiveStrategy),
])
def test_make_strategy_returns_right_class(name, cls):
    assert type(make_strategy(name)) is cls


def test_adaptive_prox_gets_mu_and_plain_adaptive_does_not():
    assert make_strategy("adaptive-prox", proximal_mu=0.5).proximal_mu == 0.5
    # plain adaptive keeps mu=0 so earlier adaptive runs stay reproducible
    assert make_strategy("adaptive", proximal_mu=0.5).proximal_mu == 0.0
    with pytest.raises(ValueError):
        make_strategy("adaptive-prox", proximal_mu=-1.0)


def test_unknown_strategy_rejected():
    with pytest.raises(ValueError, match="unknown strategy"):
        make_strategy("fedfoo")


def test_fedprox_gets_mu_and_negative_mu_rejected():
    assert make_strategy("fedprox", proximal_mu=0.5).proximal_mu == 0.5
    with pytest.raises(ValueError):
        make_strategy("fedprox", proximal_mu=-1.0)


def test_proximal_mu_ignored_by_other_strategies():
    # server_app always passes proximal_mu; non-FedProx strategies must not choke on it.
    assert type(make_strategy("fedavg", proximal_mu=0.5)) is FedAvg


def test_fedavg_is_weighted_by_examples():
    out, _ = aggregate(make_strategy("fedavg"), [shifted(BASE, 0.0), shifted(BASE, 1.0)], [300, 100])
    np.testing.assert_allclose(out[0], BASE[0] + 0.25, atol=1e-5)


def test_median_resists_one_poisoned_org_but_average_does_not():
    good = [shifted(BASE, 0.0), shifted(BASE, 0.01)]
    poisoned = [w * np.float32(-5) + np.float32(10) for w in BASE]
    sets = good + [poisoned]

    median, _ = aggregate(make_strategy("fedmedian"), sets)
    mean, _ = aggregate(make_strategy("fedavg"), sets)

    assert np.abs(median[0] - BASE[0]).max() < 0.02
    assert np.abs(mean[0] - BASE[0]).max() > 1.0


def test_aggregated_shapes_match_model():
    for name in ("fedavg", "fedmedian", "fedprox", "adaptive", "adaptive-prox"):
        out, _ = aggregate(make_strategy(name), [BASE, shifted(BASE, 0.1), shifted(BASE, 0.2)])
        assert [a.shape for a in out] == [w.shape for w in BASE]


class FakeGrid:
    def get_node_ids(self):
        return [1, 2, 3]


def train_config_sent(monkeypatch, strategy):
    """The config record `strategy.configure_train` would send to clients."""
    seen = {}

    def fake_construct(record, node_ids, message_type):
        seen.update(record["config"])
        return []

    monkeypatch.setattr(strategy, "_construct_messages", fake_construct)
    monkeypatch.setattr(
        "flwr.serverapp.strategy.fedavg.sample_nodes", lambda grid, min_avail, n: ([1, 2, 3], [1, 2, 3])
    )
    strategy.configure_train(1, ArrayRecord(BASE), ConfigRecord({}), FakeGrid())
    return seen


@pytest.mark.parametrize("name", ["fedprox", "adaptive-prox"])
def test_prox_strategies_send_mu_to_clients(monkeypatch, name):
    strategy = make_strategy(name, proximal_mu=0.3, min_available_nodes=1, min_train_nodes=1)
    assert train_config_sent(monkeypatch, strategy)["proximal-mu"] == pytest.approx(0.3)


def test_plain_adaptive_sends_no_mu(monkeypatch):
    strategy = make_strategy("adaptive", proximal_mu=0.3, min_available_nodes=1, min_train_nodes=1)
    assert "proximal-mu" not in train_config_sent(monkeypatch, strategy)


# ---- adaptive -------------------------------------------------------------

RNG = np.random.default_rng(0)


def noisy(base, scale):
    """`base` plus independent noise, like an honest org's local update."""
    return [w + RNG.normal(0, scale, w.shape).astype(np.float32) for w in base]


def poison(base):
    return [w * np.float32(-5) + np.float32(10) for w in base]


def test_choose_mode_averages_similar_updates():
    updates = [noisy(BASE, 0.01) for _ in range(3)]
    assert choose_mode(updates) == (MEAN, [])


def test_choose_mode_flags_poisoned_update():
    updates = [noisy(BASE, 0.01), poison(BASE), noisy(BASE, 0.01)]
    assert choose_mode(updates) == (MEDIAN, [1])


def test_choose_mode_needs_three_updates():
    assert choose_mode([noisy(BASE, 0.01), poison(BASE)]) == (MEAN, [])


def test_choose_mode_identical_updates_not_flagged():
    assert choose_mode([BASE, BASE, BASE]) == (MEAN, [])


def test_outlier_factor_must_exceed_one():
    with pytest.raises(ValueError):
        AdaptiveStrategy(outlier_factor=1.0)


def test_adaptive_matches_fedavg_when_no_outlier():
    sets = [noisy(BASE, 0.01) for _ in range(3)]
    examples = [100, 200, 300]
    adaptive, metrics = aggregate(make_strategy("adaptive"), sets, examples)
    fedavg, _ = aggregate(make_strategy("fedavg"), sets, examples)
    for a, b in zip(adaptive, fedavg):
        np.testing.assert_allclose(a, b, atol=1e-6)
    assert metrics["agg-robust"] == 0 and metrics["agg-flagged"] == 0


def test_adaptive_uses_median_and_ignores_poisoned_org():
    strategy = make_strategy("adaptive")
    sets = [noisy(BASE, 0.01), noisy(BASE, 0.01), poison(BASE)]
    out, metrics = aggregate(strategy, sets)

    assert metrics["agg-robust"] == 1 and metrics["agg-flagged"] == 1
    assert metrics["train_loss"] == pytest.approx(0.5)  # normal metrics still aggregated
    assert np.abs(out[0] - BASE[0]).max() < 0.1
    assert [a.dtype for a in out] == [w.dtype for w in BASE]
    assert [(d.mode, d.flagged) for d in strategy.decisions] == [(MEDIAN, [2])]


def test_outlier_factor_sets_how_far_is_too_far():
    # Third update is exactly 4x further from the median than the typical update.
    near = [BASE, shifted(BASE, 0.01), shifted(BASE, 0.05)]
    assert choose_mode(near, outlier_factor=5.0) == (MEAN, [])
    assert choose_mode(near, outlier_factor=3.0) == (MEDIAN, [2])


# ---- adaptive: checked vs unchecked rounds --------------------------------
# A round with fewer than MIN_UPDATES_FOR_DETECTION updates cannot be checked for
# outliers. It still averages (unchanged behaviour), but that must be visible:
# otherwise "averaged, nothing suspicious" and "averaged, protection off" look the same.


def test_mode_is_a_string_enum_compatible_with_plain_strings():
    assert Mode.MEAN == "mean" and Mode.MEDIAN == "median"
    assert MEAN is Mode.MEAN and MEDIAN is Mode.MEDIAN
    assert f"{Mode.MEDIAN}" == "median"


def test_checked_round_is_recorded_as_checked():
    strategy = make_strategy("adaptive")
    _, metrics = aggregate(strategy, [noisy(BASE, 0.01) for _ in range(3)])
    assert metrics["agg-checked"] == 1
    assert strategy.decisions[-1].checked is True


def test_round_too_small_to_check_still_averages_but_says_so():
    strategy = make_strategy("adaptive")
    sets = [noisy(BASE, 0.01), poison(BASE)]
    out, metrics = aggregate(strategy, sets)
    fedavg, _ = aggregate(make_strategy("fedavg"), sets)
    for a, b in zip(out, fedavg):  # behaviour unchanged: plain average
        np.testing.assert_allclose(a, b, atol=1e-6)
    assert metrics["agg-checked"] == 0 and metrics["agg-robust"] == 0
    d = strategy.decisions[-1]
    assert (d.mode, d.checked, d.num_updates) == (MEAN, False, 2)


# ---- strategy registry: one source of truth -------------------------------

REPO = Path(__file__).resolve().parents[3]


def test_registry_lists_every_strategy_and_builds_each():
    assert STRATEGIES == tuple(STRATEGY_SPECS)
    for name in STRATEGIES:
        make_strategy(name)


@pytest.mark.parametrize("name, uses_mu, uses_factor", [
    ("fedavg", False, False), ("fedmedian", False, False), ("fedprox", True, False),
    ("adaptive", False, True), ("adaptive-prox", True, True),
])
def test_registry_says_which_settings_each_strategy_uses(name, uses_mu, uses_factor):
    spec = STRATEGY_SPECS[name]
    assert (spec.uses_proximal_mu, spec.uses_outlier_factor) == (uses_mu, uses_factor)


def test_run_pipeline_offers_exactly_the_registered_strategies():
    path = REPO / "tools" / "run_pipeline.py"
    if not path.is_file():
        pytest.skip("tools/run_pipeline.py not present")
    m = re.search(r'"--strategy".*?choices=\[([^\]]*)\]', path.read_text(encoding="utf-8"))
    assert m, "could not find the --strategy choices in run_pipeline.py"
    assert tuple(re.findall(r'"([^"]+)"', m.group(1))) == STRATEGIES


def test_flower_app_config_comment_lists_the_registered_strategies():
    path = REPO / "deploy" / "flower-app" / "pyproject.toml"
    if not path.is_file():
        pytest.skip("deploy/flower-app/pyproject.toml not present")
    m = re.search(r'^strategy\s*=.*#\s*(.+)$', path.read_text(encoding="utf-8"), re.M)
    assert m, "could not find the strategy line in pyproject.toml"
    assert tuple(s.strip() for s in m.group(1).split("|")) == STRATEGIES
