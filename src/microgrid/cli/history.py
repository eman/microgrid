"""What a plant's meters recorded, read for the commands that score against them: the
battery's lots (``ledger``), the plant model against its meters (``check-plant``), the
valuations' scores (``replay``) and the battery's fit (``fit-battery``).

The record is read through :class:`History`, a port a dataset answers
(:class:`~microgrid.cli.dataset.DatasetHistory`) and any recorder a client has (a
service's database). :class:`Bindings` say which of its series is which: the
plant's meters, the SOC, the house meter, the heartbeat that proves the telemetry path
alive, and the recorded prices.

The meters are integrated by the library's ledger (``microgrid.engine.ledger``: PV, grid,
battery and a DC-bus load, allocated at every change, only while the path was alive:
within the heartbeat's limit where the bindings name one, else wherever each series is
known), the SOC read at each slot boundary, and the prices each slot was charged at as
the time-weighted mean of the recorded price in force over it (``series.means``): any
price resolution, on any slot, and no price read before it took effect. A client corrects
a recorder that writes a change late (a sensor updating seconds after the hour) at
its edge, as for any series. A PV lot's basis is the export price when it was
charged (what the site gave up to store it), a grid lot's the import price
(docs/methodology/lots.md). Every function takes the slot it works on
(``EngineConfig.slot``; :data:`~microgrid.engine.series.SLOT` by default).
"""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, time, timedelta
from typing import Final, Protocol
from zoneinfo import ZoneInfo

from microgrid.engine.analysis import ground_truth as lgt
from microgrid.engine.extend import ChargeSource, Flows, SlotFlow
from microgrid.engine.ledger import Ledger, LedgerSlot, Lots, integrate
from microgrid.engine.series import (
    SLOT,
    Series,
    alive,
    means,
    require_utc,
    signed_means,
    slot_starts,
    value_at,
)
from microgrid.engine.units import convert
from microgrid.plant import Plant

__all__ = [
    "DAY",
    "DC_STALE_AFTER",
    "TELEMETRY",
    "WEEK",
    "Bindings",
    "DcLoadMeter",
    "History",
    "LedgerRun",
    "Limits",
    "LotsError",
    "Recorded",
    "read_recorded",
    "read_recording",
    "run_ledger",
    "step_slots",
    "stored_costs",
]

#: How far back a value is carried into a window: a day (a sensor that holds still,
#: PV at night, writes nothing).
DAY: Final = timedelta(days=1)
#: Read raw meters a week at a time: a busy sensor writes ~15k rows a day.
WEEK: Final = timedelta(days=7)
_HOUR: Final = timedelta(hours=1)

#: How long a heartbeat may be silent before the readings it vouches for are unknown,
#: where the configuration says nothing (a dataset's ``limits``, a command's
#: ``--stale-after``). Three minutes suits a heartbeat that reports every few seconds:
#: it was measured on one home's plant, whose heartbeat's longest silence in a week was
#: 157 s. A recorder with another cadence sets its own.
TELEMETRY: Final = timedelta(minutes=3)
#: How long a DC-bus load's reading other than 0 may be silent before it is stale (the 0
#: that ended its draw was lost), where the configuration says nothing (a dataset's
#: ``limits``, a command's ``--dc-stale-after``). Five minutes suits a meter that writes
#: every few seconds while it draws: measured on one home's DC charger, which wrote
#: every 5 s (median) and was silent at most 103 s during a session. A meter with another
#: cadence sets its own.
DC_STALE_AFTER: Final = timedelta(minutes=5)


@dataclass(frozen=True, slots=True)
class Limits:
    """How a record's silences are judged: the heartbeat's (:data:`TELEMETRY` by
    default) and a DC-bus load's reading other than 0 (:data:`DC_STALE_AFTER`)."""

    telemetry: timedelta = TELEMETRY
    dc_stale_after: timedelta = DC_STALE_AFTER


class LotsError(RuntimeError):
    """The record cannot be read as the commands need it: no prices, no SOC."""


