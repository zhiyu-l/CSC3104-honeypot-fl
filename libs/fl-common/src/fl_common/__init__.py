"""Code shared by the coordinator, client, log processor and model API.

schema.py has no dependencies beyond the standard library, so the log
processor Lambda can `from fl_common.schema import ...` without packaging
torch or numpy. Model and metadata names are loaded lazily on first use.
"""

from fl_common.schema import (
    FEATURE_NAMES,
    NUM_CLASSES,
    NUM_FEATURES,
    SCHEMA_VERSION,
    AttackClass,
    SchemaError,
    label_session,
    transform,
    validate_row,
)

_LAZY = {
    "HoneypotMLP": "fl_common.model",
    "MODEL_ARCH_VERSION": "fl_common.model",
    "get_weights": "fl_common.model",
    "set_weights": "fl_common.model",
    "UpdateMetadata": "fl_common.metadata",
    "Verdict": "fl_common.metadata",
    "check_update": "fl_common.metadata",
}


def __getattr__(name: str):
    if name in _LAZY:
        import importlib

        return getattr(importlib.import_module(_LAZY[name]), name)
    raise AttributeError(f"module 'fl_common' has no attribute {name!r}")


__all__ = [
    "AttackClass",
    "FEATURE_NAMES",
    "NUM_CLASSES",
    "NUM_FEATURES",
    "SCHEMA_VERSION",
    "SchemaError",
    "label_session",
    "transform",
    "validate_row",
    *_LAZY,
]
