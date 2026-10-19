"""A load forecast EMHASS publishes (``from = "emhass"``).

EMHASS publishes its load forecast as an entity (``sensor.p_load_forecast`` by default)
whose attribute (``forecasts``) lists rows such as ``{"date": "2026-10-12T22:00:00-07:00",
"p_load_forecast": "1511.82"}``: the mean power over its optimization step from
``date``, in the entity's ``unit_of_measurement`` (a power unit). The forecast is issued
at the state's ``last_updated``. A row that cannot be read is skipped and logged; two
rows for the same time that disagree, overlapping rows, or none usable are an error.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final

from microgrid.cli.sources import SourceError
from microgrid.cli.sources.api import HomeAssistantApi
from microgrid.engine.series import Forecast, Interval
from microgrid.engine.units import POWER_KW

__all__ = [
    "PERIOD",
    "ForecastEntity",
    "LoadForecastError",
    "fetch_load_forecast",
    "parse_load_forecast",
    "source_label",
]

log = logging.getLogger(__name__)


#: Each row is the mean over EMHASS's optimization step from its ``date``: 30 minutes
#: unless EMHASS is set otherwise.
PERIOD: Final = timedelta(minutes=30)


class LoadForecastError(SourceError):
    """No usable load forecast in the entity's state."""


@dataclass(frozen=True, slots=True)
class ForecastEntity:
    """Where the forecast is published: the HA entity, its attribute holding the rows,
    and each row's value field."""

    entity: str
    attribute: str
    value_key: str


def source_label(entity: str) -> str:
    """The label issues carry for a load forecast read from ``entity`` (EMHASS's)."""
    return f"emhass:{entity}"


def _time(value: object) -> datetime:
    moment = datetime.fromisoformat(str(value))
    if moment.tzinfo is None:
        raise ValueError(f"no offset: {value!r}")
    return moment.astimezone(UTC)


def parse_load_forecast(
    state: object, config: ForecastEntity, *, period: timedelta = PERIOD
) -> Forecast[float]:
    """The forecast in ``state`` (``GET /api/states/<entity>``), in kW, UTC, sorted:
    the library's forecast type, issued at the state's ``last_updated``."""
    entity = config.entity
    if not isinstance(state, dict) or not isinstance(state.get("attributes"), dict):
        raise LoadForecastError(f"{entity}: no attributes")
    attributes = state["attributes"]
    try:
        as_of = _time(state.get("last_updated"))
    except ValueError as err:
        raise LoadForecastError(f"{entity}: last_updated is not a timestamp: {err}") from err
    unit = attributes.get("unit_of_measurement")
    factor = POWER_KW.get(str(unit))
    if factor is None:
        raise LoadForecastError(f"{entity}: not a power unit: {unit!r}")
    rows = attributes.get(config.attribute)
    if not isinstance(rows, list):
        status = state.get("state")
        raise LoadForecastError(f"{entity}: no {config.attribute} list (state {status!r})")
    by_start: dict[datetime, float] = {}
    skipped = 0
    for row in rows:
        try:
            if not isinstance(row, dict):
                raise TypeError("not an object")
            start = _time(row["date"])
            kw = float(row[config.value_key]) * factor
            if not math.isfinite(kw) or kw < 0:
                raise ValueError(f"not a load: {kw}")
        except KeyError, TypeError, ValueError:
            skipped += 1
            continue
        if start in by_start and by_start[start] != kw:
            raise LoadForecastError(f"{entity}: two values for {start.isoformat()}")
        by_start[start] = kw
    if skipped:
        log.warning("%s: skipped %d of %d load forecast rows", entity, skipped, len(rows))
    if not by_start:
        raise LoadForecastError(f"{entity}: no usable {config.attribute} row")
    try:
        return Forecast(
            tuple(Interval(start, start + period, kw) for start, kw in sorted(by_start.items())),
            as_of,
            source_label(entity),
        )
    except ValueError as err:  # rows closer than the period overlap
        raise LoadForecastError(f"{entity}: {err}") from err


async def fetch_load_forecast(
    client: HomeAssistantApi, config: ForecastEntity, *, period: timedelta = PERIOD
) -> Forecast[float]:
    """Read the forecast entity's state and parse it (:func:`parse_load_forecast`)."""
    return parse_load_forecast(await client.get_state(config.entity), config, period=period)
