"""The legacy dispatch, frozen as the oracle the general dispatch is held to:
``NativePlant`` and ``simulate``, as the library ran them for today's configurations (an
AC-coupled plant with its battery on its own inverter, or folded on a hybrid's DC bus
with nothing else there) until ``Plant.project`` ran ``project_paths`` for every
configuration. Moved here unchanged from ``microgrid.plant._dispatch``; only tests use
it. With them, the battery's answers (``PlantStorage``) and the meter split
(``allocate``, ``consumption_kw``) today's configurations kept until the general
dispatch took them over, moved here unchanged from ``plant._plant`` and
``plant._meters``: the oracles ``PathStorage`` and ``split_paths`` are held to.

Where each kWh goes: the AC-coupled plant's native dispatch.

The price model reaches it only through the port (``Plant.project``, which runs it for
the configurations it describes).

This predicts the plant's energy flows so energy can be valued; it decides
nothing. The model is the plant's native behaviour, as measured on the reference
plant's meters over 61 days to 2026-09-22, per 10-minute slot:

1. PV serves the house (plus the inverter's standby draw, and its overhead
   while the battery charges or discharges) first.
2. PV left over charges the battery, up to its charge limit and saturation.
3. Inside the grid-charge window, the grid tops the battery up toward its
   target when PV alone will not get it there by the window's end.
4. PV still left over is exported (curtailed above an export limit, if any).
5. A shortfall comes from the battery down to its floor, then from the grid;
   while the plant tops up, or holds at its target, the grid serves the house.

Charging stores ``eta_charge`` of each AC kWh, delivering a kWh draws ``1 /
eta_discharge`` stored, both at low power: the inverter's loss grows with the
power it converts (``Battery.stored_by``, ``Battery.drawn_by``). Each slot
reports what that conversion lost and what the inverter drew beside its flows
(``SlotFlow.conversion_loss``, ``inverter_draw``).

Its flows (:class:`~microgrid.plant.flows.SlotFlow`), and the rules for the
surplus they leave and each slot's regime, are :mod:`microgrid.plant.flows`'s,
shared with the price model. This model does not export from the
battery.

Pure: no I/O. Times are UTC; the grid-charge window is local time.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, fields
from datetime import datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

import pytest

from microgrid._checks import require_finite, require_utc
from microgrid.engine._timeline import SLOT
from microgrid.engine.extend import ChargeSource, Flows, SlotDispatch
from microgrid.plant import WORKING_KW, Battery, GridCharge, Plant, PlantConfig
from microgrid.plant._battery import Grid
from microgrid.plant.flows import SlotFlow

_EPS = 1e-9


@dataclass(frozen=True, slots=True)
class NativePlant:
    """The plant's native behaviour and its parameters."""

    battery: Battery
    timezone: ZoneInfo
    grid: Grid = field(default_factory=Grid)
    #: The inverter's own draw, not in the load series, kW: always, and more while the
    #: battery works (moves over :data:`WORKING_KW`; 0.042 and 0.049 kW fitted by
    #: ``microgrid fit-battery`` to the reference plant's meters over 30 days to 2026-09-25).
    standby_kw: float = 0.0
    grid_charge: GridCharge | None = None
    working_kw: float = 0.0
    #: The battery works, and the inverter draws ``working_kw``, above this, kW either
    #: way (:data:`WORKING_KW`, this site's measurement; a configuration's own,
    #: ``Inverter.working_threshold_kw``).
    working_threshold_kw: float = WORKING_KW

    def __post_init__(self) -> None:
        draws = {
            "standby_kw": self.standby_kw,
            "working_kw": self.working_kw,
            "working_threshold_kw": self.working_threshold_kw,
        }
        require_finite("plant", **draws)
        for name, kw in draws.items():
            if kw < 0:
                raise ValueError(f"plant {name} must not be negative")


