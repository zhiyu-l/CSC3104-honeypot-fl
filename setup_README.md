# Setup Guide (Windows)

Runs one coordinator (SuperLink) and three org clients (SuperNodes) that train the shared
`HoneypotMLP` model with fault tolerance (quorum, retries, rejection of stale, duplicate
and bad updates). By default the clients train on synthetic sessions from
`libs/fl-common`; to train on the real Kaggle Cowrie split, see "Training on real data"
in step 5.

For how the system works internally, see `docs/how-it-works.md`.

## Prerequisites

- Python 3.11 or newer (3.12 recommended; Flower 1.40 does not support 3.10)
- Docker Desktop running with Linux containers (WSL 2 backend)
- Git, and the repo cloned locally

Use PowerShell for every command below, starting from the repo root
(the folder containing `README.md`).

## 1. Check Docker

```powershell
docker version
docker compose version
```

`docker version` must show both Client and Server. If the engine is unavailable, open
Docker Desktop and wait for it to start.

## 2. Create the Python environment

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -e "libs/fl-common[dev]" -e "services/client[dev]" -e "services/coordinator[dev]"
flwr --version
```

`flwr --version` should print 1.40.x.

If `Activate.ps1` is blocked with "running scripts is disabled on this system", allow
scripts for the current terminal only and activate again:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\.venv\Scripts\Activate.ps1
```

Do not install the root `requirements.txt`; it pins an older Flower version.

## 3. Run the tests (optional, recommended)

```powershell
pytest libs/fl-common
pytest services/client
pytest services/coordinator
```

All tests should pass.

## 4. Configure the Flower connection (once)

```powershell
flwr config list
```

Open the `config.toml` path printed by Flower. Add this section once and save:

```toml
[superlink.local-deployment]
address = "127.0.0.1:8000"
insecure = true
```

This connection is for local testing only (no TLS).

## 5. Build and start the containers

```powershell
docker compose -f deploy/local/compose.yaml up --build -d
docker compose -f deploy/local/compose.yaml ps
```

Check that `superlink`, `supernode-a`, `supernode-b` and `supernode-c` are running.
The first build takes a few minutes because it downloads PyTorch.

Rebuild with the same command (`up --build -d`) after changing any code in `libs/` or
`services/`, because the code is installed inside the images.

### Training on real data

Each org can train on its own share of the Kaggle Cowrie dataset instead of synthetic
sessions. First build the split (the data stays in the git-ignored `data/` folder):

1. Download the [Kaggle dataset](https://www.kaggle.com/datasets/nlaha11/global-ssh-and-telnet-honeypot-logs-cowrie)
   and unzip it so the 10 honeypot folders are in `data\raw\kaggle-cowrie\logs\`.
2. From the repo root:

   ```powershell
   python tools/cowrie_dataset/cowrie_sessions.py label
   python tools/cowrie_dataset/split_orgs.py
   ```

   This writes `data\processed\kaggle\split\train\orgA.csv`, `orgB.csv`, `orgC.csv`
   (1,148 sessions each; main attacks: A brute force, B malware download, C persistence)
   and the replay set for the attack side.

Then start the containers with the real-data override added:

```powershell
docker compose -f deploy/local/compose.yaml -f deploy/local/compose.real-data.yaml up --build -d
```

Each SuperNode gets only its own org's CSV, mounted read-only. Use the same two `-f`
flags for `ps`, `logs` and `down`.

## 6. Run training

```powershell
cd deploy/flower-app
flwr run . local-deployment --stream
```

By default this runs 5 rounds, needs at least 2 accepted updates per round, and gives each
client a 30% chance of crashing on purpose, so failures and retries are expected.
A successful run ends with `Training completed in ... seconds.` followed by a table:

```
round  ok   attempts  version  outcomes
    1  yes         1        1  {'accept': 3}
    2  yes         1        2  {'failed': 1, 'accept': 2}
```

Return to the repo root afterwards with `cd ../..`.

### Changing settings

Defaults are in `[tool.flwr.app.config]` in `deploy/flower-app/pyproject.toml`.
Override them for a single run with `--run-config`, for example no simulated crashes and
10 rounds:

```powershell
flwr run . local-deployment --stream --run-config "fail-prob=0.0 num-server-rounds=10"
```

| Setting | Default | Meaning |
|---|---|---|
| `num-server-rounds` | 5 | number of rounds |
| `quorum` | 2 | accepted updates needed to aggregate a round |
| `max-retries` | 2 | extra attempts per round before skipping it |
| `round-timeout` | 120 | seconds to wait for client replies |
| `abort-on-failed-round` | false | stop the run instead of skipping a failed round |
| `fail-prob` | 0.3 | chance each client crashes on purpose |
| `local-epochs`, `lr`, `batch-size` | 3, 0.01, 64 | local training settings |
| `save-model` | true | save successful round checkpoints and the final `model.npz` under `models/<run-name>/`, with JSON metadata |

## 7. Logs and shutdown

From the repo root:

```powershell
docker compose -f deploy/local/compose.yaml logs --tail=100
docker compose -f deploy/local/compose.yaml logs supernode-a --tail=50
docker compose -f deploy/local/compose.yaml down
```

To run training again while the containers are up, repeat step 6.

## Running without Docker (optional)

Useful while changing code, since nothing needs rebuilding. Activate `.venv` in each of
four PowerShell windows, then from the repo root:

```powershell
# Window 1: coordinator
flower-superlink --insecure --isolation subprocess

# Window 2: org a
$env:FL_ORG_ID="org-a"; flower-supernode --insecure --superlink 127.0.0.1:8000 --host 127.0.0.1 --port 9094 --isolation subprocess --node-config "org-index=0"

# Window 3: org b
$env:FL_ORG_ID="org-b"; flower-supernode --insecure --superlink 127.0.0.1:8000 --host 127.0.0.1 --port 9095 --isolation subprocess --node-config "org-index=1"

# Window 4: org c
$env:FL_ORG_ID="org-c"; flower-supernode --insecure --superlink 127.0.0.1:8000 --host 127.0.0.1 --port 9096 --isolation subprocess --node-config "org-index=2"
```

Then run step 6 from a fifth window. Stop the containers first
(`docker compose -f deploy/local/compose.yaml down`), since both use port 8000.

## Troubleshooting

| Problem | Fix |
|---|---|
| `flwr run` cannot connect | Check the containers are up (`ps` in step 5) and the `config.toml` block in step 4 |
| Run waits forever at `configure_train` | Fewer than `quorum` SuperNodes are connected; check `docker compose ... ps` and logs |
| Code change has no effect in Docker | Rebuild: `docker compose -f deploy/local/compose.yaml up --build -d` |
| Port 8000 already in use | Stop the other SuperLink (Docker or local process) |
