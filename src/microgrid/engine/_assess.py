"""Whether a valuation can rest on its inputs (tariffkit#67 R-14).

If any input cannot be used, the valuation is ``fallback_import``: no surplus and every
slot at the import price, with the causes named in ``degraded``. A stale discount is
never reused. When to issue stays the client's.

Pure: no I/O.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class ValuationKind(StrEnum):
    """How a valuation's costs were made (a valuation's ``kind``)."""

    STORED_VALUE_V1 = "stored_value_v1"
    FALLBACK_IMPORT = "fallback_import"
    #: No price for the slot under way: nothing valued, no slots.
    UNPRICED = "unpriced"


class Cause(StrEnum):
    """Why the engine could not use an input, named in an output's ``degraded``: each value
    names an input."""

    #: The valuation's inputs (its ``inputs``).
    PRICES = "prices"
    PV_FORECAST = "pv_forecast"
    BATTERY_SOC = "battery_soc"
    WORK_MODE = "work_mode"
    LOAD_FORECAST = "load_forecast"
    PLANT_MODEL = "plant_model"
    #: A live value's own, besides the valuation's.
    PV_POWER = "pv_power"
    LOAD_POWER = "load_power"
    #: Warming up after a start or a gap.
    SMOOTHING = "smoothing"
    #: The valuation is past its ``valid_until``.
    ISSUE = "issue"
    #: The battery's lots were not given (``Inputs.lots``) on the stored-cost rule: all
    #: it holds is of unknown origin, at the import price. Named; the values stand.
    BATTERY_LOTS = "battery_lots"
    #: The PV forecast gives no quantile below its median (a point forecast, say): the
    #: pessimistic case (``marginal_cost_p10``, ``surplus_kw_p10``; tariffkit#67 R-13) is the
    #: median, as nothing is known of the downside. Named; the values stand.
    PV_SPREAD = "pv_spread"


@dataclass(frozen=True, slots=True)
class Assessment:
    """How an issue would be valued, and which inputs were not usable."""

    valuation: ValuationKind
    #: The engine's causes (:class:`Cause`), or a client's own names beside them.
    degraded: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class InputState:
    """One input's freshness, as published in the issue's ``inputs``."""

    as_of: datetime | None
    usable: bool


def assess(inputs: Mapping[Cause, InputState]) -> Assessment:
    """``stored_value_v1`` when every input is usable, else ``fallback_import`` and the causes."""
    degraded = tuple(sorted(name for name, state in inputs.items() if not state.usable))
    return Assessment(
        valuation=ValuationKind.FALLBACK_IMPORT if degraded else ValuationKind.STORED_VALUE_V1,
        degraded=degraded,
    )