def _spans(
    count: int,
    step: timedelta,
    first_remaining: timedelta | None,
    durations: Sequence[timedelta] | None,
) -> list[float]:
    """Each slot's length in hours."""
    if durations is not None:
        if len(durations) != count:
            raise ValueError("durations must have one length per slot")
        if any(d <= timedelta(0) for d in durations):
            raise ValueError("durations must be positive")
        return [d / timedelta(hours=1) for d in durations]
    if first_remaining is not None and not timedelta(0) < first_remaining <= step:
        raise ValueError("first_remaining must be within one slot")
    spans = [step / timedelta(hours=1)] * count
    if first_remaining is not None and count:
        spans[0] = first_remaining / timedelta(hours=1)
    return spans


def _in_window(plant: NativePlant, start: datetime) -> tuple[bool, datetime | None]:
    window = plant.grid_charge
    if window is None:
        return False, None
    local = start.astimezone(plant.timezone)
    if not window.start <= local.time() < window.end:
        return False, None
    end = datetime.combine(local.date(), window.end, tzinfo=plant.timezone)
    return True, end.astimezone(start.tzinfo)


def _cover_end(plant: NativePlant, window_end: datetime) -> datetime | None:
    """When the window's cover period ends, in ``window_end``'s zone; None without one."""
    window = plant.grid_charge
    if window is None or window.cover_until is None:
        return None
    local = window_end.astimezone(plant.timezone)
    day = local.date() if window.cover_until > window.end else local.date() + timedelta(days=1)
    return datetime.combine(day, window.cover_until, tzinfo=plant.timezone).astimezone(
        window_end.tzinfo
    )


