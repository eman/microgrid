"""A dataset (:mod:`microgrid.cli.dataset`) as the engine's inputs: each entry by its
name, in the engine's units. The step from what was read to
:class:`~microgrid.engine.Inputs`; the reader returns series only.
"""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Final, cast

from microgrid.cli.dataset import (
    BATTERY_FIRST,
    DC_LOAD_FORECAST,
    EXPORT_PRICE,
    IMPORT_PRICE,
    LOAD_FORECAST,
    PRICES,
    PV_FORECAST,
    SOC,
    Dataset,
    DatasetError,
    Kind,
    Value,
)
from microgrid.engine import Inputs
from microgrid.engine.series import (
    SLOT,
    Forecast,
    Interval,
    Price,
    Quantiles,
    Sample,
    Series,
    require_utc,
    slot_floor,
)
from microgrid.engine.units import Quantity, UnitError, convert, normalise

__all__ = [
    "ASSUMED_DC_LOAD",
    "NO_DC_FORECAST",
    "currency",
    "inputs",
    "no_dc_forecast",
    "prices_in_force",
    "recorded_prices",
]


def inputs(dataset: Dataset, *, has_dc_loads: bool = False) -> Inputs:
    """The engine's inputs, by their names (:mod:`microgrid.cli.dataset`), each
    in the engine's units: the SOC a fraction, power kW, prices per kWh. Raises
    :class:`DatasetError` for an entry of the wrong kind or a unit that is not its
    quantity's (no unit is a fraction for the SOC, and none at all for a forecast)."""
    read = _values(dataset, SOC, "number")
    soc = None if read is None else _normalised(SOC, cast("Series[float]", read), "fraction")
    arrays = sorted(n for n in dataset.entries if n.startswith(f"{PV_FORECAST}."))
    if arrays and PV_FORECAST in dataset.entries:
        raise DatasetError(f"{PV_FORECAST}: for the site or per array, not both")
    pv: Series[Forecast[Quantiles]] | dict[str, Series[Forecast[Quantiles]]] | None
    if arrays:
        pv = {n.split(".", 1)[1]: _pv(dataset, n) for n in arrays}
    else:
        pv = _pv(dataset, PV_FORECAST) if PV_FORECAST in dataset.entries else None
    return Inputs(
        prices=_converted(dataset, PRICES, "price", "price", _price_in),
        pv=pv,
        load=_converted(dataset, LOAD_FORECAST, "number", "power", _power_in),
        dc_load=(
            _converted(dataset, DC_LOAD_FORECAST, "number", "power", _power_in)
            if has_dc_loads
            else None
        ),
        soc=soc,
        dispatch_as_modelled=cast("Series[bool] | None", _values(dataset, BATTERY_FIRST, "bool")),
    )


def currency(dataset: Dataset) -> str | None:
    """The currency the dataset's prices are in: what their units name before the ``/``
    (``EUR`` of ``EUR/MWh``); None where none names one. Raises :class:`DatasetError`
    where two name different ones: a currency is never converted, so a dataset's prices
    must all be in one."""
    named: dict[str, str] = {}
    for name in (PRICES, IMPORT_PRICE, EXPORT_PRICE):
        entry = dataset.entries.get(name)
        unit = None if entry is None else entry.series.unit
        if isinstance(unit, str) and unit.rpartition("/")[0]:
            named[name] = unit.rpartition("/")[0]
    if len(set(named.values())) > 1:
        found = ", ".join(f"{name} in {money}" for name, money in named.items())
        raise DatasetError(f"prices in more than one currency ({found}): none is converted")
    return next(iter(named.values()), None)


def recorded_prices(dataset: Dataset) -> Dataset:
    """``dataset`` with its recorded prices (``import_price``, ``export_price``) per kWh,
    as its forecast prices are read (a per-MWh price converted; no unit: per kWh). Raises
    :class:`DatasetError` for a unit that is not a price's, or prices in more than one
    currency (:func:`currency`)."""
    currency(dataset)
    entries = dict(dataset.entries)
    for name in (IMPORT_PRICE, EXPORT_PRICE):
        entry = entries.get(name)
        if entry is None or entry.series.unit is None:
            continue
        if entry.kind != "number":
            raise DatasetError(f"{name}: must be of kind 'number'")
        priced = _normalised(name, cast("Series[float]", entry.series), "price")
        entries[name] = replace(entry, series=priced)
    return replace(dataset, entries=entries)


def _pv(dataset: Dataset, name: str) -> Series[Forecast[Quantiles]]:
    pv = _converted(dataset, name, "quantiles", "power", _quantiles_in)
    assert pv is not None
    return pv