class History(Protocol):
    """States as recorded, each as a series over ``[start, end)`` from the value in force
    at ``start`` (the last within ``carry_in`` before it), known until ``end``.
    ``chunk``: read so much at a time (a recorder's concern; a dataset ignores it)."""

    async def power_series(
        self,
        entity_id: str,
        start: datetime,
        end: datetime,
        *,
        carry_in: timedelta,
        chunk: timedelta = WEEK,
    ) -> Series[float]:
        """Power in kW, converted from the entity's unit."""
        ...

    async def numeric_series(
        self,
        entity_id: str,
        start: datetime,
        end: datetime,
        *,
        carry_in: timedelta,
        chunk: timedelta = WEEK,
    ) -> Series[float]:
        """Any numeric entity's values, the unit unchecked."""
        ...

    async def sample_times(
        self,
        entity_id: str,
        start: datetime,
        end: datetime,
        *,
        carry_in: timedelta,
        chunk: timedelta = WEEK,
    ) -> tuple[datetime, ...]:
        """When the entity's rows arrived (a liveness sensor's)."""
        ...


@dataclass(frozen=True, slots=True)
class DcLoadMeter:
    """A DC-bus load's meter, and its own rules: a reader that writes only on change has
    its last reading carried in from ``carry_in`` back, and a reading other than 0
    silent for ``stale_after`` is stale."""

    entity: str
    carry_in: timedelta
    stale_after: timedelta


@dataclass(frozen=True, slots=True)
class Bindings:
    """Which recorded series is which, for a plant (its meters: PV in total, the grid,
    the battery, and a DC-bus load where it has one)."""

    plant: Plant
    pv: str
    grid: str
    battery: str
    soc: str
    #: The SOC's unit (``%``; empty: a fraction).
    soc_unit: str
    #: The house meter: the load the plant served, as it meters it.
    house: str
    #: A series whose samples prove the telemetry path alive, and how long one does
    #: (:data:`TELEMETRY`). None: no heartbeat; each series is then trusted as far as it
    #: is known (``Series.known_until``), its gaps as samples with no value.
    heartbeat: str | None
    telemetry: timedelta
    #: The import and export prices in force, as recorded.
    import_price: str
    export_price: str
    #: The battery inverter's AC power, > 0 out (None: not metered).
    inverter: str | None = None
    dc_load: DcLoadMeter | None = None

    @property
    def timezone(self) -> ZoneInfo:
        return self.plant.config.tz


def _meters(bindings: Bindings, by_kind: Mapping[str, Series[float]]) -> dict[str, Series[float]]:
    """The plant's meters' series, by the plant's names. The bindings name PV in total,
    the grid, the battery and a DC-bus load; a plant metering anything else raises."""
    kinds = list(bindings.plant.meters.values())
    if any(kind not in by_kind for kind in kinds) or kinds.count("pv") > 1:
        raise LotsError(
            f"the bindings name PV in total, the grid, the battery and a DC-bus load,"
            f" not the plant's meters {dict(bindings.plant.meters)}"
        )
    return {name: by_kind[kind] for name, kind in bindings.plant.meters.items()}


@dataclass(frozen=True, slots=True)
class Recorded:
    """What was recorded over a window, slot by slot, ready for the ledger."""

    starts: tuple[datetime, ...]
    flows: tuple[Flows | None, ...]
    p_import: tuple[float, ...]
    p_export: tuple[float, ...]
    #: Metered stored energy at each slot's end, kWh (None: unknown).
    stored_after: tuple[float | None, ...]
    #: Metered stored energy at the window's start, kWh.
    stored_before: float | None
    #: The slot the window is read on.
    step: timedelta = SLOT

    @property
    def hours(self) -> float:
        """A slot's length in hours."""
        return self.step / _HOUR


