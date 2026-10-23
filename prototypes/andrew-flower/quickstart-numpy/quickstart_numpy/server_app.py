"""quickstart-numpy: A Flower / NumPy app."""

import numpy as np
from flwr.app import ArrayRecord, Context
from flwr.serverapp import Grid, ServerApp
from flwr.serverapp.strategy import FedAvg
from quickstart_numpy.quorum_strategy import QuorumFedAvg
from quickstart_numpy.task import get_dummy_model

# Create ServerApp
app = ServerApp()


@app.main()
def main(grid: Grid, context: Context) -> None:
    """Main entry point for the ServerApp."""

    # Read run config
    num_rounds: int = context.run_config["num-server-rounds"]

    # Load global model
    model = get_dummy_model()
    arrays = ArrayRecord(model)

    # Initialize FedAvg strategy
    strategy = QuorumFedAvg(
        fraction_train=1.0,
        min_train_nodes=2,
        min_available_nodes=2,
        required_updates=2,
        max_retries=2,
    )

    # Start strategy, run FedAvg for `num_rounds`
    result = strategy.start(
        grid=grid,
        initial_arrays=arrays,
        num_rounds=num_rounds,
    )

    if context.run_config["save-model"]:
        # Save final model to disk
        print("\nSaving final model to disk...")
        ndarrays = result.arrays.to_numpy_ndarrays()
        np.savez("final_model", *ndarrays)
