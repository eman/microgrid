"""Shared fixtures for the engine's tests: plants, a config and its inputs, built
without the site TOML (the engine knows no site file)."""

import ast
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

import microgrid.engine
import microgrid.plant
from legacy_dispatch import NativePlant
from microgrid.engine import EngineConfig, Inputs, LiveReading, Pricing
from microgrid.engine._stored import Amount, Lots, Source
from microgrid.engine._value import Model
from microgrid.engine.extend import PlantModel
from microgrid.engine.series import SLOT, Forecast, Interval, Price, Quantiles, Series
from microgrid.plant import (
    Battery,
    BatteryPart,
    Dispatch,
    GridLimits,
    Inverter,
    Plant,
    PlantConfig,
    PvArray,
)
from microgrid.plant._paths import PathStorage
from microgrid.plant.flows import SlotFlow, pv_slice
from microgrid.plant.port import ChargeSource

PACIFIC = ZoneInfo("America/Los_Angeles")

type Connection = Literal["ac", "dc_bus"]


def config_of(native: NativePlant, *, connection: Connection = "ac") -> PlantConfig:
    """``native``'s parameters as one topology: one AC-coupled PV array, and the
    battery behind its own inverter (``"ac"``) or folded on a hybrid's DC bus
    (``"dc_bus"``), that inverter holding ``native``'s draws. The service's tests have
    their own (``tests/conftest.py``): the engine's stand alone, as the engine does."""
    draws = Inverter(
        standby_kw=native.standby_kw,
        working_kw=native.working_kw,
        working_threshold_kw=native.working_threshold_kw,
    )
    on_the_bus = connection == "dc_bus"
    return PlantConfig(
        tz=native.timezone,
        pv=(PvArray("pv", "ac"),),
        battery=BatteryPart(
            native.battery, connection=connection, inverter=None if on_the_bus else draws
        ),
        hybrid=draws if on_the_bus else None,
        grid=GridLimits(export_limit_kw=native.grid.export_limit_kw),
        dispatch=Dispatch(grid_charge=() if native.grid_charge is None else (native.grid_charge,)),
    )


def plant_of(native: NativePlant, *, connection: Connection = "ac") -> Plant:
    """The :class:`Plant` :func:`config_of` describes: today's dispatch on ``native``."""
    return Plant(config_of(native, connection=connection))


def storage_of(plant: Plant) -> PathStorage:
    """``plant``'s storage, for a plant with a battery (``Plant.storage`` is None
    without one)."""
    storage = plant.storage
    assert storage is not None, "a plant with a battery"
    return storage


def soc(value: float | None) -> float:
    """An SOC a plant with a battery projects (None only without one)."""
    assert value is not None, "the plant has a battery"
    return value


NOW = datetime(2026, 9, 23, 19, 3, tzinfo=UTC)  # 12:03 PDT
IMPORT, EXPORT = 0.373, 0.092
#: Three PV scenarios, as the service's ``fast_site`` has: the tests need a valid
#: valuation, not its exact prices (``test_value.py`` checks those).
CONFIG = EngineConfig(
    plant=plant_of(
        NativePlant(Battery(12.0, 6.0, 6.0, eta_charge=0.96, eta_discharge=0.96), PACIFIC)
    ),
    pricing=Pricing(scenarios=3),
)


def received[V](forecast: Forecast[V], at: datetime | None = None) -> Series[Forecast[V]]:
    """``forecast`` as a series of its one issue, received at ``at`` (its issue time by
    default)."""
    when = forecast.issued_at if at is None else at
    return Series.of([(when, forecast)], known_until=when)


def price_forecast(now: datetime = NOW, hours: int = 49) -> Forecast[Price]:
    """Flat hourly prices from the hour of ``now``, issued then."""
    first = now.replace(minute=0, second=0, microsecond=0)
    hour = timedelta(hours=1)
    return Forecast(
        tuple(
            Interval(first + h * hour, first + (h + 1) * hour, Price(IMPORT, EXPORT))
            for h in range(hours)
        ),
        now,
        "test",
    )