async def read_recorded(
    history: History,
    bindings: Bindings,
    start: datetime,
    end: datetime,
    *,
    step: timedelta = SLOT,
) -> Recorded:
    """What was recorded over ``[start, end)``, slot by slot of ``step``.

    A slot's prices are the time-weighted means of the recorded prices in force over it;
    a slot without both moves nothing (its flows None). Raises :class:`LotsError` when no
    slot of the window has a recorded price.
    """
    require_utc(start, end)
    plant = bindings.plant
    battery = plant.battery
    starts = slot_starts(start, end, step)
    imports, exports = [
        means(await history.numeric_series(entity, start, end, carry_in=DAY), starts, step)
        for entity in (bindings.import_price, bindings.export_price)
    ]
    if all(price is None for price in imports) or all(price is None for price in exports):
        raise LotsError(
            f"no recorded prices from {bindings.import_price} or {bindings.export_price}"
        )
    pv, grid, power = [
        await history.power_series(entity, start, end, carry_in=DAY)
        for entity in (bindings.pv, bindings.grid, bindings.battery)
    ]
    limit = bindings.telemetry
    beats: tuple[datetime, ...] | None = None
    if bindings.heartbeat is not None:
        beats = await history.sample_times(bindings.heartbeat, start, end, carry_in=limit)
    soc_series = await history.numeric_series(bindings.soc, start, end, carry_in=DAY)
    dc = bindings.dc_load
    by_kind = {"pv": pv, "grid": grid, "battery": power}
    if dc is not None:
        by_kind["dc_load"] = await history.power_series(dc.entity, start, end, carry_in=dc.carry_in)
    # Each series is known to the window's end: each meter's last value stands until then.
    meters = _meters(bindings, by_kind)
    dc_meters = [name for name, kind in plant.meters.items() if kind == "dc_load"]
    flows = integrate(
        meters,
        starts,
        step,
        plant=plant,
        alive_spans=(
            None
            if beats is None
            else alive(Series.of(((at, 1.0) for at in beats), known_until=end), limit)
        ),
        stale_nonzero=(
            dict.fromkeys(dc_meters, dc.stale_after) or None if dc is not None else None
        ),
    )
    priced = [
        imported is not None and exported is not None
        for imported, exported in zip(imports, exports, strict=True)
    ]
    flows = [flow if ok else None for flow, ok in zip(flows, priced, strict=True)]

    def stored(moment: datetime) -> float | None:
        # The SOC in force at the boundary, if the path was alive then (a heartbeat
        # within the telemetry limit before it; without one, the SOC's series known).
        alive_then = True
        if beats is not None:
            last = bisect_right(beats, moment)
            alive_then = last > 0 and moment - beats[last - 1] <= limit
        value = value_at(soc_series, moment)
        if not alive_then or value is None or battery is None:
            return None
        return convert(value, bindings.soc_unit, "fraction") * battery.capacity_kwh

    return Recorded(
        starts=tuple(starts),
        flows=tuple(flows),
        p_import=tuple(price or 0.0 for price in imports),
        p_export=tuple(price or 0.0 for price in exports),
        stored_after=tuple(stored(at + step) for at in starts),
        stored_before=stored(start),
        step=step,
    )


@dataclass(frozen=True, slots=True)
class LedgerRun:
    """The ledger over a window, with the prices each slot was valued at."""

    starts: tuple[datetime, ...]
    slots: tuple[LedgerSlot, ...]
    p_import: tuple[float, ...]
    p_export: tuple[float, ...]


def step_slots(ledger: Ledger, recorded: Recorded, plant: Plant | None = None) -> list[LedgerSlot]:
    """``recorded``'s slots, each stepped through ``ledger``: with ``plant``, each slot
    also as its model's (``Plant.metered_slot``), so a ledger with the plant's storage
    books each charge by its path (``Ledger``)."""
    return [
        ledger.step(
            start,
            recorded.flows[index],
            recorded.hours,
            pv_price=recorded.p_export[index],  # a PV lot: the export given up
            grid_price=recorded.p_import[index],
            stored_after=recorded.stored_after[index],
            slot=_metered_slot(plant, recorded, index),
        )
        for index, start in enumerate(recorded.starts)
    ]


def _metered_slot(plant: Plant | None, recorded: Recorded, index: int) -> SlotFlow | None:
    """Slot ``index`` of ``recorded`` as ``plant``'s model would have it (its port's and
    the hybrid's flows worked out from the metered AC); None without a plant, a battery or
    the slot's flows. The SOCs it carries are not read by the charge's paths."""
    flows = recorded.flows[index]
    if plant is None or flows is None or plant.storage is None:
        return None
    return plant.metered_slot(recorded.starts[index], 0.0, 0.0, flows)


