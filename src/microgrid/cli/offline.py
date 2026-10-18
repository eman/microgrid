"""The library run on a dataset: the setup from the dataset's configuration files, one
valuation at a moment, and the live value over a span, each read from the dataset as a
client would have had it then; and the dataset's bindings, for the commands that read
what its meters recorded. Everything is on the engine's slot
(``EngineConfig.slot``, the dataset's ``engine.toml``).
"""

from __future__ import annotations

import asyncio
import tomllib
from bisect import bisect_right
from collections.abc import Iterator
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Final, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from microgrid.cli.dataset import (
    DC_LOAD_FORECAST,
    DOMAIN,
    EXPORT_PRICE,
    HEARTBEAT,
    HOUSE,
    IMPORT_PRICE,
    INVERTER,
    SOC,
    Dataset,
    DatasetError,
    DatasetHistory,
    Entry,
)
from microgrid.cli.history import (
    Bindings,
    DcLoadMeter,
    Limits,
    LotsError,
    read_recorded,
    step_slots,
)
from microgrid.cli.inputs import (
    ASSUMED_DC_LOAD,
    inputs,
    no_dc_forecast,
    prices_in_force,
    recorded_prices,
)
from microgrid.engine import (
    Engine,
    EngineConfig,
    EngineConfigError,
    Inputs,
    LiveValue,
    Valuation,
    ValuationKind,
)
from microgrid.engine.ledger import Ledger, Lots
from microgrid.engine.series import Sample, Series, alive, require_utc, slot_floor, value_at
from microgrid.engine.units import normalise
from microgrid.plant import Plant, PlantConfig, PlantConfigError

__all__ = [
    "ENGINE_FILE",
    "LOTS_RUN_IN",
    "PLANT_FILE",
    "TELEMETRY",
    "Setup",
    "client_causes",
    "dataset_bindings",
    "dataset_lots",
    "dataset_lots_async",
    "engine_inputs",
    "limits_of",
    "live",
    "load_setup",
    "lots_missing",
    "lots_or_why",
    "read_plant",
    "value",
    "value_with",
    "with_recorded_prices",
]

#: A client's own cause, beside the engine's: the heartbeat says the telemetry path was
#: silent, so nothing measured can be vouched for.
TELEMETRY: Final = "telemetry"

PLANT_FILE = "plant.toml"
ENGINE_FILE = "engine.toml"
#: How far back ``microgrid now`` runs the ledger for the battery's lots: two days drew
#: down what the battery held before (energy of unknown origin) at every slot of a
#: 16-day replay on one home's plant. A battery
#: held full for longer keeps some of it.
LOTS_RUN_IN: Final = timedelta(days=2)


@dataclass(frozen=True, slots=True)
class Setup:
    """The plant and the engine a dataset is run with."""

    plant: Plant
    engine: Engine


def read_plant(path: Path) -> PlantConfig:
    """The plant's file (``plant/2``, TOML) at ``path``; its ``timezone`` names the zone.
    Raises :class:`PlantConfigError` naming each problem."""
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except OSError as err:
        raise PlantConfigError([f"cannot read {path}: {err.strerror or err}"]) from err
    except tomllib.TOMLDecodeError as err:
        raise PlantConfigError([f"{path} is not TOML: {err}"]) from err
    zone = data.get("timezone")
    try:
        tz = ZoneInfo(zone) if isinstance(zone, str) else None
    except ZoneInfoNotFoundError, ValueError:
        tz = None
    if tz is None:
        why = "is required: the zone's name" if zone is None else f"{zone!r} is not a known zone"
        raise PlantConfigError([f"timezone: {why}"])
    return PlantConfig.from_mapping(data, tz=tz)


def load_setup(
    data: Path,
    plant: Path | None = None,
    engine: Path | None = None,
    *,
    beside: bool = True,
) -> Setup:
    """The plant from ``plant`` (default: the dataset's ``plant.toml``), the engine from
    ``engine`` (default: the dataset's ``engine.toml`` where ``beside``, else the
    library's defaults)."""
    the_plant = Plant(read_plant(plant or data / PLANT_FILE))
    path = engine or data / ENGINE_FILE
    mapping: dict[str, object] = {"schema": "engine/2"}
    if engine is not None or (beside and path.exists()):
        try:
            mapping = tomllib.loads(path.read_text(encoding="utf-8"))
        except OSError as err:
            raise EngineConfigError([f"cannot read {path}: {err.strerror}"]) from err
        except tomllib.TOMLDecodeError as err:
            raise EngineConfigError([f"{path} is not TOML: {err}"]) from err
    return Setup(the_plant, Engine(EngineConfig.from_mapping(mapping, plant=the_plant)))


