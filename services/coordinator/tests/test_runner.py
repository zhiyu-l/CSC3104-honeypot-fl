"""Tests for the reply filtering in FaultTolerantRunner (no Flower network needed)."""

from collections import Counter

import numpy as np
from flwr.app import ArrayRecord, ConfigRecord, MetricRecord, RecordDict

from fl_common import HoneypotMLP, UpdateMetadata, get_weights
from fl_coordinator.runner import BAD_ARRAYS, FAILED, MALFORMED, FaultTolerantRunner
from fl_coordinator.strategies import make_strategy


class FakeReply:
    """Duck-typed stand-in for a Flower reply Message."""

    def __init__(self, content=None, error=False):
        self.content = content
        self._error = error

    def has_error(self):
        return self._error


GLOBAL = ArrayRecord(get_weights(HoneypotMLP()))


def reply(round_number=3, model_version=2, weights=None, meta=True, **kw):
    weights = weights if weights is not None else get_weights(HoneypotMLP())
    content = {
        "arrays": ArrayRecord(weights),
        "metrics": MetricRecord({"num-examples": 100}),
    }
    if meta:
        m = UpdateMetadata(
            client_id="org-a", round_number=round_number, model_version=model_version,
            num_examples=100, **kw,
        )
        content["meta"] = ConfigRecord(m.to_config())
    return FakeReply(RecordDict(content))


def runner():
    r = FaultTolerantRunner(make_strategy("fedavg"), quorum=2)
    r.current_round, r.model_version = 3, 2
    return r


def test_accepts_good_updates():
    accepted, outcomes = runner().filter_replies([reply(), reply()], GLOBAL)
    assert len(accepted) == 2
    assert outcomes == Counter({"accept": 2})


def test_rejects_failed_stale_and_malformed():
    replies = [
        FakeReply(error=True),
        reply(round_number=2),
        reply(model_version=1),
        reply(meta=False),
        reply(),
    ]
    accepted, outcomes = runner().filter_replies(replies, GLOBAL)
    assert len(accepted) == 1
    assert outcomes == Counter(
        {FAILED: 1, "stale_round": 1, "stale_model": 1, MALFORMED: 1, "accept": 1}
    )


def test_duplicate_rejected_across_attempts():
    r = runner()
    first = reply(update_id="abc")
    r.filter_replies([first], GLOBAL)
    accepted, outcomes = r.filter_replies([reply(update_id="abc")], GLOBAL)
    assert accepted == []
    assert outcomes == Counter({"duplicate": 1})


def test_rejects_nan_and_wrong_shape():
    nan_weights = get_weights(HoneypotMLP())
    nan_weights[0][0, 0] = np.nan
    wrong_shape = get_weights(HoneypotMLP())
    wrong_shape[0] = np.zeros((2, 2), dtype=np.float32)
    accepted, outcomes = runner().filter_replies(
        [reply(weights=nan_weights), reply(weights=wrong_shape)], GLOBAL
    )
    assert accepted == []
    assert outcomes == Counter({BAD_ARRAYS: 2})
