# 0001: Shared model, feature schema and update metadata

**Status:** Proposed (needs team agreement)
**Date:** 2026-10-22

## Context

The coordinator, the client, the log processor and the model API all need to agree on
the same model, the same feature columns and the same update format. If any of them
drift, averaging weights across organisations stops making sense. The log processor is
not ready yet, but the client and coordinator need data to develop against now.

## Decision

All shared definitions live in `libs/fl-common/` and every service imports them from there.

1. **Feature schema** (`schema.py`): one row per Cowrie SSH session, 10 numeric features
   derived from Cowrie events. Rows are validated (no missing or unknown columns, no
   NaN, inf or negatives) and clipped, because logs are attacker controlled. Features
   are passed through `log1p` before training. `SCHEMA_VERSION` must be bumped on any change.
2. **Labels**: four attack classes (scan, brute force, recon, malware) assigned by a fixed
   rule in `label_session`, so every organisation labels sessions identically.
3. **Model** (`model.py`): a small MLP (10 → 64 → 64 → 4). Weights are exchanged as a
   list of numpy arrays in `state_dict` order.
4. **Update metadata** (`metadata.py`): every client update carries `client_id`,
   `round_number`, `model_version`, `num_examples`, a unique `update_id`, and schema and
   architecture versions. `round_number` increments every round; `model_version` only
   increments when a round reaches quorum and produces a new global model.
5. **Update checks**: `check_update` decides whether the coordinator aggregates an update
   (accept, duplicate, stale round, stale model, incompatible, empty). It is a pure
   function so it can be unit tested without Flower.
6. **Strategy boundary**: aggregation strategies (FedAvg, FedMedian, FedProx) only
   aggregate. Quorum, stale and duplicate rejection and resynchronisation stay in the
   coordinator so they work with any strategy. The strategy is chosen by config name.
7. **Synthetic data** (`synthetic.py`): generated sessions that match the schema, split
   non-IID across organisations with a Dirichlet distribution, used until the log
   processor produces real features.

## Alternatives considered

- **Each service defines its own model/schema**: rejected, guaranteed to drift.
- **Training on raw Cowrie events or command text**: rejected for now. More expressive,
  but much harder to validate safely and needs a far bigger model.
- **Binary label (malicious or not)**: rejected. All honeypot traffic is malicious, so the
  useful question is what kind of attack it is.
- **Using a public intrusion dataset as stand-in**: possible, but its columns do not match
  what Cowrie produces, so code would need rewriting when real data arrives.

## Consequences

- The log processor must output exactly `FEATURE_NAMES` and use `validate_row` and
  `label_session`.
- Changing the schema or model breaks compatibility with old checkpoints; the version
  fields let the coordinator reject mismatched updates instead of silently averaging them.
