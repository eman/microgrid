"""Prices from the OpenEI integration (``from = "openei"``).

The integration publishes the utility rate plan's rate in force (its current-rate
entity: tier one's rate, or the tier the meter's reading reaches), the plan's adjustment
in force (added to it, where the plan has one), its sell rate, and when the rate
structure next changes, as a local time without an offset. It publishes no rate past
that change, so the forecast holds the rate in force from the top of the hour under way
until the change, and ends there: a valuation's horizon ends with it, a shorter
valuation rather than a fallback. With no change to come (a plan with one rate) the rate
holds over the whole horizon.

A change time already past means the integration's rate is out of date: an error. The
export price is the sell rate, else the configuration's fixed ``export``. The rate is
the integration's answer for now, so the forecast is issued when it is read.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from microgrid.cli.sources import SourceError
from microgrid.cli.sources.api import HomeAssistantApi
from microgrid.cli.sources.config import OpenEI
from microgrid.cli.sources.rows import RowError, number, time
from microgrid.engine.series import Forecast, Interval, Price

__all__ = [
    "OpenEIError",
    "fetch_prices",
    "parse_prices",
]

#: The states an entity holds when it has no value.
_NO_VALUE = frozenset({"", "none", "unknown", "unavailable"})


class OpenEIError(SourceError):
    """An OpenEI entity is unavailable, or its rate is out of date."""


def _value(states: Mapping[str, object], entity: str) -> str | None:
    state = states.get(entity)
    if not isinstance(state, dict):
        raise OpenEIError(f"{entity}: no state")
    value = str(state.get("state", "")).strip()
    return None if value.lower() in _NO_VALUE else value


def _rate(states: Mapping[str, object], entity: str) -> float:
    value = _value(states, entity)
    if value is None:
        raise OpenEIError(f"{entity}: no rate (unavailable)")
    try:
        return number(value, entity)
    except RowError as err:
        raise OpenEIError(str(err)) from err


def parse_prices(
    states: Mapping[str, object],
    config: OpenEI,
    *,
    now: datetime,
    horizon: timedelta,
    tz: ZoneInfo,
) -> Forecast[Price]:
    """The rate in force until the structure next changes (``tz``: the local time the
    integration states that change in)."""
    p_import = _rate(states, config.current_rate)
    if config.adjustment is not None and _value(states, config.adjustment) is not None:
        p_import += _rate(states, config.adjustment)
    p_export = _rate(states, config.sell_rate) if config.sell_rate is not None else config.export
    if p_export is None:  # pragma: no cover - the configuration requires one
        raise OpenEIError("no export price: name the sell rate or a fixed export")
    start = now.astimezone(tz).replace(minute=0, second=0, microsecond=0)
    change = _value(states, config.next_structure_time)
    if change is None:
        end = now + horizon  # no change to come: one rate
    else:
        try:
            end = time(change, config.next_structure_time, tz=tz)
        except RowError as err:
            raise OpenEIError(str(err)) from err
        if end <= now:
            raise OpenEIError(
                f"{config.next_structure_time}: the next change ({change}) is past: the rate"
                " is out of date"
            )
    return Forecast(
        (Interval(start.astimezone(UTC), end, Price(p_import, p_export)),),
        now,
        f"openei:{config.current_rate}",
    )


async def fetch_prices(
    client: HomeAssistantApi,
    config: OpenEI,
    *,
    now: datetime,
    horizon: timedelta,
    tz: ZoneInfo,
) -> Forecast[Price]:
    """Read the configured entities and parse them (:func:`parse_prices`)."""
    states: dict[str, object] = {}
    for entity in (
        config.current_rate,
        config.next_structure_time,
        config.adjustment,
        config.sell_rate,
    ):
        if entity is not None:
            states[entity] = await client.get_state(entity)
    return parse_prices(states, config, now=now, horizon=horizon, tz=tz)
