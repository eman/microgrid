"""The checks every part of the engine makes of its inputs: neutral.

Shared by the price model, the plant models and the neutral types, so it imports
nothing from the engine. Every time in the core is **UTC**: Python compares and
subtracts two aware datetimes that share a ``tzinfo`` by wall clock, which is wrong
across a DST transition, so adapters convert at the edge and the core never sees
local time.

Pure: no I/O.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime


def require_utc(*moments: datetime) -> None:
    """Raise unless every moment is timezone-aware UTC."""
    for moment in moments:
        if moment.tzinfo is not UTC:
            raise ValueError(f"core times must be UTC: {moment!r}")


def require_finite(owner: str, **values: float) -> None:
    """Raise unless every value is a finite number."""
    for name, value in values.items():
        if not math.isfinite(value):
            raise ValueError(f"{owner} {name} must be finite, not {value}")