def _values(dataset: Dataset, name: str, kind: Kind) -> Series[Value] | None:
    series = dataset.series(name)
    if series is not None and dataset.entries[name].kind != kind:
        raise DatasetError(f"{name}: must be of kind {kind!r}")
    return series


def _converted[V](
    dataset: Dataset,
    name: str,
    kind: Kind,
    quantity: Quantity,
    to: Callable[[V, str | None, Quantity], V],
) -> Series[Forecast[V]] | None:
    """Forecasts ``name``, each value in the engine's unit for ``quantity``."""
    series = dataset.forecasts(name)
    if series is None:
        return None
    if dataset.entries[name].kind != kind:
        raise DatasetError(f"{name}: must be of kind {kind!r}")
    unit = series.unit
    try:
        return Series(
            tuple(
                Sample(
                    sample.at,
                    None
                    if sample.value is None
                    else Forecast(
                        tuple(
                            Interval(i.start, i.end, to(cast("V", i.value), unit, quantity))
                            for i in sample.value.intervals
                        ),
                        sample.value.issued_at,
                        sample.value.source,
                    ),
                )
                for sample in series.samples
            ),
            series.known_until,
        )
    except UnitError as err:
        raise DatasetError(f"{name}: {err}") from err


def _normalised(name: str, series: Series[float], quantity: Quantity) -> Series[float]:
    try:
        return normalise(series, quantity)
    except UnitError as err:
        raise DatasetError(f"{name}: {err}") from err


def _power_in(value: float, unit: str | None, quantity: Quantity) -> float:
    return convert(value, unit, quantity)


def _price_in(value: Price, unit: str | None, quantity: Quantity) -> Price:
    return Price(convert(value.p_import, unit, quantity), convert(value.p_export, unit, quantity))


def _quantiles_in(value: Quantiles, unit: str | None, quantity: Quantity) -> Quantiles:
    return Quantiles({q: convert(kw, unit, quantity) for q, kw in value.kw.items()})


#: The source a DC-bus load's forecast names when none was given (:func:`no_dc_forecast`).
NO_DC_FORECAST: Final = "none: no forecast"
#: The client's own cause (kept beside the engine's, never in place of one) where it
#: valued a plant's DC-bus loads on :func:`no_dc_forecast`: the engine was given a
#: forecast, so it names nothing; the command does.
ASSUMED_DC_LOAD: Final = "dc_load_forecast"


def no_dc_forecast(now: datetime, horizon: timedelta, step: timedelta = SLOT) -> Forecast[float]:
    """A DC-bus load's forecast where no source gives one: 0 from the slot of ``step``
    under way at ``now`` (the engine's ``EngineConfig.slot``) to the horizon's end and a
    slot beyond, as one interval, so it holds every slot of the valuation whatever their
    length; issued at ``now`` (so it never makes the load forecast's ``as_of`` older than
    the house's). The plant model then holds the battery for a session only from the SOC
    it reads (the hold rule; nothing here forecasts a load: R-9). A
    client that values on it says so: :data:`ASSUMED_DC_LOAD`."""
    require_utc(now)
    first = slot_floor(now, step)
    return Forecast((Interval(first, first + horizon + step, 0.0),), now, NO_DC_FORECAST)


def prices_in_force(prices: Series[Forecast[Price]]) -> tuple[Series[float], Series[float]]:
    """The import and the export price in force over time, as a client that received
    ``prices``' issues knew them (what a ledger is charged at where no price was recorded
    as it took effect: a dataset with price forecasts alone). At each moment, the value of
    the issue received last by then, where it covers that moment, as the engine reads
    prices (the issue received last, alone); none where it does not. An issue counts from
    when it was received, never before: nothing is read ahead."""
    issues = [(s.at, s.value) for s in prices.samples if s.value is not None]
    received = [at for at, _ in issues]
    known_until = prices.known_until
    moments = sorted(
        {at for at, _ in issues}
        | {
            edge
            for at, forecast in issues
            for interval in forecast.intervals
            for edge in (max(interval.start, at), max(interval.end, at))
            if edge <= known_until
        }
    )
    points: list[tuple[datetime, Price | None]] = []
    for moment in moments:
        newest = bisect_right(received, moment) - 1
        found: Price | None = None
        if newest >= 0:
            intervals = issues[newest][1].intervals
            index = bisect_right(intervals, moment, key=lambda i: i.start) - 1
            if index >= 0 and moment < intervals[index].end:
                found = intervals[index].value
        if not points or points[-1][1] != found:
            points.append((moment, found))
    unit = prices.unit

    def side(pick: Callable[[Price], float]) -> Series[float]:
        return Series.of(
            ((at, None if p is None else pick(p)) for at, p in points),
            known_until=known_until,
            unit=unit,
        )

    return side(lambda p: p.p_import), side(lambda p: p.p_export)