def prices(now: datetime = NOW, hours: int = 49) -> Series[Forecast[Price]]:
    """:func:`price_forecast`, received when issued."""
    return received(price_forecast(now, hours))


def pv_forecast(polled: datetime = NOW - timedelta(hours=1)) -> Forecast[Quantiles]:
    """Three days of half-hourly PV from local midnight, peaking at 13:00, polled then."""
    first = datetime(2026, 9, 23, 7, 0, tzinfo=UTC)  # midnight PDT
    intervals = []
    for i in range(3 * 48):
        start = first + i * timedelta(minutes=30)
        local = start.astimezone(PACIFIC)
        kw = max(0.0, 6.0 * (1 - abs(local.hour + local.minute / 60 - 13) / 6))
        intervals.append(
            Interval(
                start,
                start + timedelta(minutes=30),
                Quantiles({0.1: kw * 0.6, 0.5: kw, 0.9: kw * 1.2}),
            )
        )
    return Forecast(tuple(intervals), polled, "test")


def pv(polled: datetime = NOW - timedelta(hours=1)) -> Series[Forecast[Quantiles]]:
    """:func:`pv_forecast`, received when polled."""
    return received(pv_forecast(polled))


def load_forecast(
    starts: Sequence[datetime],
    kw: Sequence[float | None],
    *,
    issued: datetime = NOW,
    source: str = "test",
) -> Forecast[float]:
    """A load forecast on slots: ``kw`` per start, a slot with None left out (a gap)."""
    return Forecast(
        tuple(
            Interval(start, start + SLOT, value)
            for start, value in zip(starts, kw, strict=True)
            if value is not None
        ),
        issued,
        source,
    )


def load_starts(now: datetime = NOW) -> list[datetime]:
    """Two days of slots from the slot of ``now``."""
    first = now.replace(minute=now.minute - now.minute % 10, second=0, microsecond=0)
    return [first + k * SLOT for k in range(288)]


def load(now: datetime = NOW, kw: float | None = 0.7) -> Series[Forecast[float]]:
    """``kw`` over :func:`load_starts`, issued and received at ``now``."""
    return received(load_forecast(load_starts(now), [kw] * 288, issued=now))


def reading[T](value: T | None, as_of: datetime | None = None) -> Series[T] | None:
    """A measured input that took ``value`` at ``as_of`` (None with neither: no series),
    vouched for well past the tests' moments."""
    if as_of is None:
        assert value is None, "a value takes effect at a time"
        return None
    return Series.of([(as_of, value)], known_until=max(as_of, NOW) + timedelta(days=2))


def inputs(**overrides: object) -> Inputs:
    """The inputs at :data:`NOW`: prices, PV, load, the SOC and the dispatch."""
    values: dict[str, object] = {
        "prices": prices(),
        "pv": pv(),
        "load": load(),
        "soc": reading(0.4, NOW - timedelta(minutes=2)),
        "dispatch_as_modelled": reading(True, NOW - timedelta(hours=3)),
        "lots": lots(),
    }
    values.update(overrides)
    return Inputs(**values)  # type: ignore[arg-type]


def lots(per_kwh: float = 0.10, as_of: datetime | None = None) -> Series[Lots]:
    """The battery's lots, as a client's ledger gives them: 5 kWh at ``per_kwh`` per
    stored kWh, booked ``as_of`` (10 minutes before :data:`NOW`), vouched for to NOW."""
    booked = as_of or NOW - timedelta(minutes=10)
    pool = Amount(5.0, 5.0 * per_kwh)
    return Series.of([(booked, Lots(booked, {Source.PV: pool}, pool))], known_until=NOW)


