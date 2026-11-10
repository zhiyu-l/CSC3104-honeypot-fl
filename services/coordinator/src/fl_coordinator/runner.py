"""Fault-tolerant round loop, independent of the aggregation strategy.

Based on Andrew's QuorumFedAvg prototype (quorum + per-round retries), with
the fault-tolerance checks moved out of the strategy so they work the same
for FedAvg, FedMedian and FedProx:

    1. Drop replies that errored (client crashed or timed out).
    2. Check each update's metadata with fl_common.check_update:
       duplicate, stale round, stale model, incompatible, empty.
    3. Check the weights themselves: same shapes as the global model, all finite.
    4. Aggregate with the strategy only if at least `quorum` updates survive.
       Otherwise retry the round, up to `max_retries` times.
    5. If every attempt fails, keep the previous global model and move on
       (or stop, if abort_on_failed_round is set).

model_version only increases when a round produces a new global model.
Clients are told the current version in the train config, so an update
trained from an older model is rejected as STALE_MODEL.
"""

from __future__ import annotations

import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Callable, Iterable

import numpy as np
from flwr.app import ArrayRecord, ConfigRecord, Message, MetricRecord
from flwr.serverapp import Grid
from flwr.serverapp.strategy import Strategy
from flwr.serverapp.strategy.result import Result

from fl_common import UpdateMetadata, Verdict, check_update

META_KEY = "meta"
DATA_KEY = "data"            # optional ConfigRecord: what the client trained on (see fl_client.data)
FAILED = "failed"            # reply carried an error (crash, exception, timeout)
MALFORMED = "malformed"      # missing or unparsable metadata
BAD_ARRAYS = "bad_arrays"    # wrong shapes, NaN or inf


@dataclass
class RoundLog:
    round: int
    attempts: int = 0
    succeeded: bool = False
    model_version: int = 0
    outcomes: Counter = field(default_factory=Counter)
    data: dict[str, dict] = field(default_factory=dict)  # client_id -> data summary of accepted updates


def data_summary(msg: Message) -> tuple[str, dict] | None:
    """(client_id, what it trained on) from an accepted reply, if the client sent it."""
    if DATA_KEY not in msg.content:
        return None
    client = str(dict(msg.content[META_KEY]).get("meta.client_id", "?"))
    return client, {k: v for k, v in dict(msg.content[DATA_KEY]).items()}


def arrays_ok(update: ArrayRecord, reference: ArrayRecord) -> bool:
    """Same keys and shapes as the current global model, and every value finite."""
    if list(update.keys()) != list(reference.keys()):
        return False
    for key in reference.keys():
        a, ref = update[key].numpy(), reference[key].numpy()
        if a.shape != ref.shape or not np.all(np.isfinite(a)):
            return False
    return True


