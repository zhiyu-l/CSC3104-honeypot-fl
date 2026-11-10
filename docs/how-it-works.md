# How the federated learning system works

This explains what happens when you run training: which programs run where, which files
are involved, and which functions are called at each step. For setup and run commands,
see `deploy/local/README.md`.

## Big picture

Each organisation trains the same model on its own honeypot data and sends only the
**model weights** (never the data) to a coordinator. The coordinator combines the weights
into a better **global model** and sends it back. One cycle of this is a **round**.

Flower splits the system into two programs:

| Program | Runs in | Our code |
|---|---|---|
| **ServerApp** (coordinator) | the **SuperLink** container | `services/coordinator/` |
| **ClientApp** (one per org) | each **SuperNode** container | `services/client/` |

Both import shared definitions from `libs/fl-common/`, because averaging weights only
makes sense if every org uses the same model, features and labels.

```
                    ┌──────────────────────────────┐
                    │ SuperLink                    │
                    │  fl_coordinator (ServerApp)  │
                    └──────┬────────┬────────┬─────┘
          global model  ↓  │  ↑  updates (weights + metrics + metadata)
                    ┌──────┴──┐ ┌───┴─────┐ ┌──┴──────┐
                    │ org-a   │ │ org-b   │ │ org-c   │   SuperNodes
                    │fl_client│ │fl_client│ │fl_client│   (ClientApp)
                    └─────────┘ └─────────┘ └─────────┘
                    local data never leaves each org
```

## Folders in use

| Folder | Role |
|---|---|
| `libs/fl-common/` | Shared feature schema, model, update metadata and synthetic data |
| `services/client/` | Flower ClientApp: local training and evaluation |
| `services/coordinator/` | Flower ServerApp: fault-tolerant round loop around a pluggable strategy |
| `deploy/flower-app/` | Run settings that `flwr run` sends to the SuperLink |
| `deploy/local/` | Docker Compose for 1 SuperLink + 3 SuperNodes |

## What each file does

### `libs/fl-common/src/fl_common/`

| File | Purpose |
|---|---|
| `schema.py` | The 16 per-session features (schema version 2). `validate_row()` rejects bad rows (missing columns, text, NaN, negatives) and caps huge values, since attackers control the logs. `transform()` applies `log1p`. `label_session()` assigns one of 4 attack classes (brute force, recon, persistence, malware download) by a fixed rule so every org labels identically; `parse_label()` reads the text labels in the log processor's CSVs. |
| `model.py` | `HoneypotMLP` (16 → 64 → 64 → 4). `get_weights()` / `set_weights()` convert between the model and the list of numpy arrays Flower sends. |
| `metadata.py` | `UpdateMetadata`: client ID, round, model version, number of examples and a unique `update_id`, attached to every update. `check_update()` decides whether the coordinator accepts it. |
| `evaluation.py` | Scores a saved model on labelled session CSVs (accuracy, per-class precision/recall/F1, confusion matrix, per org). Used by `tools/evaluate_model.py` and by the coordinator at the end of every run, so both report the same numbers. |
| `synthetic.py` | Fake sessions matching the schema, with a different attack mix per org (non-IID), used when no data path is set (tests, or machines without the dataset). |

### `services/client/src/fl_client/`

| File | Purpose |
|---|---|
| `task.py` | Plain PyTorch, no Flower. `load_org_data()`, `read_feature_csv()`, `train_local()` (with optional FedProx term), `evaluate_local()`. |
| `data.py` | Cumulative training data: the org's Kaggle base CSV plus every verified nightly batch from S3 (or a local folder), de-duplicated by session ID, minus held-out test sessions. Falls back to cached batches if S3 can't be reached. |
| `client_app.py` | Flower `train()` and `evaluate()` handlers. Works out which org it is, optionally simulates a crash, trains, and replies with weights, metrics, metadata and a summary of the data it trained on. |

### `services/coordinator/src/fl_coordinator/`

| File | Purpose |
|---|---|
| `server_app.py` | Entry point: reads settings, creates the starting model, strategy and runner. |
| `runner.py` | `FaultTolerantRunner`: quorum, retries, update filtering, model versioning, summary table. |
| `strategies.py` | `make_strategy(name)`: returns the aggregation strategy. |
| `checkpoints.py` | Saves the global model after every round and at the end, plus `history.json`; `resume-from` crash recovery. |
| `publish.py` | The `latest.json` pointer that carries the model from one run to the next, the promotion gate, optional copy to S3, and a rollback command. |
| `trigger.py` | The scheduled job: submits one run with `init-from='latest' promote=true`, refuses if a run is still in progress. |

Strategies **only average weights**. Deciding which updates count (quorum, stale,
duplicate, bad weights) lives in `runner.py`, so it works the same for FedAvg, FedMedian
and FedProx.

## One round, step by step

Functions marked **(Flower)** come from the `flwr` library, not this repo.

