# fl-common

Shared code for the coordinator, client, log processor and model API. See
`docs/adr/0001-shared-model-schema-and-update-metadata.md` for why it looks like this.

| Module | What it holds |
|---|---|
| `schema.py` | Feature names, validation, clipping, `log1p` transform, attack-class labels |
| `model.py` | `HoneypotMLP` and `get_weights` / `set_weights` for Flower |
| `metadata.py` | `UpdateMetadata` sent with each update, and `check_update` for the coordinator |
| `synthetic.py` | Fake sessions matching the schema, split non-IID across orgs |

## Install (from a service folder or the repo root)

```bash
pip install -e libs/fl-common[dev]
pytest libs/fl-common
```

## Quick use

```python
from fl_common import HoneypotMLP, get_weights, transform, UpdateMetadata
from fl_common.synthetic import make_org_dataset

X, y = make_org_dataset(org_index=0, num_orgs=4)   # raw features, labels
X = [transform(row) for row in X.tolist()]          # same transform on every client
model = HoneypotMLP()

meta = UpdateMetadata(client_id="org-0", round_number=1, model_version=0, num_examples=len(y))
metrics = meta.to_config()   # put in the Flower reply alongside the weights
```
