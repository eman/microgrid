"""A forecast held in any entity's attribute (``from = "entity"``): for a source with no
named reader here, or one published under another shape.

The attribute is a list of rows (objects). Each row has a start time (``start_key``),
an end time (``end_key``) or a fixed ``period`` from the start, and its values under the
keys the configuration names: ``import``, and ``export`` or a fixed ``export`` price,
for prices; ``value``, or ``p10``, ``p50`` and ``p90``, for PV; ``value`` for a load.
Times are ISO 8601 with an offset (or ``datetime`` values, inside Home Assistant). The
values' unit is the configuration's ``unit``, else the entity's ``unit_of_measurement``:
a power unit (W, kW, MW) for PV and loads; for prices, a currency per kWh or per MWh
(none: per kWh). The forecast is issued at the state's ``last_updated``.

A row that cannot be read is an error, not skipped: a source with no named reader has no
known faults to forgive.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any, Literal

from microgrid.cli.sources import SourceError
from microgrid.cli.sources.api import HomeAssistantApi
from microgrid.cli.sources.config import EntityForecast
from microgrid.cli.sources.rows import RowError, number, time
from microgrid.engine.series import Forecast, Interval, Price, Quantiles
from microgrid.engine.units import Quantity, UnitError, convert

__all__ = [
    "EntityError",
    "Kind",
    "fetch_forecast",
    "parse_forecast",
    "parse_rows",
]

type Kind = Literal["prices", "pv_forecast", "load_forecast"]


class EntityError(SourceError):
    """The entity's forecast cannot be read as configured."""


def _number(row: dict[str, object], key: str, where: str) -> float:
    if key not in row:
        raise EntityError(f"{where}: no {key!r}")
    try:
        return number(row[key], f"{where}: {key!r}")
    except RowError as err:
        raise EntityError(str(err)) from err


def parse_rows(
    rows: Sequence[object],
    *,
    start_key: str,
    end_key: str | None,
    period: timedelta | None,
    keys: Mapping[str, str],
    unit: str | None,
    kind: Kind,
    issued_at: datetime,
    source: str,
    where: str,
    export: float | None = None,
) -> Forecast[Price] | Forecast[Quantiles] | Forecast[float]:
    """Rows of a forecast, each with its start, end (or ``period``) and values under
    ``keys``, as the library's forecast of ``kind`` (an entity's or an action's)."""
    quantity: Quantity = "price" if kind == "prices" else "power"
    if unit is None and quantity == "power":
        raise EntityError(f"{where}: no unit (set unit, or the entity's unit_of_measurement)")

    def scaled(value: float) -> float:
        if unit is None:  # a price with no unit: per kWh
            return value
        try:
            return convert(value, unit, quantity)
        except UnitError as err:
            raise EntityError(f"{where}: {err}") from err

    intervals: list[Interval[object]] = []
    for index, row in enumerate(rows):
        at = f"{where}[{index}]"
        if not isinstance(row, dict):
            raise EntityError(f"{at}: not an object")
        try:
            start = time(row.get(start_key), at)
            end = time(row.get(end_key), at) if end_key is not None else None
        except RowError as err:
            raise EntityError(str(err)) from err
        if end is None:
            end = start + (period or timedelta(0))
        value: object
        if kind == "prices":
            p_export = scaled(_number(row, keys["export"], at)) if "export" in keys else export
            if p_export is None:
                raise EntityError(f"{at}: no export price (keys.export, or a fixed export)")
            value = Price(scaled(_number(row, keys["import"], at)), p_export)
        elif "value" in keys:
            kw = scaled(_number(row, keys["value"], at))
            value = Quantiles.point(kw) if kind == "pv_forecast" else kw
        else:
            value = Quantiles(
                {
                    q: scaled(_number(row, keys[name], at))
                    for q, name in ((0.1, "p10"), (0.5, "p50"), (0.9, "p90"))
                }
            )
        try:
            intervals.append(Interval(start, end, value))
        except ValueError as err:
            raise EntityError(f"{at}: {err}") from err
    intervals.sort(key=lambda interval: interval.start)
    try:
        found: Forecast[Any] = Forecast(tuple(intervals), issued_at, source)
        return found
    except ValueError as err:
        raise EntityError(f"{where}: {err}") from err


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
    try:
        issued = time(state.get("last_updated"), f"{entity} last_updated")
    except RowError as err:
        raise EntityError(str(err)) from err
    unit = config.unit if config.unit is not None else attributes.get("unit_of_measurement")
    return parse_rows(
        rows,
        start_key=config.start_key,
        end_key=config.end_key,
        period=config.period,
        keys=config.keys,
        unit=None if unit is None else str(unit),
        kind=kind,
        issued_at=issued,
        source=f"entity:{entity}",
        where=f"{entity} {config.attribute}",
        export=config.export,
    )


async def fetch_forecast(
    client: HomeAssistantApi, config: EntityForecast, kind: Kind
) -> Forecast[Price] | Forecast[Quantiles] | Forecast[float]:
    """Read the entity and parse its forecast (:func:`parse_forecast`)."""
    return parse_forecast(await client.get_state(config.entity), config, kind)
