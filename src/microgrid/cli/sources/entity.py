"""A forecast held in any entity's attribute (``from = "entity"``): for a source with no
named reader here, or one published under another shape.

The attribute is a list of rows (objects). Each row has a start time (``start_key``),
an end time (``end_key``) or a fixed ``period`` from the start, and its values under the
keys the configuration names: ``import`` and ``export`` for prices; ``value``, or
``p10``, ``p50`` and ``p90``, for PV; ``value`` for a load. Times are ISO 8601 with an
offset. The values' unit is the configuration's ``unit``, else the entity's
``unit_of_measurement``: a power unit (W, kW, MW) for PV and loads; for prices, a
currency per kWh or per MWh (none: per kWh). The forecast is issued at the state's
``last_updated``.

A row that cannot be read is an error, not skipped: a source with no named reader has no
known faults to forgive.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from microgrid.cli.sources import SourceError
from microgrid.cli.sources.config import EntityForecast
from microgrid.cli.sources.home_assistant import HaClient
from microgrid.engine.series import Forecast, Interval, Price, Quantiles
from microgrid.engine.units import Quantity, UnitError, convert

__all__ = [
    "EntityError",
    "Kind",
    "fetch_forecast",
    "parse_forecast",
]

type Kind = Literal["prices", "pv_forecast", "load_forecast"]


class EntityError(SourceError):
    """The entity's forecast cannot be read as configured."""


def _time(value: object, where: str) -> datetime:
    try:
        moment = datetime.fromisoformat(str(value))
    except ValueError as err:
        raise EntityError(f"{where}: not a time: {value!r}") from err
    if moment.tzinfo is None:
        raise EntityError(f"{where}: a time without an offset: {value!r}")
    return moment.astimezone(UTC)


def _number(row: dict[str, object], key: str, where: str) -> float:
    try:
        value = float(str(row[key]))
    except KeyError as err:
        raise EntityError(f"{where}: no {key!r}") from err
    except ValueError as err:
        raise EntityError(f"{where}: {key!r} is not a number") from err
    if not math.isfinite(value):
        raise EntityError(f"{where}: {key!r} is not finite")
    return value


def parse_forecast(
    state: object, config: EntityForecast, kind: Kind
) -> Forecast[Price] | Forecast[Quantiles] | Forecast[float]:
    """The forecast in ``state`` (``GET /api/states/<entity>``), as configured."""
    entity = config.entity
    if not isinstance(state, dict) or not isinstance(state.get("attributes"), dict):
        raise EntityError(f"{entity}: no attributes")
    attributes = state["attributes"]
    rows = attributes.get(config.attribute)
    if not isinstance(rows, list) or not rows:
        raise EntityError(f"{entity}: no {config.attribute} list (state {state.get('state')!r})")
    issued = _time(state.get("last_updated"), f"{entity} last_updated")
    unit = config.unit if config.unit is not None else attributes.get("unit_of_measurement")
    quantity: Quantity = "price" if kind == "prices" else "power"
    if unit is None and quantity == "power":
        raise EntityError(f"{entity}: no unit (set unit, or the entity's unit_of_measurement)")

    def scaled(value: float) -> float:
        if unit is None:  # a price with no unit: per kWh
            return value
        try:
            return convert(value, str(unit), quantity)
        except UnitError as err:
            raise EntityError(f"{entity}: {err}") from err

    intervals: list[Interval[object]] = []
    for index, row in enumerate(rows):
        where = f"{entity} {config.attribute}[{index}]"
        if not isinstance(row, dict):
            raise EntityError(f"{where}: not an object")
        start = _time(row.get(config.start_key), where)
        if config.end_key is not None:
            end = _time(row.get(config.end_key), where)
        else:
            end = start + (config.period or timedelta(0))
        keys = config.keys
        value: object
        if kind == "prices":
            value = Price(
                scaled(_number(row, keys["import"], where)),
                scaled(_number(row, keys["export"], where)),
            )
        elif "value" in keys:
            kw = scaled(_number(row, keys["value"], where))
            value = Quantiles.point(kw) if kind == "pv_forecast" else kw
        else:
            value = Quantiles(
                {
                    q: scaled(_number(row, keys[name], where))
                    for q, name in ((0.1, "p10"), (0.5, "p50"), (0.9, "p90"))
                }
            )
        try:
            intervals.append(Interval(start, end, value))
        except ValueError as err:
            raise EntityError(f"{where}: {err}") from err
    intervals.sort(key=lambda interval: interval.start)
    try:
        found: Forecast[Any] = Forecast(tuple(intervals), issued, f"entity:{entity}")
        return found
    except ValueError as err:
        raise EntityError(f"{entity}: {err}") from err


async def fetch_forecast(
    client: HaClient, config: EntityForecast, kind: Kind
) -> Forecast[Price] | Forecast[Quantiles] | Forecast[float]:
    """Read the entity and parse its forecast (:func:`parse_forecast`)."""
    return parse_forecast(await client.get_state(config.entity), config, kind)
