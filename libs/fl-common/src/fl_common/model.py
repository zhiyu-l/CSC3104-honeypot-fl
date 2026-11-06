"""The model every client trains and the coordinator aggregates.

All orgs must use the same architecture, so it lives here and nowhere else.
Change MODEL_ARCH_VERSION whenever the layers change.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any, Mapping

import numpy as np
import torch
from torch import nn

from fl_common.schema import FEATURE_NAMES, NUM_CLASSES, NUM_FEATURES, SCHEMA_VERSION, AttackClass

MODEL_ARCH_VERSION = 1


class HoneypotMLP(nn.Module):
    """Small MLP over per-session features. Outputs one logit per AttackClass."""

    def __init__(
        self,
        num_features: int = NUM_FEATURES,
        num_classes: int = NUM_CLASSES,
        hidden: int = 64,
    ) -> None:
        super().__init__()
        self.net = nn.Sequential(
            nn.Linear(num_features, hidden),
            nn.ReLU(),
            nn.Linear(hidden, hidden),
            nn.ReLU(),
            nn.Linear(hidden, num_classes),
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.net(x)


def get_weights(model: nn.Module) -> list[np.ndarray]:
    """Model parameters as numpy arrays, in state_dict order."""
    return [v.detach().cpu().numpy().copy() for v in model.state_dict().values()]


def set_weights(model: nn.Module, weights: list[np.ndarray]) -> None:
    """Load numpy arrays produced by get_weights (or by aggregation)."""
    keys = list(model.state_dict().keys())
    if len(keys) != len(weights):
        raise ValueError(f"expected {len(keys)} arrays, got {len(weights)}")
    state = {}
    for key, arr, current in zip(keys, weights, model.state_dict().values()):
        if tuple(arr.shape) != tuple(current.shape):
            raise ValueError(f"{key}: expected shape {tuple(current.shape)}, got {arr.shape}")
        state[key] = torch.as_tensor(arr, dtype=current.dtype)
    model.load_state_dict(state, strict=True)


# ---- saving and loading -----------------------------------------------------
#
# A saved model is two files with the same stem:
#     model.npz    the weights, one array per state_dict entry, in order
#     model.json   what the weights are: schema and architecture versions,
#                  feature and class names, SHA-256 of the .npz, plus any
#                  extra info the caller passes (round, strategy, metrics...)
# load_model refuses a model whose versions don't match this code, or whose
# .npz doesn't match the recorded hash (corrupted or swapped file).


class ModelFileError(ValueError):
    """Raised when a saved model is missing, corrupted or incompatible."""


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _paths(path: str | os.PathLike) -> tuple[Path, Path]:
    """'models/run/model', '.npz' or '.json' -> (npz path, json path)."""
    p = Path(path)
    if p.suffix in (".npz", ".json"):
        p = p.with_suffix("")
    return p.with_name(p.name + ".npz"), p.with_name(p.name + ".json")


def save_model(
    path: str | os.PathLike,
    weights: list[np.ndarray],
    info: Mapping[str, Any] | None = None,
) -> Path:
    """Write weights to <path>.npz and a description to <path>.json.

    Both files are written to a temp name first and then renamed, so a crash
    mid-save never leaves a half-written model behind. Returns the .npz path.
    """
    npz, meta = _paths(path)
    npz.parent.mkdir(parents=True, exist_ok=True)
    keys = list(HoneypotMLP().state_dict().keys())
    if len(keys) != len(weights):
        raise ValueError(f"expected {len(keys)} arrays, got {len(weights)}")

    tmp_npz = npz.with_name(npz.name + ".tmp")
    with tmp_npz.open("wb") as f:
        np.savez(f, **{k: np.asarray(w) for k, w in zip(keys, weights)})
    description = {
        "schema_version": SCHEMA_VERSION,
        "model_arch_version": MODEL_ARCH_VERSION,
        "feature_names": list(FEATURE_NAMES),
        "class_names": [c.name.lower() for c in AttackClass],
        "sha256": _sha256(tmp_npz),
        "saved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        **(info or {}),
    }
    tmp_json = meta.with_name(meta.name + ".tmp")
    tmp_json.write_text(json.dumps(description, indent=2, default=str))
    os.replace(tmp_npz, npz)
    os.replace(tmp_json, meta)
    return npz


def load_weights(path: str | os.PathLike) -> tuple[list[np.ndarray], dict[str, Any]]:
    """Read and check a saved model. Returns (weights, description)."""
    npz, meta = _paths(path)
    if not npz.is_file() or not meta.is_file():
        raise ModelFileError(f"need both {npz} and {meta}")
    info = json.loads(meta.read_text())
    if info.get("schema_version") != SCHEMA_VERSION:
        raise ModelFileError(
            f"{npz} uses feature schema v{info.get('schema_version')}, this code is v{SCHEMA_VERSION}"
        )
    if info.get("model_arch_version") != MODEL_ARCH_VERSION:
        raise ModelFileError(
            f"{npz} uses model architecture v{info.get('model_arch_version')}, "
            f"this code is v{MODEL_ARCH_VERSION}"
        )
    if info.get("sha256") != _sha256(npz):
        raise ModelFileError(f"{npz} does not match the SHA-256 in {meta.name} (corrupted or replaced)")
    keys = list(HoneypotMLP().state_dict().keys())
    with np.load(npz, allow_pickle=False) as data:
        if list(data.files) != keys:
            raise ModelFileError(f"{npz} has arrays {data.files}, expected {keys}")
        weights = [data[k] for k in keys]
    return weights, info


def load_model(path: str | os.PathLike) -> tuple[HoneypotMLP, dict[str, Any]]:
    """Saved model -> (HoneypotMLP in eval mode, description)."""
    weights, info = load_weights(path)
    model = HoneypotMLP()
    set_weights(model, weights)
    model.eval()
    return model, info
