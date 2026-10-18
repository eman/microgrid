"""The inputs as they stand now, read from the configured sources (``microgrid now``).

Each input is read from its source once, at ``now``: a forecast is one issue received
now; a measured value is the value in force, from when it changed. An input whose source
is not configured, or cannot be read, is left out and the reason kept
(:attr:`Read.problems`): the valuation then names it as a cause, as it would any input it
cannot use. Nothing is guessed in its place.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from microgrid.cli.dataset import (
    BATTERY_FIRST,
    LOAD_FORECAST,
    PRICES,
    PV_FORECAST,
    SOC,
    Dataset,
    Entry,
)
from microgrid.cli.history import Limits
from microgrid.cli.inputs import no_dc_forecast
from microgrid.cli.offline import LOTS_RUN_IN, Setup, lots_or_why
from microgrid.cli.sources import SourceError, emhass, entity, predbat, solcast, tariffkit
from microgrid.cli.sources.config import (
    Emhass,
    EntityForecast,
    Measured,
    Predbat,
    Solcast,
    Sources,
    TariffKit,
)
from microgrid.cli.sources.fetch import fetch_measured
from microgrid.cli.sources.home_assistant import HaClient
from microgrid.engine import Engine, EngineConfig, Inputs
from microgrid.engine.ledger import Lots
from microgrid.engine.series import Forecast, Sample, Series
from microgrid.engine.units import UnitError, convert
from microgrid.plant import Plant

__all__ = [
    "Read",
    "read_now",
]


@dataclass(frozen=True, slots=True)
class Read:
    """The inputs read now, where each came from, and why any is missing."""

    inputs: Inputs
    #: Each input read, and its source (``prices: tariffkit``).
    sources: dict[str, str] = field(default_factory=dict)
    #: Each input not read, and why (not configured, or the source's error).
    problems: dict[str, str] = field(default_factory=dict)
    #: The inputs as a dataset's entries, to keep or re-run offline.
    entries: dict[str, Entry] = field(default_factory=dict)


def _issue[V](forecast: Forecast[V], now: datetime, unit: str | None = None) -> Series[Forecast[V]]:
    """One forecast, received now, in the engine's unit (``unit``, for a dataset)."""
    return Series((Sample(now, forecast),), known_until=now, unit=unit)


def _price_unit(source: object) -> str:
    """The prices' unit as read: per kWh, in the source's currency where it names one
    (the engine never converts a currency)."""
    unit = getattr(source, "unit", None)
    currency = unit.rpartition("/")[0] if isinstance(unit, str) and "/" in unit else ""
    if isinstance(source, TariffKit):
        currency = "USD"  # TariffKit prices in US dollars
    return f"{currency or 'currency'}/kWh"


async def _forecast(
    client: HaClient | None,
    source: object,
    kind: entity.Kind,
    engine: EngineConfig,
    now: datetime,
) -> Forecast[Any]:
    if client is None:  # pragma: no cover - the configuration requires the connection
        raise SourceError("no Home Assistant connection")
    if isinstance(source, TariffKit):
        entry = source.config_entry or await tariffkit.discover_config_entry(client)
        hours = math.ceil(engine.horizon / timedelta(hours=1))
        return await tariffkit.fetch_rates(
            client, config_entry=entry, horizon=timedelta(hours=hours), now=now
        )
    if isinstance(source, Solcast):
        read = await solcast.fetch_forecast(client, source.entities, source.last_polled)
        return read.forecast
    if isinstance(source, Emhass):
        config = emhass.ForecastEntity(source.entity, source.attribute, source.value_key)
        return await emhass.fetch_load_forecast(client, config, period=source.period)
    if isinstance(source, Predbat):
        return await predbat.fetch_load_forecast(client, source, now=now)
    if isinstance(source, EntityForecast):
        return await entity.fetch_forecast(client, source, kind)
    raise SourceError(f"no reader for {type(source).__name__}")  # pragma: no cover


def _name(source: object) -> str:
    return {
        TariffKit: "tariffkit",
        Solcast: "solcast",
        Emhass: "emhass",
        Predbat: "predbat",
        EntityForecast: "entity",
    }[type(source)]


async def _measured(
    client: HaClient,
    measured: Measured,
    now: datetime,
    read: dict[str, Any],
    problems: dict[str, str],
) -> None:
    if measured.soc is not None:
        try:
            state = await client.get_state(measured.soc)
            unit = state.get("attributes", {}).get("unit_of_measurement", measured.soc_unit)
            value = convert(float(state["state"]), unit or None, "fraction")
            changed = datetime.fromisoformat(str(state["last_changed"])).astimezone(UTC)
            read["soc"] = Series((Sample(min(changed, now), value),), known_until=now)
        except (SourceError, KeyError, ValueError, UnitError) as err:
            problems["soc"] = f"{measured.soc}: {err}"
    if measured.dispatch_as_modelled is not None:
        read["dispatch"] = Series((Sample(now, measured.dispatch_as_modelled),), known_until=now)
    elif measured.dispatch is not None:
        try:
            state = await client.get_state(measured.dispatch)
            changed = datetime.fromisoformat(str(state["last_changed"])).astimezone(UTC)
            modelled = str(state.get("state")) in measured.modelled_modes
            read["dispatch"] = Series((Sample(min(changed, now), modelled),), known_until=now)
        except (SourceError, KeyError, ValueError) as err:
            problems["dispatch"] = f"{measured.dispatch}: {err}"