### Step 0: Setup (once, before round 1)

**File:** `services/coordinator/src/fl_coordinator/server_app.py`

When you run `flwr run`, Flower calls `main()`, which reads the settings from
`deploy/flower-app/pyproject.toml`, creates the starting global model, builds the strategy
and the runner, and starts the rounds.

```
main()                                  server_app.py
 ├─ HoneypotMLP(), get_weights()        fl-common/model.py
 ├─ make_strategy("fedavg")             strategies.py
 ├─ FaultTolerantRunner(...)            runner.py
 └─ runner.run(...)                     runner.py   (Steps 1 to 6 repeat inside this)
```

### Step 1: Send the global model to the clients

**Files:** `runner.py`, Flower's `FedAvg`

`run()` puts the current model version in a config, the strategy builds one message per
client (Flower adds `server-round`), and the grid sends them and waits up to
`round-timeout` seconds.

```
FaultTolerantRunner.run()               runner.py
 ├─ ConfigRecord({"model-version": ...})
 ├─ strategy.configure_train()          (Flower FedAvg)
 └─ grid.send_and_receive()             (Flower)   → messages go to each SuperNode
```

### Step 2: Each client trains locally

**Files:** `client_app.py`, `task.py`, fl-common `schema.py`, `synthetic.py`, `model.py`, `metadata.py`

Flower calls `train()` on each SuperNode. It finds its org from `FL_ORG_ID`,
`FL_ORG_INDEX` and `FL_DATA_PATH`, may crash on purpose (`fail-prob`), loads its data,
loads the global weights, trains, and replies with weights, metrics (`train_loss`,
`num-examples`) and metadata.

```
train()                                 client_app.py
 ├─ _org() → _setting()                 client_app.py
 ├─ (maybe) raise RuntimeError          simulated crash
 ├─ load_org_data()                     task.py
 │   ├─ make_org_dataset()              fl-common/synthetic.py
 │   │   └─ validate_row(), label_session()   fl-common/schema.py
 │   │  or read_feature_csv()           task.py (same schema checks)
 │   └─ transform()                     fl-common/schema.py
 ├─ _model_from() → set_weights()       fl-common/model.py
 ├─ train_local()                       task.py
 ├─ UpdateMetadata(...).to_config()     fl-common/metadata.py
 └─ get_weights() → reply Message       fl-common/model.py
```

### Step 3: The coordinator filters the replies

**Files:** `runner.py`, `fl-common/metadata.py`

`filter_replies()` sorts every reply into one outcome. Accepted update IDs are remembered
so a resent update is caught as a duplicate.

| Outcome | Meaning | Checked by |
|---|---|---|
| `failed` | client crashed or timed out | `msg.has_error()` (Flower) |
| `malformed` | metadata missing or unreadable | `UpdateMetadata.from_config()` |
| `duplicate` | this `update_id` was already accepted | `check_update()` |
| `stale_round` | sent for an earlier round (late reply) | `check_update()` |
| `stale_model` | trained from an older global model, client must resync | `check_update()` |
| `incompatible` | different schema or model architecture | `check_update()` |
| `empty` | trained on 0 examples | `check_update()` |
| `bad_arrays` | wrong shapes, NaN or inf in the weights | `arrays_ok()` |
| `accept` | safe to aggregate | all of the above passed |

```
filter_replies()                        runner.py
 ├─ msg.has_error()                     (Flower)
 ├─ UpdateMetadata.from_config()        fl-common/metadata.py
 ├─ check_update()                      fl-common/metadata.py
 └─ arrays_ok()                         runner.py
```

### Step 4: Aggregate if quorum is reached

**Files:** `runner.py`, Flower's `FedAvg`

If at least `quorum` updates were accepted, only those are passed to the strategy.
FedAvg averages them, weighting each org by `num-examples`. The model version goes up by 1.

```
run()                                   runner.py
 ├─ len(accepted) >= quorum ?
 ├─ strategy.aggregate_train()          (Flower FedAvg)
 └─ model_version += 1, RoundLog        runner.py
```

**Round number vs model version:** the round number goes up every round; the model
version only goes up when a round produces a new model. That is how an update trained
from an old model is caught as `stale_model`.

### Step 5: Retry, or skip the round

**File:** `runner.py`

Without quorum, the round is retried from Step 1, up to `max-retries` more times. If every
attempt fails:

- `abort-on-failed-round = false` (default): keep the previous global model and continue.
- `abort-on-failed-round = true`: stop the run with `RuntimeError`.

### Step 6: Evaluate the new model

**Files:** `runner.py`, `client_app.py`, `task.py`

Each client tests the new global model on its own 20% test split. The coordinator
averages the results.

```
run()                                   runner.py
 ├─ strategy.configure_evaluate()       (Flower)
 ├─ grid.send_and_receive()             (Flower)
 │    └─ evaluate()                     client_app.py
 │        ├─ load_org_data()            task.py
 │        ├─ _model_from()              fl-common/model.py
 │        └─ evaluate_local()           task.py
 └─ strategy.aggregate_evaluate()       (Flower)
```

