# Running the stack locally

One coordinator (SuperLink) and three org clients (SuperNodes) in Docker, training
the shared `HoneypotMLP` on synthetic sessions from `fl-common` (or on the real Kaggle
split with `compose.real-data.yaml`, see `setup_README.md`). Based on Andrew's Flower
prototype (`andrew` branch).

## 1. Python environment (for `flwr` CLI and tests)

From the repo root, with Python 3.11+ (Flower 1.40 does not support 3.10):

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e "libs/fl-common[dev]" -e "services/client[dev]" -e "services/coordinator[dev]"
```

Run the tests:

```powershell
pytest libs/fl-common; pytest services/client; pytest services/coordinator
```

## 2. Point `flwr` at the local SuperLink (once)

`flwr config list` prints the path of `config.toml`. Add:

```toml
[superlink.local-deployment]
address = "127.0.0.1:8000"
insecure = true
```

## 3. Start the containers

```powershell
docker compose -f deploy/local/compose.yaml up --build -d
docker compose -f deploy/local/compose.yaml ps
```

## 4. Run training

```powershell
cd deploy/flower-app
flwr run . local-deployment --stream
```

Settings live in `deploy/flower-app/pyproject.toml` under `[tool.flwr.app.config]`.
Override them per run, e.g. no simulated crashes and 10 rounds:

```powershell
flwr run . local-deployment --stream --run-config "fail-prob=0.0 num-server-rounds=10"
```

The run ends with a per-round table: attempts, model version and what happened to
each update (`accept`, `failed`, `stale_round`, `stale_model`, `duplicate`, `bad_arrays`, ...).

The global model is saved to `models/<run-name>/` in the repo (mounted into the
coordinator container at `/app/models`): `round_NNN.npz` after every successful
round and `model.npz` at the end. String settings need single quotes:

```powershell
flwr run . local-deployment --stream --run-config "run-name='fedprox-10r' strategy='fedprox' num-server-rounds=10"
# continue a run that stopped part-way (path as seen inside the container):
flwr run . local-deployment --stream --run-config "resume-from='/app/models/fedprox-10r' num-server-rounds=10"
```

### Daily cycle by hand

The scheduled job on AWS is `python -m fl_coordinator.trigger`. Locally, with the stack
running, the same command submits one cycle: start from the model in
`models/latest.json` (a fresh model the first time), train, score on the fixed test set,
and promote it to `latest.json` if it is not worse. It refuses if a run is still going.

```powershell
python -m fl_coordinator.trigger --insecure --app deploy/flower-app `
    --run-config "eval-data='/app/models/eval/sessions.csv' num-server-rounds=10"
python -m fl_coordinator.publish --model-dir models --show       # which model is latest
python -m fl_coordinator.publish --model-dir models --set <run>  # roll back
```

`eval-data` is a path as seen inside the coordinator container (copy the replay
`sessions.csv` into `models/eval/`). For clients to pick up nightly batches, set
`FL_BATCHES_URI` (an `s3://` prefix, or a folder mounted into the container) and
optionally `FL_EXCLUDE_URI` on each SuperNode; see `services/client/src/fl_client/data.py`.

Test a saved model with `python tools/evaluate_model.py`, or run every stage
with `python tools/run_pipeline.py` (see the ML pipeline section of the main README).

## Without Docker

Same thing as local processes (no image rebuild needed after code changes):

```powershell
flower-superlink --insecure --isolation subprocess
# in three more terminals, i = 0, 1, 2:
$env:FL_ORG_ID="org-0"; flower-supernode --insecure --superlink 127.0.0.1:8000 --host 127.0.0.1 --port 9094 --isolation subprocess --node-config "org-index=0"
```

Use a different `--port` (9094, 9095, 9096) and org index for each SuperNode.

## Stop

```powershell
docker compose -f deploy/local/compose.yaml down
```
