"""What a battery's meters show of its rating: the most it was seen to charge and
discharge, and its capacity from the energy it took against its SOC's change.

A proposal for a plant's file, where the battery's own figures are not to hand, not a
fit: the most seen is a floor on the battery's limit (a week may never reach it), and
the capacity is the energy charged and discharged over the SOC's travel, which leaves
the conversion's losses out to first order (charging overstates it and discharging
understates it by about as much). Pure: the caller reads the meters.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final

from microgrid.engine.series import Series, means, value_at

__all__ = ["BatterySeen", "battery_seen", "rounded"]

#: The span the most seen is taken over: a slot's mean, so a reading's spike is not
#: taken for the battery's rate.
PEAK_SLOT: Final = timedelta(minutes=5)
#: The SOC must travel this far (a fraction, summed) for the capacity to be told.
_TRAVEL_MIN: Final = 0.3
#: An hour that moved less than this (kWh) is idle: what the SOC does then (a drift,
#: a standby draw the meter does not see) is not the meter's energy.
_IDLE_KWH: Final = 0.05
#: A SOC this close to full or empty is at its bound: held there, it cannot step to
#: follow what the meter shows (a battery kept full by PV, still balancing at 100 %).
_AT_BOUND: Final = 0.005
_HOUR: Final = timedelta(hours=1)


@dataclass(frozen=True, slots=True)
class BatterySeen:
    """The battery's figures its meters show; None where they do not."""

    #: The most it charged and discharged, kW, as a :data:`PEAK_SLOT` mean.
    charge_kw: float | None
    discharge_kw: float | None
    #: Its capacity, kWh.
    capacity_kwh: float | None


def _slots(start: datetime, end: datetime, step: timedelta) -> list[datetime]:
    return [start + i * step for i in range(int((end - start) / step))]


def battery_seen(
    power: Series[float], soc: Series[float] | None, start: datetime, end: datetime
) -> BatterySeen:
    """The figures ``power`` (kW, positive charging) and ``soc`` (a fraction) show
    between ``start`` and ``end`` (whole hours)."""
    peaks = [v for v in means(power, _slots(start, end, PEAK_SLOT), PEAK_SLOT) if v is not None]
    charge = max((v for v in peaks if v > 0), default=None)
    discharge = max((-v for v in peaks if v < 0), default=None)
    return BatterySeen(
        charge, discharge, None if soc is None else _capacity(power, soc, start, end)
    )


def _capacity(
    power: Series[float], soc: Series[float], start: datetime, end: datetime
) -> float | None:
    """The energy moved over the SOC's travel. An hour the SOC did not step keeps its
    energy: over a run of hours the SOC's travel adds up to the true change within one
    step, so leaving such hours out would leave energy out and no travel. An hour where
    the meter and the SOC go opposite ways (a step from the hour before, a meter out of
    step) is left out, as is an hour the SOC spends at full or empty, where it cannot
    move however the meter reads."""
    starts = _slots(start, end, _HOUR)
    kw = means(power, starts, _HOUR)
    moved: list[float] = []
    travelled: list[float] = []
    for hour, mean in zip(starts, kw, strict=True):
        before, after = value_at(soc, hour), value_at(soc, hour + _HOUR)
        if mean is None or before is None or after is None:
            continue
        kwh, change = mean * 1.0, after - before
        if abs(kwh) < _IDLE_KWH or kwh * change < 0 or _held(before, after):
            continue
        moved.append(abs(kwh))
        travelled.append(abs(change))
    total = math.fsum(travelled)
    if total < _TRAVEL_MIN:
        return None
    return math.fsum(moved) / total


def _held(before: float, after: float) -> bool:
    """Whether the SOC was at the same bound at both ends of an hour."""
    return (before >= 1 - _AT_BOUND and after >= 1 - _AT_BOUND) or (
        before <= _AT_BOUND and after <= _AT_BOUND
    )


def _round_up(value: float | None, step: float = 0.1) -> float | None:
    return None if value is None else round(math.ceil(value / step - 1e-9) * step, 1)


def rounded(seen: BatterySeen) -> BatterySeen:
    """``seen`` to a tenth: the limits up, the capacity to the nearest."""
    return BatterySeen(
        charge_kw=_round_up(seen.charge_kw),
        discharge_kw=_round_up(seen.discharge_kw),
        capacity_kwh=None if seen.capacity_kwh is None else round(seen.capacity_kwh, 1),
    )