def simulate(
    plant: NativePlant,
    starts: Sequence[datetime],
    pv_kw: Sequence[float | None],
    load_kw: Sequence[float | None],
    soc: float,
    *,
    step: timedelta = SLOT,
    first_remaining: timedelta | None = None,
    durations: Sequence[timedelta] | None = None,
    stop: Callable[[int, SlotFlow], bool] | None = None,
) -> tuple[SlotFlow, ...]:
    """Flows for each slot from ``soc`` (0-1) at the first slot's start.

    ``first_remaining`` is how much of the first slot is still ahead when it
    is already under way (``soc`` is then the SOC now, not at its start):
    only that much energy moves in it. Flows stay mean kW over that time.
    ``durations`` gives every slot its own length instead (the live value's
    window, :mod:`microgrid.engine._live`); it overrides both.

    Stops at the first slot whose PV or load is unknown: every later flow
    depends on it, so none is returned (the caller treats them as unknown).
    Stops too after a slot for which ``stop(index, flow)`` holds.

    The inverter's draw while the battery works (``working_kw``) is drawn in a
    slot where the battery moves more than ``working_threshold_kw`` with it drawn.
    Where it would move more without it but not with it (within the draw of
    idle), the battery is taken to move without it. Looking ahead in the
    grid-charge window, it is taken as drawn throughout.
    """
    if not len(starts) == len(pv_kw) == len(load_kw):
        raise ValueError("starts, pv_kw and load_kw must have the same length")
    if not 0 <= soc <= 1:
        raise ValueError("soc must be in [0, 1]")
    battery = plant.battery
    spans = _spans(len(starts), step, first_remaining, durations)
    stored = soc * battery.capacity_kwh
    full = battery.saturation_soc * battery.capacity_kwh
    floor = battery.reserve_soc * battery.capacity_kwh
    overhead = plant.standby_kw + plant.working_kw
    flows: list[SlotFlow] = []

    def settle(index: int, pv: float, demand: float, draw: float) -> tuple[SlotFlow, float]:
        """Slot ``index`` from ``stored`` with the house drawing ``demand``, of which
        ``draw`` is the inverter's own."""
        hours = spans[index]
        start = starts[index]
        level = stored
        pv_to_load = min(pv, demand)
        spare = pv - pv_to_load
        short = demand - pv_to_load

        room = battery.charge_for(max(0.0, full - level) / hours)
        pv_to_battery = min(spare, battery.charge_kw, room)
        # What the slot's charge stores, kW: the conversion loss is the rest.
        charged = battery.stored_by(pv_to_battery)
        level += charged * hours
        spare -= pv_to_battery

        grid_to_battery = 0.0
        holding = False
        gap: float | None = None
        charging_window, window_end = _in_window(plant, start)
        if charging_window and plant.grid_charge is not None and window_end is not None:
            target = plant.grid_charge.target_soc * battery.capacity_kwh
            # What PV alone would still add before the window ends, what the grid
            # could still add then at the charge limit beside it, and what the house
            # will need from the battery after it, until the cover period ends.
            coming, later_room, cover = 0.0, 0.0, 0.0
            cover_end = _cover_end(plant, window_end)
            for later in range(index + 1, len(starts)):
                if cover_end is not None and starts[later] >= cover_end:
                    break
                if cover_end is None and starts[later] >= window_end:
                    break
                later_pv, later_load = pv_kw[later], load_kw[later]
                if later_pv is None or later_load is None:
                    break
                if starts[later] < window_end:
                    later_spare = max(0.0, later_pv - later_load - overhead)
                    by_pv = battery.stored_by(min(later_spare, battery.charge_kw))
                    coming += by_pv * spans[later]
                    later_room += (battery.stored_by(battery.charge_kw) - by_pv) * spans[later]
                else:
                    later_short = max(0.0, later_load + overhead - later_pv)
                    cover += battery.drawn_by(min(later_short, battery.discharge_kw)) * spans[later]
            if cover_end is not None:
                target = min(target, floor + cover)
            needed = min(target, full) - level - coming
            # At its target the plant holds the battery and the grid serves the house,
            # so the top-up is not undone by the slot's own draw.
            holding = needed + battery.drawn_by(min(short, battery.discharge_kw)) * hours > _EPS
            if plant.grid_charge.timing == "late":
                needed -= later_room  # the rest of the window can still add that
            if needed > _EPS:
                # The grid adds to what PV charges through the same inverter.
                by_pv = battery.stored_by(pv_to_battery)
                wanted = by_pv + min(needed, max(0.0, full - level)) / hours
                grid_to_battery = min(
                    battery.charge_kw - pv_to_battery, battery.charge_for(wanted) - pv_to_battery
                )
                grid_to_battery = max(0.0, grid_to_battery)
                charged = battery.stored_by(pv_to_battery + grid_to_battery)
                level += (charged - by_pv) * hours
            # On course to end the window at the target, with room to charge more: a
            # kWh less now would be bought back here (after an early top-up, in every
            # slot to the window's end).
            gap = needed

        limit = plant.grid.export_limit_kw
        pv_export = spare if limit is None else min(spare, limit)
        pv_curtail = spare - pv_export

        battery_to_load = 0.0
        drawn = 0.0
        if not holding:  # the plant does not discharge while topping up, or at its target
            available = battery.delivered_by(max(0.0, level - floor) / hours)
            battery_to_load = min(short, battery.discharge_kw, available)
            drawn = battery.drawn_by(battery_to_load)
            level -= drawn * hours
        grid_to_load = short - battery_to_load

        level = min(max(level, 0.0), battery.capacity_kwh)
        flow = SlotFlow(
            start=start,
            soc_start=stored / battery.capacity_kwh,
            soc_end=level / battery.capacity_kwh,
            pv_to_load=pv_to_load,
            pv_to_battery=pv_to_battery,
            pv_export=pv_export,
            pv_curtail=pv_curtail,
            battery_to_load=battery_to_load,
            grid_to_load=grid_to_load,
            grid_to_battery=grid_to_battery,
            conversion_loss=(pv_to_battery + grid_to_battery - charged) + (drawn - battery_to_load),
            inverter_draw=draw,
            dispatch=SlotDispatch(held=holding, topup_gap=gap),
        )
        return flow, level

    for index, start in enumerate(starts):
        require_utc(start)
        pv, load = pv_kw[index], load_kw[index]
        if pv is None or load is None:
            break
        flow, level = settle(index, pv, load + plant.standby_kw, plant.standby_kw)
        if flow.moving > plant.working_threshold_kw and plant.working_kw > 0:
            working, working_level = settle(index, pv, load + overhead, overhead)
            if working.moving > plant.working_threshold_kw:
                flow, level = working, working_level
        stored = level
        flows.append(flow)
        if stop is not None and stop(index, flow):
            break
    return tuple(flows)