def engine_inputs(
    setup: Setup, read: Inputs, at: datetime, lots: Series[Lots] | None = None
) -> Inputs:
    """The engine's inputs at ``at`` from a dataset's (``inputs.inputs``), with the
    battery's ``lots`` (:func:`dataset_lots`). A plant with DC-bus loads and no forecast
    of them takes a forecast of 0, labelled so (``inputs.no_dc_forecast``)."""
    plant = setup.plant
    dc_load = read.dc_load
    config = setup.engine.config
    if plant.has_dc_loads and dc_load is None:
        dc_load = Series((Sample(at, no_dc_forecast(at, config.horizon, config.slot)),), at)
    return Inputs(
        prices=read.prices,
        pv=read.pv,
        load=read.load,
        dc_load=dc_load,
        soc=read.soc,
        dispatch_as_modelled=read.dispatch_as_modelled,
        lots=lots if plant.storage is not None else None,
    )


def client_causes(setup: Setup, dataset: Dataset) -> tuple[str, ...]:
    """What the command assumed in valuing ``dataset``, named beside the engine's causes
    (never in place of them): a plant with DC-bus loads and no forecast of them valued on
    0 kW (:data:`~microgrid.cli.inputs.ASSUMED_DC_LOAD`)."""
    if setup.plant.has_dc_loads and DC_LOAD_FORECAST not in dataset.entries:
        return (ASSUMED_DC_LOAD,)
    return ()


def limits_of(dataset: Dataset, given: Limits | None = None) -> Limits:
    """How the dataset's record is judged: ``given`` (a command's flags), else the
    dataset's own (its manifest's ``limits``), else the defaults."""
    return given or dataset.limits or Limits()


def with_recorded_prices(dataset: Dataset) -> Dataset:
    """``dataset`` with the prices in force as recorded series (``import_price``,
    ``export_price``), derived from its price forecasts where it records none
    (``inputs.prices_in_force``): a dataset need not carry the prices twice. Recorded ones
    are per kWh, all in one currency (``inputs.recorded_prices``)."""
    dataset = recorded_prices(dataset)
    entries = dict(dataset.entries)
    if IMPORT_PRICE in entries and EXPORT_PRICE in entries:
        return dataset
    prices = inputs(dataset).prices
    if prices is None:
        return dataset
    imported, exported = prices_in_force(prices)
    entries.setdefault(IMPORT_PRICE, Entry("number", imported))
    entries.setdefault(EXPORT_PRICE, Entry("number", exported))
    return Dataset(entries, dataset.limits)


async def dataset_lots_async(
    setup: Setup, dataset: Dataset, *, limits: Limits | None = None
) -> Series[Lots] | None:
    """The battery's lots over the dataset, after each slot (the ledger over what its
    meters recorded, ``history.read_recorded``, on the engine's slot): what a valuation at
    a moment reads as the energy the battery holds then. The prices are the recorded ones,
    else those its forecasts put in force (:func:`with_recorded_prices`); the path is
    alive within the heartbeat's limit where it has one (``limits``: :func:`limits_of`),
    else wherever each series is known. None where the dataset cannot say (no battery, no
    recorded meters or SOC, or no prices): the engine then values all the battery holds as
    of unknown origin, at the import, and names ``battery_lots``; :func:`lots_missing`
    says why. The ledger starts at the dataset's first whole slot, all it holds then of
    unknown origin."""
    return (await lots_or_why(setup, dataset, limits=limits))[0]


def lots_missing(setup: Setup, dataset: Dataset, *, limits: Limits | None = None) -> str | None:
    """Why :func:`dataset_lots` gives none for a plant with a battery; None where it gives
    lots, or the plant has no battery."""
    return asyncio.run(lots_or_why(setup, dataset, limits=limits))[1]


