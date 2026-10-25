# Client (Flower ClientApp)

Runs inside each organisation. Trains `fl_common.HoneypotMLP` on that org's sessions and
sends back weights, `num-examples` and `UpdateMetadata`. Only weights leave the org.

| File | Contents |
|---|---|
| `src/fl_client/client_app.py` | Flower `train` / `evaluate` handlers |
| `src/fl_client/task.py` | Data loading, local training (with optional FedProx term), evaluation |

Configuration (env vars take priority over `--node-config`):

| Env var | Node config | Meaning |
|---|---|---|
| `FL_ORG_ID` | `org-id` | Name reported in update metadata |
| `FL_ORG_INDEX` | `org-index` | Which synthetic shard to use (ignored when a data path is set) |
| `FL_DATA_PATH` | `data-path` | CSV from the log processor: `FEATURE_NAMES` columns plus optional `label` |
| `FL_SUPERLINK_ADDRESS` | | Coordinator address, passed to `flower-supernode --superlink` by the ECS task command |

Training settings (`local-epochs`, `lr`, `batch-size`, `fail-prob`) come from the run
config. If the server sends `proximal-mu` (FedProx), the proximal term is applied.
