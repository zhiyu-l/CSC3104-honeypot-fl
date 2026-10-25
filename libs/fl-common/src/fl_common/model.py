"""The model every client trains and the coordinator aggregates.

All orgs must use the same architecture, so it lives here and nowhere else.
Change MODEL_ARCH_VERSION whenever the layers change.
"""

from __future__ import annotations

import numpy as np
import torch
from torch import nn

from fl_common.schema import NUM_CLASSES, NUM_FEATURES

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