def native_of(plant: Plant) -> NativePlant | None:
    """The legacy dispatch for ``plant``: its battery, the draws and working threshold of
    the inverter the battery converts through, the export limit, and the grid-charge
    window; None for a configuration it did not run (``_todays``). What
    ``Plant._dispatch_of`` gave until the general dispatch ran every configuration."""
    config = plant.config
    part, inverter = config.battery, config.battery_inverter
    if not _todays(config) or part is None or inverter is None:
        return None
    grid_charge = config.dispatch.grid_charge
    return NativePlant(
        battery=part.battery,
        timezone=config.tz,
        grid=Grid(export_limit_kw=config.grid.export_limit_kw),
        standby_kw=inverter.standby_kw,
        working_kw=inverter.working_kw,
        working_threshold_kw=inverter.working_threshold_kw,
        grid_charge=grid_charge[0] if grid_charge else None,
    )


#: What the legacy dispatch did not report (left at 0): where the battery's port sits.
UNREPORTED = ("crossing", "battery_port", "battery_port_in", "battery_port_out")
#: The general dispatch gives the legacy flows to float noise: 4e-14 kW measured over
#: 3000 generated configurations of today's.
TOLERANCE = 1e-9


def assert_matches(general: Sequence[SlotFlow], legacy: Sequence[SlotFlow]) -> None:
    """``general`` is ``legacy``, each number within :data:`TOLERANCE` and everything
    else (times, decisions, a gap's presence) equal, but for :data:`UNREPORTED`."""
    assert len(general) == len(legacy)
    for mine, theirs in zip(general, legacy, strict=True):
        got_all, want_all = flat(mine), flat(theirs)
        for name, want in want_all.items():
            if name in UNREPORTED:
                continue
            got = got_all[name]
            if isinstance(want, float) and isinstance(got, float):
                assert got == pytest.approx(want, abs=TOLERANCE), name
            else:
                assert got == want, name


def flat(flow: SlotFlow) -> dict[str, object]:
    """``flow``'s fields by name, its dispatch's (``SlotFlow.dispatch``) among them."""
    values = {item.name: getattr(flow, item.name) for item in fields(flow)}
    del values["dispatch"]
    return values | {item.name: getattr(flow.dispatch, item.name) for item in fields(flow.dispatch)}


# The battery's answers and the meter split, as today's configurations ran them.