async def _lots(
    client: HaClient | None,
    sources: Sources,
    plant: Plant,
    engine: EngineConfig,
    now: datetime,
    run_in: timedelta,
    limits: Limits | None,
) -> tuple[Series[Lots] | None, str | None]:
    """The battery's lots now: the ledger over the ``run_in`` before ``now``, on what the
    ``[measured]`` section's entities recorded (the history ``fetch`` reads), as a
    dataset's lots are (``offline.lots_or_why``). None, and why, where it cannot."""
    if engine.pricing.battery_value != "stored_cost":
        return None, None  # the make-up rule reads no lots
    if sources.measured is None or client is None:
        return None, "no [measured] section and [home_assistant] to read the record from"
    try:
        entries = await fetch_measured(client, sources.measured, now - run_in, now)
    except (SourceError, ValueError) as err:
        return None, str(err)
    return await lots_or_why(Setup(plant, Engine(engine)), Dataset(entries), limits=limits)


async def read_now(
    sources: Sources,
    plant: Plant,
    engine: EngineConfig,
    now: datetime,
    *,
    client: HaClient | None = None,
    lots_run_in: timedelta = LOTS_RUN_IN,
    limits: Limits | None = None,
) -> Read:
    """Every configured input as it stands at ``now`` (UTC), and for a plant with a
    battery its lots: the ledger over the ``lots_run_in`` before ``now``, from what the
    ``[measured]`` entities recorded, the record judged by ``limits``. ``client``: Home
    Assistant's, else one made from ``sources`` (and closed here)."""
    if now.utcoffset() != timedelta(0):
        raise ValueError("now must be UTC")
    used: dict[str, str] = {}
    problems: dict[str, str] = {}
    measured: dict[str, Any] = {}
    forecasts: dict[str, Forecast[Any]] = {}
    lots: Series[Lots] | None = None
    owned = client is None and sources.home_assistant is not None
    if owned and sources.home_assistant is not None:
        client = HaClient.connect(sources.home_assistant)
    try:
        wanted: list[tuple[str, object, entity.Kind]] = [("prices", sources.prices, "prices")]
        if plant.has_pv and plant.pv_per_array:
            # A plant with arrays of both couplings takes a forecast per array; sources/1
            # reads one for the site. A dataset can hold one per array.
            problems["pv_forecast"] = (
                "this plant takes a PV forecast per array, which sources/1 does not read yet:"
                " use a dataset (pv_forecast.<array>)"
            )
        elif plant.has_pv:
            wanted.append(("pv_forecast", sources.pv_forecast, "pv_forecast"))
        wanted.append(("load_forecast", sources.load_forecast, "load_forecast"))
        for name, source, kind in wanted:
            if source is None:
                problems[name] = "no source configured"
                continue
            try:
                forecasts[name] = await _forecast(client, source, kind, engine, now)
                used[name] = _name(source)
            except (SourceError, ValueError) as err:
                problems[name] = str(err)
        if plant.storage is not None:
            if sources.measured is None or client is None:
                problems["soc"] = "no source configured"
            else:
                await _measured(client, sources.measured, now, measured, problems)
                used.update({k: "home_assistant" for k in ("soc", "dispatch") if k in measured})
            lots, why = await _lots(client, sources, plant, engine, now, lots_run_in, limits)
            if lots is not None:
                used["battery_lots"] = f"the ledger over {lots_run_in} of home_assistant history"
            elif why is not None:
                problems["battery_lots"] = f"{why} (all it holds priced at the import)"
    finally:
        if owned and client is not None:
            await client.aclose()
    prices = forecasts.get("prices")
    pv = forecasts.get("pv_forecast")
    load = forecasts.get("load_forecast")
    dc = no_dc_forecast(now, engine.horizon, engine.slot) if plant.has_dc_loads else None
    inputs = Inputs(
        prices=_issue(prices, now) if prices is not None else None,
        pv=_issue(pv, now) if pv is not None else None,
        load=_issue(load, now) if load is not None else None,
        dc_load=_issue(dc, now) if dc is not None else None,
        soc=measured.get("soc") if plant.storage is not None else None,
        dispatch_as_modelled=measured.get("dispatch") if plant.storage is not None else None,
        lots=lots,
    )
    entries: dict[str, Entry] = {}
    for name, entry_kind, found, unit in (
        (PRICES, "price", prices, _price_unit(sources.prices)),
        (PV_FORECAST, "quantiles", pv, "kW"),
        (LOAD_FORECAST, "number", load, "kW"),
    ):
        if found is not None:
            entries[name] = Entry(entry_kind, _issue(found, now, unit), forecasts=True)  # type: ignore[arg-type]
    if "soc" in measured:
        entries[SOC] = Entry("number", measured["soc"])
    if "dispatch" in measured:
        entries[BATTERY_FIRST] = Entry("bool", measured["dispatch"])
    return Read(inputs, used, problems, entries)