async def lots_or_why(
    setup: Setup, dataset: Dataset, *, limits: Limits | None = None
) -> tuple[Series[Lots] | None, str | None]:
    """:func:`dataset_lots_async`'s lots, and where it gives none for a plant with a
    battery, why (else None)."""
    plant = setup.plant
    if plant.storage is None:
        return None, None
    battery = plant.battery
    slot = setup.engine.config.slot
    dataset = with_recorded_prices(dataset)
    needed = (IMPORT_PRICE, EXPORT_PRICE, SOC, *plant.meters)
    missing = [
        name
        for name in needed
        if name not in dataset.entries or not dataset.entries[name].series.samples
    ]
    if missing:
        return None, f"the dataset records no {', '.join(missing)}"
    series = [dataset.series(name) for name in needed]
    first = max(found.samples[0].at for found in series if found is not None)
    last = min(found.known_until for found in series if found is not None)
    start, end = slot_floor(first, slot) + slot, slot_floor(last, slot)
    if end <= start:
        return None, "the dataset's meters, SOC and prices share no whole slot"
    bindings = dataset_bindings(setup, dataset, limits=limits_of(dataset, limits))
    try:
        # Read to all that is known: the reading is half-open, and the last whole slot's
        # closing SOC may be stamped at its end exactly. Only whole slots are kept.
        recorded = await read_recorded(DatasetHistory(dataset), bindings, start, last, step=slot)
    except (LotsError, DatasetError) as err:
        return None, str(err)
    if recorded.stored_before is None:
        return None, f"no SOC at {start.isoformat()}, where the ledger starts"
    # Unknown: as bought, through the grid's path.
    ledger = Ledger(battery, unknown_kwh=recorded.stored_before, storage=plant.storage)
    points = [
        (done.start + slot, Lots(done.start + slot, done.held, done.pool))
        for done in step_slots(ledger, recorded, plant)
        if done.start + slot <= end
    ]
    return Series.of(points, known_until=last), None  # vouched for as far as the meters


def dataset_lots(
    setup: Setup, dataset: Dataset, *, limits: Limits | None = None
) -> Series[Lots] | None:
    """:func:`dataset_lots_async`, for a caller outside an event loop."""
    return asyncio.run(dataset_lots_async(setup, dataset, limits=limits))


def value(
    setup: Setup, dataset: Dataset, at: datetime, *, limits: Limits | None = None
) -> Valuation:
    """The valuation at ``at`` from what the dataset says was received by then
    (``unpriced`` without a price for the slot under way), the battery's lots from the
    dataset's meters (:func:`dataset_lots`)."""
    return value_with(setup, dataset, at, dataset_lots(setup, dataset, limits=limits))


def value_with(
    setup: Setup, dataset: Dataset, at: datetime, lots: Series[Lots] | None
) -> Valuation:
    """:func:`value` with the battery's ``lots`` given (read once for many valuations)."""
    require_utc(at)
    read = inputs(dataset, has_dc_loads=setup.plant.has_dc_loads)
    return setup.engine.value(engine_inputs(setup, read, at, lots), at=at)


@dataclass(frozen=True, slots=True)
class _Measured:
    """The dataset's measurements as the live value reads them, and its heartbeats."""

    inputs: Inputs
    beats: tuple[datetime, ...] | None


def _floor(series: Series[float]) -> Series[float]:
    """At 0 at least, as the service reads PV and the house load (``live_telemetry``)."""
    return Series(
        tuple(Sample(s.at, None if s.value is None else max(0.0, s.value)) for s in series.samples),
        series.known_until,
        series.unit,
    )


def _vouched[T](series: Series[T], spans: tuple[tuple[datetime, datetime], ...]) -> Series[T]:
    """``series`` with no value where the heartbeat says the path was silent (outside
    ``spans``, from the limit after a beat on), from the first beat on, as the service's
    cache keeps it (``TelemetryCache.history``)."""
    if not spans:
        return Series((), series.known_until, series.unit)
    edges = {edge for span in spans for edge in span if edge <= series.known_until}
    starts = [start for start, _ in spans]
    points: list[tuple[datetime, T | None]] = []
    for at in sorted({sample.at for sample in series.samples} | edges):
        if at < starts[0]:
            continue
        span = bisect_right(starts, at) - 1
        found = value_at(series, at) if at < spans[span][1] else None
        if not points or points[-1][1] != found:
            points.append((at, found))
    return Series.of(points, known_until=series.known_until, unit=series.unit)


def _measured(setup: Setup, dataset: Dataset, stale_after: timedelta) -> _Measured:
    """The meters, the house load, the SOC and the dispatch as series, with no value where
    the heartbeat was silent longer than ``stale_after``."""
    meters: dict[str, Series[float]] = {}
    for name, kind in setup.plant.meters.items():
        series = dataset.series(name)
        if series is None:
            raise DatasetError(f"{name}: the plant's meter is not in the dataset")
        kw = normalise(cast("Series[float]", series), "power")
        meters[name] = _floor(kw) if kind == "pv" else kw
    house = dataset.series(HOUSE)
    beats = dataset.series(HEARTBEAT)
    read = inputs(dataset, has_dc_loads=setup.plant.has_dc_loads)
    measured = Inputs(
        prices=None,
        soc=read.soc,
        dispatch_as_modelled=read.dispatch_as_modelled,
        meters=meters,
        measured_load=(
            None if house is None else _floor(normalise(cast("Series[float]", house), "power"))
        ),
    )
    if beats is None:
        return _Measured(measured, None)
    spans = alive(beats, stale_after)

    def vouched[T](series: Series[T] | None) -> Series[T] | None:
        return None if series is None else _vouched(series, spans)

    return _Measured(
        Inputs(
            prices=None,
            soc=vouched(measured.soc),
            dispatch_as_modelled=vouched(measured.dispatch_as_modelled),
            meters={name: _vouched(series, spans) for name, series in meters.items()},
            measured_load=vouched(measured.measured_load),
        ),
        tuple(s.at for s in beats.samples),
    )


