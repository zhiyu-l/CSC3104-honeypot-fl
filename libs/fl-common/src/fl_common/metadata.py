"""Metadata attached to every client update.

The client sends this alongside its weights. The coordinator uses it for
fault tolerance: rejecting stale and duplicate updates, and spotting clients
that need to resynchronise.

round_number vs model_version:
    round_number   increments every time the coordinator starts a round.
    model_version  increments only when a round actually produces a new
                   global model (quorum reached). A round that fails quorum
                   bumps round_number but not model_version.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Mapping

from fl_common.model import MODEL_ARCH_VERSION
from fl_common.schema import SCHEMA_VERSION

_PREFIX = "meta."


@dataclass(frozen=True)
class UpdateMetadata:
    client_id: str
    round_number: int
    model_version: int  # global model version the client started training from
    num_examples: int
    update_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    schema_version: int = SCHEMA_VERSION
    arch_version: int = MODEL_ARCH_VERSION
    created_at: float = field(default_factory=time.time)

    def to_config(self) -> dict[str, int | float | str]:
        """Flat dict of scalars, safe to put in a Flower metrics/config record."""
        return {_PREFIX + k: v for k, v in asdict(self).items()}

    @classmethod
    def from_config(cls, config: Mapping[str, object]) -> "UpdateMetadata":
        """Parse a dict produced by to_config. Raises ValueError if malformed."""
        try:
            return cls(
                client_id=str(config[_PREFIX + "client_id"]),
                round_number=int(config[_PREFIX + "round_number"]),
                model_version=int(config[_PREFIX + "model_version"]),
                num_examples=int(config[_PREFIX + "num_examples"]),
                update_id=str(config[_PREFIX + "update_id"]),
                schema_version=int(config[_PREFIX + "schema_version"]),
                arch_version=int(config[_PREFIX + "arch_version"]),
                created_at=float(config[_PREFIX + "created_at"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"malformed update metadata: {exc}") from exc


class Verdict(str, Enum):
    ACCEPT = "accept"
    DUPLICATE = "duplicate"           # same update_id already accepted
    STALE_ROUND = "stale_round"       # sent for an earlier round
    STALE_MODEL = "stale_model"       # trained from an old global model: resync
    INCOMPATIBLE = "incompatible"     # different schema or architecture
    EMPTY = "empty"                   # trained on zero examples


def check_update(
    meta: UpdateMetadata,
    *,
    current_round: int,
    current_model_version: int,
    seen_update_ids: set[str],
) -> Verdict:
    """Decide whether the coordinator should aggregate this update.

    Pure function so it can be unit tested without Flower. The caller adds
    meta.update_id to seen_update_ids only when the verdict is ACCEPT.
    """
    if meta.schema_version != SCHEMA_VERSION or meta.arch_version != MODEL_ARCH_VERSION:
        return Verdict.INCOMPATIBLE
    if meta.update_id in seen_update_ids:
        return Verdict.DUPLICATE
    if meta.round_number != current_round:
        return Verdict.STALE_ROUND
    if meta.model_version != current_model_version:
        return Verdict.STALE_MODEL
    if meta.num_examples <= 0:
        return Verdict.EMPTY
    return Verdict.ACCEPT
