# Flower Federated Learning Prototype

## Overview

This prototype demonstrates a Flower-based federated learning system with quorum enforcement, automatic training-round retries, and Docker containerisation.

**Note:** This is currently a proof of concept using dummy model parameters and simulated training. It does not yet train an intrusion-detection model using real Cowrie honeypot data.

## Implemented Features

- **Federated Averaging (FedAvg):** Aggregates successful client model updates using Flower's built-in strategy.
- **Quorum Enforcement:** Requires at least two successful client updates before aggregation.
- **Automatic Retries:** Allows up to two retries per training round if quorum is not reached.
- **Simulated Client Failures:** Uses a 30% random failure probability to test the coordinator's recovery behaviour.
- **Docker Deployment:** Uses Docker Compose to run one Flower SuperLink and three SuperNodes in separate containers.

## Project Structure

- `quickstart-numpy/` — Flower application, including `server_app.py`, `client_app.py` and `quorum_strategy.py`.
- `flower-docker/` — Dockerfiles and Compose configuration.

## Running the Prototype

**Prerequisites:** Docker Desktop, Docker Compose, Python and Flower 1.40.x.

1. From `flower-docker/`, build and start the services:

   ```powershell
   docker compose up --build -d
   ```

2. Activate the Python environment containing Flower and navigate to `quickstart-numpy/`.

3. Run the application:

   ```powershell
   flwr run . local-deployment --stream
   ```

   The `local-deployment` connection profile must point to the Docker-hosted SuperLink at `127.0.0.1:8000`.

4. To stop the containers:

   ```powershell
   docker compose down
   ```

## Current Limitations

- Uses randomly generated model updates rather than actual ML training.
- Does not yet process Cowrie honeypot datasets.
- Duplicate and stale model-update validation are not implemented.
- No persistent global-model checkpointing or coordinator crash recovery.
- Deployment currently uses insecure Flower connections and is intended only for local development.

## Future Work

- Integrate actual intrusion-detection model training.
- Reuse coordinator logic in the team's `services/coordinator` implementation.
- Deploy Flower clients into independent AWS environments using ECR and ECS Fargate.
- Introduce model version validation, checkpoints and stronger failure-recovery mechanisms.
- Evaluate performance under real client disconnections and failures.