#: A neutral example plant, with every part of today's model: a lossy conversion, the
#: inverter's draw, the wear and meter factors (``plant.examples``' ``ac_coupled_hybrid``
#: numbers, not this site's).
EXAMPLE_PLANT = NativePlant(
    Battery(
        12.0,
        6.0,
        6.0,
        eta_charge=0.96,
        eta_discharge=0.96,
        wear_charge_per_kwh=0.02,
        wear_discharge_per_kwh=0.02,
        loss_charge_per_kw=0.003,
        loss_discharge_per_kw=0.003,
        meter_charge=0.99,
        meter_discharge=1.0,
    ),
    PACIFIC,
    standby_kw=0.03,
    working_kw=0.06,
)


def live_reading(**overrides: object) -> LiveReading:
    """A reading now. ``pv_kw``, ``grid_kw``, ``battery_kw`` and ``dc_load_kw`` go into
    the default meters, ``pv``, ``grid``, ``battery`` and ``dc_load``; None leaves one
    out."""
    values: dict[str, object] = {
        "at": NOW + timedelta(seconds=20),
        "load_kw": 0.7,
        "soc": 1.0,
        "dispatch_as_modelled": True,
    }
    meters: dict[str, object] = {"pv": 6.0}
    for key, name in (
        ("pv_kw", "pv"),
        ("grid_kw", "grid"),
        ("battery_kw", "battery"),
        ("dc_load_kw", "dc_load"),
    ):
        if key in overrides:
            meters[name] = overrides.pop(key)
    values["meters"] = {name: kw for name, kw in meters.items() if kw is not None}
    values.update(overrides)
    return LiveReading(**values)  # type: ignore[arg-type]


def site_meters(
    plant: PlantModel, pv: float = 0.0, grid: float = 0.0, battery: float = 0.0
) -> dict[str, float | None]:
    """Today's three meters as a reading, each that ``plant`` has (its default set)."""
    values: dict[str, float | None] = {"pv": pv, "grid": grid, "battery": battery}
    return {name: values[name] for name in plant.meters if name in values}


# --- Who imports whom: the engine's import rules -------------------------------------

#: The engine package's directory.
ENGINE = Path(microgrid.engine.__file__).parent
PLANT = Path(microgrid.plant.__file__).parent


def _library(name: str) -> bool:
    """A module of the library's models: the engine, the plant model or the shared
    checks."""
    return name in ("microgrid.engine", "microgrid.plant", "microgrid._checks") or name.startswith(
        ("microgrid.engine.", "microgrid.plant.")
    )


