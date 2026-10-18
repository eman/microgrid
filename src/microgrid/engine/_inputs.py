"""What a valuation rests on, as time series.

A client hands :meth:`microgrid.engine.Engine.value` an :class:`Inputs` and a moment,
and the engine reads each series as it stood then: the forecasts issued by then, each
measured value in force then. Nothing issued or received after it is read, so the same
call over recorded inputs at a past moment is a replay that cannot look ahead.

Pure: no I/O.
"""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime

from microgrid._checks import require_utc
from microgrid.engine._stored import Lots
from microgrid.engine.series import Forecast, Price, Quantiles, Series


@dataclass(frozen=True, slots=True)
class Inputs:
    """Everything one valuation rests on, as series; the moment is given beside it.

    Each forecast input is a series of forecasts: a sample is a forecast the client
    received at the sample's time. The engine reads what was received by the moment,
    whatever the source's own stamp says. Prices and PV come from the issue received
    last, which replaces those before it; the loads, slot by slot, from the newest fresh
    issue that covers the slot (:func:`microgrid.engine.series.compose`), as a source
    that publishes from the next half hour needs. A sample with no value (the source
    answered nothing) leaves the forecast read before it in force until it is too old.

    A measured input (the SOC, the dispatch) is a series of values: the engine reads the
    value in force at the moment, if the client vouches for the series up to it
    (``known_until``), and when that value took effect.

    The live value (:meth:`microgrid.engine.Engine.live`) reads the measured series as
    well: the meters and the house load, smoothed over time, as they stood at its moment.

    An input the plant does not have is left None: PV for a plant without arrays; the
    SOC and the dispatch for one without a battery; ``dc_load`` for one with no load on
    a DC bus. Passing one is a mistake (``ValueError``).
    """

    #: Import and export prices per kWh; None: no prices, so no valuation. Their age is
    #: their issue's (``EngineConfig.price_max_age``): a client whose source restamps
    #: the same rates at each fetch (TariffKit does) records each fetch as a sample, or
    #: the rates age from the first.
    prices: Series[Forecast[Price]] | None
    #: PV, kW in its source's terms, at quantiles: for the site, or per array by name
    #: (a plant with AC and DC arrays takes it per array).
    pv: Series[Forecast[Quantiles]] | Mapping[str, Series[Forecast[Quantiles]]] | None = None
    #: The AC side's load, kW, as the plant meters it.
    load: Series[Forecast[float]] | None = None
    #: The DC-bus loads' draw, DC kW at the loads, for a plant with any; ``load`` then
    #: excludes them. Two keywords, as the plant model's projection takes them.
    dc_load: Series[Forecast[float]] | None = None
    #: The battery's state of charge, 0-1 (``units.normalise(series, "fraction")``).
    soc: Series[float] | None = None
    #: Whether the plant runs the dispatch its model describes (a work mode the model
    #: covers); not so, or unknown, is the ``work_mode`` cause.
    dispatch_as_modelled: Series[bool] | None = None
    #: The plant's meters, kW by name (``PlantModel.meters``), each in its configured
    #: sign, as measured: what the live value smooths (:meth:`Engine.live`). A meter
    #: missing is unknown; a name the plant does not meter raises. A valuation does not
    #: read them.
    meters: Mapping[str, Series[float]] = field(default_factory=dict)
    #: The house load as the plant meters it, kW, as measured: the live value's.
    measured_load: Series[float] | None = None
    #: The battery's lots: what it holds by source and what that cost
    #: (:class:`~microgrid.engine.ledger.Lots`, a client's ledger). A battery kWh is priced
    #: at the running average cost of all it holds (``Lots.pool``; ``Pricing.battery_value``);
    #: without them, all it holds is of unknown origin, at the import price, and the output
    #: names ``battery_lots`` (tariffkit#67 R-14), the valuation standing.
    lots: Series[Lots] | None = None


def issued[V](series: Series[Forecast[V]] | None, at: datetime) -> list[Forecast[V]]:
    """The forecasts the client had received by ``at``, in the order received."""
    require_utc(at)
    if series is None:
        return []
    return [s.value for s in series.samples if s.at <= at and s.value is not None]


@dataclass(frozen=True, slots=True)
class Read[T]:
    """A measured input as read at a moment: its value (None: not usable), and when the
    value in force then took effect (None: there was none)."""

    value: T | None = None
    as_of: datetime | None = None


def reading_at[T](series: Series[T] | None, at: datetime) -> Read[T]:
    """The value in force at ``at`` and when it took effect: the value None where there
    is none, where it is None, or where ``at`` is past ``known_until`` (the path went
    quiet: the value was last known then, and is not vouched for now)."""
    require_utc(at)
    if series is None:
        return Read()
    index = bisect_right(series.samples, at, key=lambda sample: sample.at)
    if not index:
        return Read()
    sample = series.samples[index - 1]
    return Read(sample.value if at <= series.known_until else None, sample.at)
