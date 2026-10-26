# Running the stack locally

One coordinator (SuperLink) and three org clients (SuperNodes) in Docker, training
the shared `HoneypotMLP` on synthetic sessions from `fl-common`. Based on Andrew's
prototype in `prototypes/andrew-flower/`.

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