@dataclass(frozen=True, slots=True)
class PlantStorage:
    """The battery as the price rule asks about it (``port.Storage``), in today's
    branch: one conversion from the AC bus to stored energy, which both sources
    charge through (a battery on its own inverter, or folded on a hybrid's DC bus).
    The branch has no DC PV, so a question about it raises ``ValueError``."""

    battery: Battery

    @property
    def capacity_kwh(self) -> float:
        return self.battery.capacity_kwh

    @property
    def reserve_soc(self) -> float:
        return self.battery.reserve_soc

    @property
    def saturation_soc(self) -> float:
        return self.battery.saturation_soc

    @property
    def wear_discharge_per_kwh(self) -> float:
        return self.battery.wear_discharge_per_kwh

    def charge_wear_rate(self, source: ChargeSource) -> float:
        # PV and the grid charge through the same conversion: one AC rate for both.
        _on_the_ac_bus(source)
        return self.battery.wear_charge_per_kwh

    def divertible_kw(self, flow: SlotFlow) -> float:
        # AC-coupled PV: what charges the battery is AC a load could take as it is
        # (``_supply.supply_stack``'s PV step).
        return flow.pv_to_battery

    def can_charge_more(
        self, flow: SlotFlow, source: ChargeSource, *, margin_kw: float = 0.0
    ) -> bool:
        # ``_value.price_flows.buys_later``: under the charge limit, and for the grid
        # no PV spare, since PV charges first and spare PV would refill it.
        _on_the_ac_bus(source)
        charging = flow.pv_to_battery + flow.grid_to_battery
        if source == ChargeSource.PV:
            return charging < self.battery.charge_kw - margin_kw
        return (
            charging < self.battery.charge_kw - margin_kw
            and flow.pv_export + flow.pv_curtail <= margin_kw
        )

    def at_discharge_limit(self, flow: SlotFlow, *, margin_kw: float = 0.0) -> bool:
        # ``_value.price_flows``: the battery has nothing more to give.
        return flow.battery_out >= self.battery.discharge_kw - margin_kw

    def can_serve(self, flow: SlotFlow, more_kw: float) -> bool:
        # ``_value.importing``: the import is regulation the battery could carry.
        return flow.battery_out + more_kw <= self.battery.discharge_kw

    def grid_refills_diverted_pv(self, flow: SlotFlow) -> bool:
        # PV charges before the plant decides its top-up (``_value.price_flows``).
        return True

    def takes_clipped_pv(self, flow: SlotFlow) -> bool:
        # Today's branch clips nothing: AC PV is clipped in its own inverters, before
        # the forecast.
        return False

    def headroom(self, flow: SlotFlow, hours: float) -> float:
        # What was ``_value.battery_headroom``'s body before the port answered it.
        battery = self.battery
        out = flow.battery_out
        soc = flow.soc_end
        if soc is None:
            raise ValueError("a plant with storage projects its SOC")
        above = (soc - battery.reserve_soc) * battery.capacity_kwh
        above -= battery.stored_by(flow.pv_to_battery + flow.grid_to_battery) * hours
        limit = battery.discharge_kw - out
        if above <= 0 or limit <= 0:
            return 0.0
        # What the whole discharge could deliver, less what it already does.
        drawn = battery.drawn_by(out) + above / hours
        return max(0.0, min(limit, battery.delivered_by(drawn) - out))

    def refill_value(self, price: float, source: ChargeSource, flow: SlotFlow) -> float:
        # ``_value.price_flows``: both sources through the one inverter, at the margin
        # of the power it already converts.
        _on_the_ac_bus(source)
        return self.battery.refill_value(
            price, charging_kw=flow.pv_to_battery + flow.grid_to_battery
        )

    def displaced_value(self, price: float, flow: SlotFlow | None = None) -> float:
        # ``_value.price_flows``: at low power where no one knows the power.
        if flow is None:
            return self.battery.displaced_value(price)
        return self.battery.displaced_value(price, discharging_kw=flow.battery_out)

    def dead_band(self, stored_value: float, flow: SlotFlow) -> tuple[float, float]:
        # ``_value.price_flows``, ``_value.value_energy`` and ``_supply.supply_stack``.
        return self.battery.dead_band(
            stored_value,
            charging_kw=flow.pv_to_battery + flow.grid_to_battery,
            discharging_kw=flow.battery_out,
        )

    def stored_per_kwh(self, source: ChargeSource, flow: SlotFlow | None = None) -> float:
        # Today's expression: one conversion both sources share, averaged over the charge
        # (at the battery's limit where nothing charges).
        _on_the_ac_bus(source)
        battery = self.battery
        charging = 0.0 if flow is None else flow.pv_to_battery + flow.grid_to_battery
        if charging <= 1e-9:
            charging = battery.charge_kw
        return battery.stored_by(charging) / charging


def _on_the_ac_bus(source: ChargeSource) -> None:
    """Raise for a path today's branch does not have: PV on the DC bus."""
    if source == ChargeSource.DC_PV:
        raise ValueError("this plant has no DC-coupled PV")