### After the last round

- `print_summary()` in `runner.py` prints one row per round: succeeded or not, attempts,
  model version, and the count of each outcome.
- `main()` in `server_app.py` saves the final weights to `final_model.npz` if
  `save-model = true`.

## The daily cycle (many runs)

One `flwr run` is one run: N rounds, one final model. The daily loop is the same run
repeated, each one continuing from the last:

```
00:00  nightly log processor (per org)   Cowrie logs -> <org>/processed/sessions/<batch>/
~02:00 trigger (scheduled ECS task)       python -m fl_coordinator.trigger
        -> coordinator starts a run with init-from='latest' promote=true
        -> round 1 sends the latest global model to every client (this is how
           clients get the newest model)
        -> each client trains on its Kaggle base CSV + every verified batch so far
           (cumulative), skipping held-out test sessions
        -> rounds as usual (quorum, retries, checkpoints)
        -> final model scored on the fixed test set (eval-data) -> eval_report.json
        -> promoted to latest.json only if not worse than the current latest
        -> optionally copied to S3 (publish-s3-uri)
```

- **First run (bootstrap):** there is no `latest.json` yet, so it starts from a fresh
  model. Any run that starts fresh tells the clients `bootstrap=true`, and they train on
  their Kaggle base CSV only, ignoring nightly batches even if some already exist
  (`bootstrap-with-batches = true` to include them). That is the bootstrap model.
- **Starting from a model you already have:** put its run folder in model-dir, score it
  with `tools/evaluate_model.py` (so the gate has a baseline), then
  `python -m fl_coordinator.publish --set <run>`. The first scheduled run continues from
  it, with batches, and no bootstrap happens.
- **Model versions keep counting** across runs (v3 at the end of run 1, v4 after the
  first round of run 2...), so a higher version is always a newer model.
- **`init-from` vs `resume-from`:** `init-from` starts a NEW run folder from an existing
  model (daily cycle). `resume-from` continues the SAME run after a crash.
- **`history.json`** of each run records where it started from, which batches each org
  trained on (and how many held-out sessions were excluded) per round, the test score,
  and whether it was promoted.
- **Rollback:** every run folder is kept. `python -m fl_coordinator.publish --set <run>`
  points `latest.json` back at an older run; `--show` prints it.
- Normal runs (`flwr run` by hand, experiments) have `promote = false`, so they never
  move `latest.json`. Only the trigger turns promotion on.

## Settings

All in `[tool.flwr.app.config]` in `deploy/flower-app/pyproject.toml`. Override per run with
`flwr run . local-deployment --run-config "key=value key2=value2"`.

| Key | Used by | Meaning |
|---|---|---|
| `num-server-rounds` | coordinator | number of rounds |
| `strategy` | coordinator | `fedavg` (more to come) |
| `quorum` | coordinator | accepted updates needed to aggregate |
| `max-retries` | coordinator | extra attempts per round |
| `round-timeout` | coordinator | seconds to wait for replies |
| `abort-on-failed-round` | coordinator | stop instead of skipping a failed round |
| `save-model` | coordinator | save checkpoints and `model.npz` |
| `init-from` | coordinator | `latest`, a run folder or a `.npz`: start a new run from that model |
| `eval-data` | coordinator | fixed test set CSV(s), scored at the end of every run |
| `promote`, `promote-tolerance` | coordinator | move `latest.json` to this run if its macro F1 is not worse (minus tolerance) |
| `publish-s3-uri` | coordinator | also copy the promoted model and `latest.json` to this S3 prefix |
| `bootstrap-with-batches` | coordinator | `false`: runs that start from a fresh model train on the Kaggle base only |
| `seed` | both | random seed |
| `num-orgs` | client | number of orgs the synthetic data is split across |
| `local-epochs`, `lr`, `batch-size` | client | local training settings |
| `fail-prob` | client | chance a client crashes on purpose (0 for normal runs) |

Per-org settings come from environment variables on each SuperNode (`FL_ORG_ID`,
`FL_ORG_INDEX`, `FL_DATA_PATH`, and for the daily cycle `FL_BATCHES_URI`,
`FL_EXCLUDE_URI`, `FL_CACHE_DIR`; see `services/client/src/fl_client/data.py`), set in
`deploy/local/compose.yaml` locally and in the `fl-client-settings` ConfigMap on EKS.

## Not done yet

- Real Cowrie data (needs `services/log-processor/`); synthetic data is currently too easy
  (100% accuracy after one round)
- FedMedian and FedProx in `strategies.py`
- Scheduling the trigger on AWS (EventBridge Scheduler + ECS task) and the IAM for
  reading batches / writing models
- TLS between SuperLink and SuperNodes
- AWS deployment (`deploy/ecs/`, `infra/`)