class FaultTolerantRunner:
    def __init__(
        self,
        strategy: Strategy,
        quorum: int = 2,
        max_retries: int = 2,
        round_timeout: float = 120.0,
        abort_on_failed_round: bool = False,
    ) -> None:
        self.strategy = strategy
        self.quorum = quorum
        self.max_retries = max_retries
        self.round_timeout = round_timeout
        self.abort_on_failed_round = abort_on_failed_round

        self.current_round = 0
        self.model_version = 0
        self.seen_update_ids: set[str] = set()
        self.history: list[RoundLog] = []
        # Extra settings sent to clients with every train and evaluate request
        # (e.g. {"bootstrap": True}: train on the base data only, see server_app.py).
        self.round_config: dict[str, int | float | str | bool] = {}

    # ---- filtering (pure, unit tested) --------------------------------------

    def filter_replies(
        self, replies: Iterable[Message], global_arrays: ArrayRecord
    ) -> tuple[list[Message], Counter]:
        """Return the replies safe to aggregate, and a count of every outcome."""
        accepted: list[Message] = []
        outcomes: Counter = Counter()
        for msg in replies:
            if msg.has_error():
                outcomes[FAILED] += 1
                continue
            try:
                meta = UpdateMetadata.from_config(dict(msg.content[META_KEY]))
            except (KeyError, ValueError):
                outcomes[MALFORMED] += 1
                continue
            verdict = check_update(
                meta,
                current_round=self.current_round,
                current_model_version=self.model_version,
                seen_update_ids=self.seen_update_ids,
            )
            if verdict is Verdict.ACCEPT and not arrays_ok(msg.content["arrays"], global_arrays):
                outcomes[BAD_ARRAYS] += 1
                continue
            outcomes[verdict.value] += 1
            if verdict is Verdict.ACCEPT:
                self.seen_update_ids.add(meta.update_id)
                accepted.append(msg)
        return accepted, outcomes

    # ---- main loop ------------------------------------------------------------

    def run(
        self,
        grid: Grid,
        initial_arrays: ArrayRecord,
        num_rounds: int,
        evaluate_config: ConfigRecord | None = None,
        start_round: int = 1,
        on_round_end: Callable[[RoundLog, ArrayRecord, MetricRecord | None], None] | None = None,
    ) -> Result:
        """Run num_rounds rounds starting at start_round.

        on_round_end(log, global_arrays, eval_metrics) is called after every
        round that produced a new global model (used to save checkpoints).
        """
        result = Result()
        arrays = initial_arrays
        result.arrays = arrays
        t0 = time.time()

        for rnd in range(start_round, start_round + num_rounds):
            self.current_round = rnd
            log = RoundLog(round=rnd)
            print(f"\n========== ROUND {rnd} (model v{self.model_version}) ==========")

            new_arrays, train_metrics = None, None
            for attempt in range(1, self.max_retries + 2):
                log.attempts = attempt
                config = ConfigRecord({**self.round_config, "model-version": self.model_version})
                messages = self.strategy.configure_train(rnd, arrays, config, grid)
                replies = grid.send_and_receive(messages=messages, timeout=self.round_timeout)
                accepted, outcomes = self.filter_replies(replies, arrays)
                log.outcomes.update(outcomes)
                for msg in accepted:
                    if (summary := data_summary(msg)) is not None:
                        log.data[summary[0]] = summary[1]
                print(f"Attempt {attempt}: {dict(outcomes)}")

                if len(accepted) >= self.quorum:
                    new_arrays, train_metrics = self.strategy.aggregate_train(rnd, accepted)
                    if new_arrays is not None:
                        break
                print(f"Quorum not reached ({len(accepted)}/{self.quorum}).")

            if new_arrays is None:
                self.history.append(log)
                msg = f"Round {rnd} failed after {log.attempts} attempts"
                if self.abort_on_failed_round:
                    raise RuntimeError(msg)
                print(f"{msg}; keeping global model v{self.model_version}.")
                continue

            arrays = new_arrays
            self.model_version += 1
            log.succeeded, log.model_version = True, self.model_version
            self.history.append(log)
            result.arrays = arrays
            if train_metrics is not None:
                result.train_metrics_clientapp[rnd] = train_metrics
            print(f"Round {rnd} succeeded -> global model v{self.model_version}")

            eval_messages = self.strategy.configure_evaluate(
                rnd, arrays, evaluate_config or ConfigRecord(dict(self.round_config)), grid
            )
            eval_replies = grid.send_and_receive(messages=eval_messages, timeout=self.round_timeout)
            eval_metrics = self.strategy.aggregate_evaluate(rnd, eval_replies)
            if eval_metrics is not None:
                result.evaluate_metrics_clientapp[rnd] = eval_metrics
                print(f"Round {rnd} eval: {dict(eval_metrics)}")
            if on_round_end is not None:
                on_round_end(log, arrays, eval_metrics)

        print(f"\nTraining completed in {time.time() - t0:.2f} seconds.")
        self.print_summary()
        return result

    def print_summary(self) -> None:
        print("\nround  ok   attempts  version  outcomes")
        for log in self.history:
            print(
                f"{log.round:>5}  {'yes' if log.succeeded else 'no ':<4} {log.attempts:>8}  "
                f"{log.model_version if log.succeeded else '-':>7}  {dict(log.outcomes)}"
            )


__all__ = ["FaultTolerantRunner", "RoundLog", "arrays_ok", "data_summary"]