def _todays(config: PlantConfig) -> bool:
    """Whether ``config`` is one of today's: AC-coupled PV without ratings (or none),
    and a battery on its own inverter with no hybrid, or folded on a hybrid's DC bus
    with nothing else there, without DC limits or an AC rating. Its
    battery answers as :class:`PlantStorage`, and its meters split by ``allocate``."""
    if any(
        array.coupling != "ac" or array.dc_rating_kw is not None or array.ac_rating_kw is not None
        for array in config.pv
    ):
        return False
    part, inverter = config.battery, config.battery_inverter
    if part is None or inverter is None or inverter.ac_rating_kw is not None:
        return False
    dispatch = config.dispatch
    if len(dispatch.grid_charge) > 1 or dispatch.charge_order != "pv_first":
        return False  # several windows, or the target first: the general dispatch
    if dispatch.export_windows:
        return False  # battery export: the general dispatch
    if config.grid.import_limit_kw is not None:
        return False  # an import limit: the general dispatch
    if any(load.connection == "dc_bus" for load in config.loads) or dispatch.hold_rules:
        return False  # a DC-bus load, or a hold rule on one: the general dispatch
    if part.connection == "ac":
        return config.hybrid is None
    return part.form == "folded" and part.dc_charge_kw is None and part.dc_discharge_kw is None


def allocate(
    pv_kw: float,
    grid_kw: float,
    battery_kw: float,
    consumed_kw: float,
    *,
    battery: Battery,
    at: Literal["dc", "ac"] = "dc",
) -> Flows:
    """Split metered flows between sources and sinks, all AC.

    Signs as the plant reports them: grid > 0 importing, battery > 0 charging.
    The plant's battery power is measured at the battery, DC side (``microgrid
    fit-battery`` fits the meter against the SOC and the inverter's own AC meter), so
    it is converted first (``Battery.charge_ac``, ``discharge_ac``). Export beyond the
    battery's AC output is PV; PV serves the house before the battery, and the battery
    takes the PV left before the grid (the plant's native order, as measured on its
    meters). A battery metered at its AC side (``at`` ``"ac"``) is not converted.
    """
    pv = max(pv_kw, 0.0)
    if at == "dc":
        charge = battery.charge_ac(max(battery_kw, 0.0))
        discharge = max(battery.discharge_ac(max(-battery_kw, 0.0)), 0.0)
    else:
        charge, discharge = max(battery_kw, 0.0), max(-battery_kw, 0.0)
    exported = max(-grid_kw, 0.0)
    pv_export = min(pv, max(exported - discharge, 0.0))
    battery_export = min(discharge, max(exported - pv_export, 0.0))
    consumed = max(consumed_kw, 0.0)
    pv_to_load = min(pv - pv_export, consumed)
    battery_to_load = min(discharge - battery_export, consumed - pv_to_load)
    pv_to_battery = min(charge, max(pv - pv_export - pv_to_load, 0.0))
    return Flows(
        consumed=consumed,
        pv_to_load=pv_to_load,
        battery_to_load=battery_to_load,
        grid_to_load=max(consumed - pv_to_load - battery_to_load, 0.0),
        pv_to_battery=pv_to_battery,
        grid_to_battery=charge - pv_to_battery,
        pv_export=pv_export,
        battery_export=battery_export,
    )


def consumption_kw(
    pv_kw: float,
    grid_kw: float,
    battery_kw: float,
    *,
    battery: Battery,
    at: Literal["dc", "ac"] = "dc",
) -> float:
    """Everything the site consumed, AC, from the energy balance (kW).

    PV plus net import, less what went into the battery and plus what came out
    of it, both converted from the DC battery meter (:func:`allocate`): the
    house, loads outside its meter (a DC-bus load) and the inverter's own
    draw together. A battery metered at its AC side (``at`` ``"ac"``) is not
    converted.
    """
    if at == "dc":
        charged = battery.charge_ac(max(battery_kw, 0.0))
        delivered = max(battery.discharge_ac(max(-battery_kw, 0.0)), 0.0)
    else:
        charged, delivered = max(battery_kw, 0.0), max(-battery_kw, 0.0)
    return max(max(pv_kw, 0.0) + grid_kw - charged + delivered, 0.0)