def _silent(measured: _Measured, at: datetime, stale_after: timedelta) -> bool:
    """Whether the heartbeat was silent at ``at`` longer than ``stale_after``: the
    ``telemetry`` cause, as the service names it (``live_telemetry``)."""
    if measured.beats is None:
        return False
    index = bisect_right(measured.beats, at)
    return not index or at - measured.beats[index - 1] > stale_after


def live(
    setup: Setup,
    dataset: Dataset,
    start: datetime,
    end: datetime,
    *,
    every: timedelta,
    issue_every: timedelta,
    valid_for: timedelta,
    limits: Limits | None = None,
) -> Iterator[tuple[datetime, LiveValue]]:
    """The live value every ``every`` in ``[start, end)``, valued again at the first
    reading in each ``issue_every`` from ``start`` (each valuation valid for
    ``valid_for``), as a client running the library would have had it. A valuation that
    comes to nothing (``unpriced``) leaves the last one in place, as the service keeps its
    last issue (past its ``valid_until``, the engine names ``issue``)."""
    require_utc(start, end)
    stale_after = limits_of(dataset, limits).telemetry
    measured = _measured(setup, dataset, stale_after)
    read = inputs(dataset, has_dc_loads=setup.plant.has_dc_loads)
    lots = dataset_lots(setup, dataset, limits=limits)
    valuation: Valuation | None = None
    valid_until: datetime | None = None
    next_issue = start
    at = start
    while at < end:
        if at >= next_issue:
            found = setup.engine.value(engine_inputs(setup, read, at, lots), at=at)
            if found.kind is not ValuationKind.UNPRICED:
                valuation, valid_until = found, at + valid_for
            while next_issue <= at:
                next_issue += issue_every
        yield (
            at,
            setup.engine.live(
                replace(measured.inputs, lots=lots),
                at=at,
                valuation=valuation,
                valid_until=valid_until,
                external=(TELEMETRY,) if _silent(measured, at, stale_after) else (),
            ),
        )
        at += every


#: A DC-bus load's last reading stands from anywhere earlier in the dataset.
_DATASET_CARRY_IN: Final = timedelta(days=3650)


def _entity(name: str) -> str:
    return f"{DOMAIN}.{name}"


def dataset_bindings(setup: Setup, dataset: Dataset, *, limits: Limits | None = None) -> Bindings:
    """The dataset's entries as the record the commands read (``dataset.<name>``, read
    by :class:`~microgrid.cli.dataset.DatasetHistory`), for its plant (``setup``), its
    silences judged by ``limits`` (:func:`limits_of`). Its heartbeat where it has one
    (else each series is trusted as far as it is known), its house meter and its
    inverter's AC meter where it has them."""
    judged = limits_of(dataset, limits)
    by_kind = {kind: name for name, kind in setup.plant.meters.items()}
    for kind in ("pv", "grid", "battery"):
        if kind not in by_kind:
            raise DatasetError(f"the plant meters no {kind}: the commands read one")
    soc = dataset.series(SOC)
    return Bindings(
        plant=setup.plant,
        pv=_entity(by_kind["pv"]),
        grid=_entity(by_kind["grid"]),
        battery=_entity(by_kind["battery"]),
        soc=_entity(SOC),
        soc_unit=soc.unit if soc is not None and soc.unit is not None else "",
        house=_entity(HOUSE),
        heartbeat=_entity(HEARTBEAT) if HEARTBEAT in dataset.entries else None,
        telemetry=judged.telemetry,
        import_price=_entity(IMPORT_PRICE),
        export_price=_entity(EXPORT_PRICE),
        inverter=_entity(INVERTER) if INVERTER in dataset.entries else None,
        dc_load=(
            DcLoadMeter(_entity(by_kind["dc_load"]), _DATASET_CARRY_IN, judged.dc_stale_after)
            if "dc_load" in by_kind and setup.plant.has_dc_loads
            else None
        ),
    )
