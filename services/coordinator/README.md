# Coordinator (Flower ServerApp)

| File | Contents |
|---|---|
| `src/fl_coordinator/server_app.py` | Entry point: reads run config, builds strategy and runner |
| `src/fl_coordinator/runner.py` | `FaultTolerantRunner`: quorum, retries, update filtering, model versioning |
| `src/fl_coordinator/strategies.py` | `make_strategy(name, **config)` (Amos) |

Each round the runner sends the global model plus its `model-version`, then drops:
failed replies, malformed metadata, and anything `fl_common.check_update` rejects
(duplicate, stale round, stale model, incompatible, empty), plus weights with the wrong
shape or NaN/inf. It aggregates only when at least `quorum` updates are left, retries up
to `max-retries` times, and otherwise keeps the previous model and moves on.

Not done yet: S3 checkpointing and resume after a coordinator crash, TLS.