def engine_imports(source: str) -> set[str]:
    """The engine and plant-model modules ``source`` imports; a relative import is
    ``"<relative>"``."""
    found: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom) and node.level > 0:
            found.add("<relative>")
        elif isinstance(node, ast.ImportFrom) and node.module in ("microgrid.engine", "microgrid"):
            found.update(f"{node.module}.{alias.name}" for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
        elif isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
    return {name for name in found if name == "<relative>" or _library(name)}


def _plant_root(root: Path) -> Path:
    """Where the plant package's files are: beside the engine for the real tree; a
    ``plant`` directory under a test's own ``root``."""
    return PLANT if root == ENGINE else root / "plant"


def module_source(name: str, root: Path = ENGINE) -> Path | None:
    """The file of engine or plant module ``name`` under ``root`` (a module, or a
    subpackage's ``__init__``); None for a package itself, a relative import or a missing
    one."""
    if name.startswith("microgrid.engine."):
        base, rest = root, name.removeprefix("microgrid.engine.")
    elif name.startswith("microgrid.plant."):
        base, rest = _plant_root(root), name.removeprefix("microgrid.plant.")
    else:
        return None
    relative = Path(*rest.split("."))
    for path in (base / relative.with_suffix(".py"), base / relative / "__init__.py"):
        if path.exists():
            return path
    return None


def reach(module: str, root: Path = ENGINE) -> set[str]:
    """Every engine or plant module ``module`` imports, directly or through others; a name
    it cannot follow (a package, a relative import) is kept, unfollowed."""
    seen: set[str] = set()
    todo = [module]
    while todo:
        path = module_source(todo.pop(), root)
        if path is None:
            continue
        for name in engine_imports(path.read_text(encoding="utf-8")) - seen:
            seen.add(name)
            if module_source(name, root) is not None:
                todo.append(name)
    return seen


def rerun_cost(model: Model, scenario: int, first: int, kw: float, run: timedelta = SLOT) -> float:
    """The test oracle for the price, per kWh of ``kw`` drawn for ``run`` from slot
    ``first`` in one scenario, by running the plant again with it added, until its
    battery is back where it would have been (or to the horizon's end), and pricing
    what changed as the rule prices a kWh: export lost at its price, grid charging at
    its import and the charge's wear, the battery's throughput at its wear, the house's
    imports inside the run at their price and after it at the cheapest import since
    the run's start, and a battery still lower at the horizon's end at what it would
    have displaced. A vanishing load costs the slot's price (``_value.price_flows``)."""
    plant = model.plant
    storage = plant.storage
    base = model.flows[scenario][first:]
    count = len(base)
    shares: list[float] = []
    left = run
    for duration in model.durations[first:]:
        if left <= timedelta(0):
            break
        shares.append(min(1.0, left / duration))
        left -= duration
    hours = [duration / timedelta(hours=1) for duration in model.durations[first:]]
    energy = sum(kw * share * hours[k] for k, share in enumerate(shares))
    if energy <= 0:
        raise ValueError("the run draws no energy")
    house = list(model.load_kw[first:])
    for k, share in enumerate(shares):
        house[k] += kw * share
    run_length = len(shares)

    def back(index: int, flow: SlotFlow) -> bool:
        if index < run_length - 1:
            return False
        after, before = flow.soc_end, base[index].soc_end
        return after is None or before is None or abs(after - before) <= 1e-9

    dc_load = model.dc_load_kw
    moved = plant.project(
        model.starts[first:],
        model.durations[first:],
        pv_slice(model.scenarios[scenario], first),
        house,
        base[0].soc_start,
        dc_load_kw=None if dc_load is None else dc_load[first:],
        held_at_start=base[0].dispatch.hold_session,
        stop=back,
    )
    p_import, p_export = model.p_import[first:], model.p_export[first:]
    c_grid, c_pv, c_dis = 0.0, 0.0, 0.0
    if storage is not None:
        c_grid = storage.charge_wear_rate(ChargeSource.GRID)
        c_pv = storage.charge_wear_rate(ChargeSource.PV)
        c_dis = storage.wear_discharge_per_kwh
    c_dc, asked = 0.0, storage is None
    cost = 0.0
    cheapest = p_import[0]
    for k, (after, before) in enumerate(zip(moved, base, strict=False)):
        cheapest = min(cheapest, p_import[k])
        buy = p_import[k] if k < run_length else cheapest
        moved_dc = after.dc_pv_to_battery - before.dc_pv_to_battery
        if moved_dc and not asked and storage is not None:
            c_dc, asked = storage.charge_wear_rate(ChargeSource.DC_PV), True
        cost += hours[k] * (
            (after.grid_to_load - before.grid_to_load) * buy
            + (after.grid_to_battery - before.grid_to_battery) * (p_import[k] + c_grid)
            - (after.pv_export - before.pv_export) * p_export[k]
            - (after.battery_export - before.battery_export) * p_export[k]
            + (after.pv_to_battery - before.pv_to_battery) * c_pv
            + (after.battery_out - before.battery_out) * c_dis
            + moved_dc * c_dc
        )
    last = len(moved) - 1
    if storage is not None and len(moved) == count and not back(last, moved[last]):
        day = round(timedelta(days=1) / SLOT)
        day_price = min(min(p_import), min(model.p_import[max(0, len(model.starts) - day) :]))
        soc_moved, soc_base = moved[last].soc_end, base[last].soc_end
        assert soc_moved is not None and soc_base is not None
        cost += (soc_base - soc_moved) * storage.capacity_kwh * storage.displaced_value(day_price)
    return cost / energy