async def run_ledger(
    history: History,
    bindings: Bindings,
    start: datetime,
    end: datetime,
    *,
    step: timedelta = SLOT,
) -> LedgerRun:
    """The ledger over ``[start, end)`` on slots of ``step``; what the battery held at
    ``start`` is unknown energy."""
    battery = bindings.plant.battery
    if battery is None:
        raise LotsError("the plant has no battery")
    recorded = await read_recorded(history, bindings, start, end, step=step)
    if recorded.stored_before is None:
        raise LotsError("no battery SOC at the window's start")
    plant = bindings.plant
    ledger = Ledger(battery, unknown_kwh=recorded.stored_before, storage=plant.storage)
    slots = step_slots(ledger, recorded, plant)
    return LedgerRun(recorded.starts, tuple(slots), recorded.p_import, recorded.p_export)


def stored_costs(
    recorded: Recorded, plant: Plant, lots: Series[Lots] | None = None
) -> list[float | None]:
    """Per slot of ``recorded``, the running average cost of what the battery held at
    its start, per stored kWh (:attr:`Ledger.pool`): what a battery kWh realised
    under the stored-cost rule. From ``lots`` (the ledger the valuations read, so both
    sides start alike) where they are in force; else the ledger over ``recorded``, all
    it held at the window's start of unknown origin, at the first slot's import, as if
    bought (R-7)."""
    battery, storage = plant.battery, plant.storage
    found: list[float | None] = []
    if not recorded.starts or storage is None:
        return found
    # All it held at the start at the first slot's import, through the grid's path.
    as_bought = recorded.p_import[0] / storage.stored_per_kwh(ChargeSource.GRID)
    ledger = Ledger(
        battery,
        unknown_kwh=recorded.stored_before or 0.0,
        unknown_basis=as_bought,
        storage=storage,
    )
    for index, start in enumerate(recorded.starts):
        held = None if lots is None else value_at(lots, start)
        pool = held.pool if held is not None and held.pool is not None else ledger.pool
        found.append(pool.unit if pool.kwh > 1e-9 else None)
        ledger.step(
            start,
            recorded.flows[index],
            recorded.hours,
            pv_price=recorded.p_export[index],
            grid_price=recorded.p_import[index],
            stored_after=recorded.stored_after[index],
            slot=_metered_slot(plant, recorded, index),
        )
    return found


async def read_recording(
    history: History,
    bindings: Bindings,
    pinned: Plant,
    first: date,
    last: date,
    *,
    step: timedelta = SLOT,
) -> lgt.Recording:
    """The local days ``first`` to ``last``'s meters for the ground truth, on slots of
    ``step``, split through the pinned conversion without the DC-bus loads
    (``lgt.meters_plant``), and the DC-bus load's meter where the plant has one."""
    tz = bindings.timezone
    start = datetime.combine(first, time(), tz).astimezone(UTC)
    end = datetime.combine(last + timedelta(days=1), time(), tz).astimezone(UTC)
    starts = list(slot_starts(start, end, step))
    recorded = await read_recorded(
        history,
        replace(bindings, plant=lgt.meters_plant(pinned), dc_load=None),
        start,
        end,
        step=step,
    )
    if list(recorded.starts) != starts:
        raise LotsError("the recorded slots do not match the window's")

    async def power(entity: str, carry_in: timedelta = DAY) -> Series[float]:
        return await history.power_series(entity, start, end, carry_in=carry_in, chunk=WEEK)

    pv = means(await power(bindings.pv), starts, step)
    house = means(await power(bindings.house), starts, step)
    grid_in, grid_out = signed_means(await power(bindings.grid), starts, step)
    out_kw = in_kw = None
    if bindings.inverter is not None:
        out_kw, in_kw = signed_means(await power(bindings.inverter), starts, step)
    dc_kw = None
    if bindings.dc_load is not None:
        # A meter that writes only on change: its last reading carried in (``carry_in``).
        dc = bindings.dc_load
        dc_kw = means(await power(dc.entity, dc.carry_in), starts, step)
    return lgt.Recording(
        step=step,
        starts=tuple(starts),
        flows=tuple(recorded.flows),
        stored_after=tuple(recorded.stored_after),
        stored_before=recorded.stored_before,
        p_import=tuple(recorded.p_import),
        p_export=tuple(recorded.p_export),
        pv_kw=tuple(pv),
        house_kw=tuple(house),
        grid_import_kw=tuple(grid_in),
        grid_export_kw=tuple(grid_out),
        inverter_out_kw=None if out_kw is None else tuple(out_kw),
        inverter_in_kw=None if in_kw is None else tuple(in_kw),
        dc_load_kw=None if dc_kw is None else tuple(dc_kw),
    )
