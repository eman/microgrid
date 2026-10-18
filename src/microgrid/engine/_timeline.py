"""The slot timeline: the grid of slots a valuation prices.

:data:`SLOT` is the default slot, 10 minutes (``EngineConfig.slot``, ``engine/2``'s
``slot_minutes``); every function takes the slot it works on. A source's intervals reach
the slots through ``series.align``. Everything here is UTC (see
:mod:`microgrid._checks`), so a DST day has the right number of slots.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from microgrid._checks import require_utc

SLOT = timedelta(minutes=10)


_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def slot_floor(moment: datetime, step: timedelta = SLOT) -> datetime:
    """The start (UTC) of the slot containing ``moment``."""
    require_utc(moment)
    return _EPOCH + ((moment - _EPOCH) // step) * step


def slot_starts(start: datetime, end: datetime, step: timedelta = SLOT) -> tuple[datetime, ...]:
    """Slot starts (UTC) from ``slot_floor(start)`` up to, not including, ``end``."""
    require_utc(end)
    first = slot_floor(start, step)
    count = max(0, -(-(end - first) // step))
    return tuple(first + i * step for i in range(count))


def horizon_slots(
    now: datetime, horizon: timedelta, step: timedelta = SLOT
) -> tuple[datetime, ...]:
    """The published slots: from the slot containing ``now``, exactly ``horizon`` long."""
    first = slot_floor(now, step)
    return slot_starts(first, first + horizon, step)
