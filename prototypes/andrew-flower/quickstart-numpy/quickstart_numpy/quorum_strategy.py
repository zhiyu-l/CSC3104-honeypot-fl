import time
from collections.abc import Callable

from flwr.app import ArrayRecord, ConfigRecord, MetricRecord
from flwr.serverapp import Grid
from flwr.serverapp.strategy import FedAvg
from flwr.serverapp.strategy.result import Result


class QuorumFedAvg(FedAvg):
    """FedAvg with quorum enforcement and training-round retries."""

    def __init__(
        self,
        required_updates: int = 2,
        max_retries: int = 2,
        **kwargs,
    ):
        super().__init__(**kwargs)

        self.required_updates = required_updates
        self.max_retries = max_retries

    def aggregate_train(self, server_round, replies):
        """Aggregate only when enough clients respond successfully."""

        replies = list(replies)

        valid_replies, error_replies = (
            self._check_and_log_replies(replies, is_train=True)
        )

        print(f"\nRound {server_round}")
        print(f"Valid updates: {len(valid_replies)}")
        print(f"Failed updates: {len(error_replies)}")

        if len(valid_replies) < self.required_updates:
            print("Quorum not reached.")
            return None, None

        print("Quorum reached.")

        return super().aggregate_train(server_round, replies)

    def start(
        self,
        grid: Grid,
        initial_arrays: ArrayRecord,
        num_rounds: int = 3,
        timeout: float = 3600,
        train_config: ConfigRecord | None = None,
        evaluate_config: ConfigRecord | None = None,
        evaluate_fn: (
            Callable[[int, ArrayRecord], MetricRecord | None] | None
        ) = None,
    ) -> Result:
        """Run federated learning with a retry policy per round."""

        train_config = (
            ConfigRecord() if train_config is None else train_config
        )

        evaluate_config = (
            ConfigRecord() if evaluate_config is None else evaluate_config
        )

        result = Result()
        arrays = initial_arrays
        result.arrays = initial_arrays

        start_time = time.time()

        # Optional evaluation before the first training round
        if evaluate_fn is not None:
            metrics = evaluate_fn(0, arrays)

            if metrics is not None:
                result.evaluate_metrics_serverapp[0] = metrics

        # OUTER LOOP: federated learning rounds
        for current_round in range(1, num_rounds + 1):

            print(f"\n========== ROUND {current_round}/{num_rounds} ==========")

            retries_remaining = self.max_retries

            # INNER LOOP: attempts within the same round
            while retries_remaining >= 0:

                attempt = self.max_retries - retries_remaining + 1

                print(f"\nTraining attempt {attempt}")

                # Send current global model to selected clients
                train_messages = self.configure_train(
                    current_round,
                    arrays,
                    train_config,
                    grid,
                )

                # Receive fresh training replies
                train_replies = grid.send_and_receive(
                    messages=train_messages,
                    timeout=timeout,
                )

                # Check quorum and perform FedAvg if successful
                agg_arrays, agg_train_metrics = self.aggregate_train(
                    current_round,
                    train_replies,
                )

                if agg_arrays is not None:
                    print(f"Round {current_round} succeeded!")
                    break

                retries_remaining -= 1

                if retries_remaining >= 0:
                    print(
                        f"Retrying round {current_round}. "
                        f"Retries remaining: {retries_remaining}"
                    )

            # Stop the entire run when every attempt fails
            if agg_arrays is None:
                raise RuntimeError(
                    f"Round {current_round} failed after "
                    f"{self.max_retries + 1} attempts."
                )

            # Commit the successful aggregated global model
            arrays = agg_arrays
            result.arrays = agg_arrays

            if agg_train_metrics is not None:
                result.train_metrics_clientapp[current_round] = (
                    agg_train_metrics
                )

            # Evaluate only after successful training
            evaluate_replies = grid.send_and_receive(
                messages=self.configure_evaluate(
                    current_round,
                    arrays,
                    evaluate_config,
                    grid,
                ),
                timeout=timeout,
            )

            agg_evaluate_metrics = self.aggregate_evaluate(
                current_round,
                evaluate_replies,
            )

            if agg_evaluate_metrics is not None:
                result.evaluate_metrics_clientapp[current_round] = (
                    agg_evaluate_metrics
                )

            # Optional centralized evaluation
            if evaluate_fn is not None:
                metrics = evaluate_fn(current_round, arrays)

                if metrics is not None:
                    result.evaluate_metrics_serverapp[current_round] = (
                        metrics
                    )

        print(
            f"\nTraining completed in "
            f"{time.time() - start_time:.2f} seconds."
        )

        return result
