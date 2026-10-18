"""One slot's metered flows, split between sources and sinks.

The split's type, :class:`~microgrid.plant.flows.Flows`, is neutral; the split itself
reads the plant's meters through its conversion, so it stays with the plant.

Every plant with a battery or a hybrid is split by :func:`split_paths`, from the meters
its configuration names (``PlantConfig.meter_set``), for the live import test and the
battery ledger (``Plant.read_meters``). The earlier AC-coupled split (``allocate``) is
kept in the engine's tests as an oracle.

Pure: no I/O.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from microgrid.plant._paths import Paths
from microgrid.plant.flows import Flows, SlotDispatch, SlotFlow


@dataclass(frozen=True, slots=True)
class Metered:
    """A reading by what each meter measures, each in the plant's convention (PV made,
    importing, charging, drawing positive): what a split needs."""

    #: PV on the AC bus (AC arrays, after their inverters) and on the DC bus (DC).
    pv_ac: float
    pv_dc: float
    #: The grid's meter; None where consumption is metered instead (one of the two closes
    #: the balance, which gives the other).
    grid: float | None
    #: The battery's meter, and its side (``Meter.at``).
    battery: float
    battery_at: Literal["dc", "ac"]
    #: The AC side's consumption where metered in place of the grid; None: the balance's.
    consumption: float | None
    #: The DC-bus loads' draw, DC.
    dc_load: float


def split_paths(paths: Paths, m: Metered) -> SlotFlow:
    """A plant with a DC bus's meters split between sources and sinks in the general
    dispatch's order (``_paths``), step for step, so a metered slot
    reads as the projection of the same flows would:

    1. AC PV serves the AC side's consumption; DC PV the DC-bus loads.
    2. DC PV left serves the AC side through the hybrid; AC PV left the DC-bus loads.
    3. PV left charges the battery: on the DC bus, DC PV first, then AC PV through the
       hybrid; on the AC bus, AC PV first, then DC PV through the hybrid.
    4. PV still left is exported.
    5. The battery serves: on the DC bus the DC-bus loads as they are, then across;
       on its own inverter the AC side, then the DC-bus loads through the hybrid. What
       is left of it is exported.
    6. The grid serves the DC-bus loads through the hybrid, then the battery's charge
       left (netted with them, the loads' AC is booked before the charge, as the
       dispatch books it), then the AC side.

    The crossing is the DC bus's net, as metered. Consumption, unmetered, is the
    balance. A battery on its own inverter metered at its DC side is converted by its
    law. What the meters show is within the
    hybrid's rating, so no rating applies. The times and SOCs are the caller's; the
    losses and draws are left at 0, as a metered slot's are (``SlotFlow``)."""
    curve, port, on_bus, rating = paths.curve, paths.port, paths.on_bus, paths.rating
    a, d = max(m.pv_ac, 0.0), max(m.pv_dc, 0.0)
    load_dc = max(m.dc_load, 0.0)
    charge = discharge = 0.0
    if port is not None:
        b = m.battery
        if port.kind == "ac" and m.battery_at == "dc":
            charge = port.battery.charge_ac(max(b, 0.0))
            discharge = max(port.battery.discharge_ac(max(-b, 0.0)), 0.0)
        else:
            charge, discharge = max(b, 0.0), max(-b, 0.0)

    def ac(x: float) -> float:
        return curve.ac(x) if curve is not None else 0.0

    # The AC side's consumption: the balance's from the grid's meter, or metered in its
    # place (then the balance gives the grid, and the split books its import and
    # export).
    if m.grid is not None:
        net = d - load_dc + (discharge - charge if on_bus else 0.0)
        own = 0.0 if on_bus else discharge - charge
        consumed = max(a + m.grid + ac(net) + own, 0.0)
    else:
        consumed = max(m.consumption or 0.0, 0.0)
    x = 0.0
    # 1.
    pv_to_house = min(a, consumed)
    a_left, short = a - pv_to_house, consumed - pv_to_house
    dc_pv_to_dc_load = min(d, load_dc)
    d_left, dl_short = d - dc_pv_to_dc_load, load_dc - dc_pv_to_dc_load
    dc_pv_to_ac = ac_to_dc = 0.0
    pv_to_dc = 0.0  # AC PV the hybrid takes for the DC-bus loads
    # 2.
    if curve is not None and d_left > 0 and short > 0:
        give = max(0.0, min(short, rating, curve.ac(min(d_left, curve.peak))))
        used = min(curve.dc(give), d_left)
        x += used
        dc_pv_to_ac += used
        pv_to_house += give
        d_left -= used
        short -= give
    if curve is not None and a_left > 0 and dl_short > 0:
        take = max(0.0, min(dl_short, x - _floor(paths, x, a_left)))
        used_ac = curve.ac(x) - curve.ac(x - take)
        x -= take
        ac_to_dc += take
        pv_to_dc += used_ac
        a_left = max(0.0, a_left - used_ac)
        dl_short -= take
    # 3.
    charge_left = charge
    dc_pv_to_battery = pv_to_battery = 0.0
    if on_bus and curve is not None:
        dc_pv_to_battery = min(d_left, charge_left)
        d_left -= dc_pv_to_battery
        charge_left -= dc_pv_to_battery
        if a_left > 0 and charge_left > 0:
            more = max(0.0, min(charge_left, x - _floor(paths, x, a_left)))
            pv_to_battery = curve.ac(x) - curve.ac(x - more)
            x -= more
            a_left = max(0.0, a_left - pv_to_battery)
            charge_left -= more
    else:
        pv_to_battery = min(a_left, charge_left)
        a_left -= pv_to_battery
        charge_left -= pv_to_battery
        if curve is not None and d_left > 0 and charge_left > 0:
            now = curve.ac(x)
            top = curve.ac(min(x + d_left, curve.peak)) - now
            give = max(0.0, min(charge_left, rating - now, top))
            used = min(max(0.0, curve.dc(now + give) - x), d_left)
            x += used
            dc_pv_to_ac += used
            d_left -= used
            pv_to_battery += give
            charge_left -= give
    # 4.
    pv_export = a_left
    if curve is not None and d_left > 0:
        pv_export += curve.ac(x + d_left) - curve.ac(x)
        x += d_left
        dc_pv_to_ac += d_left
    # 5.
    battery_to_dc_load = battery_to_load = battery_export = 0.0
    if discharge > 0:
        if on_bus and curve is not None:
            battery_to_dc_load = min(discharge, dl_short)
            dl_short -= battery_to_dc_load
            across = discharge - battery_to_dc_load
            out = curve.ac(x + across) - curve.ac(x)
            x += across
            battery_to_load = min(out, short)
            short -= battery_to_load
            battery_export = out - battery_to_load
        else:
            battery_to_load = min(discharge, short)
            short -= battery_to_load
            rest = discharge - battery_to_load
            if curve is not None and rest > 0 and dl_short > 0:
                take = max(0.0, min(dl_short, x - _floor(paths, x, rest)))
                used_ac = curve.ac(x) - curve.ac(x - take)
                x -= take
                ac_to_dc += take
                dl_short -= take
                battery_to_load += used_ac
                rest -= used_ac
            battery_export = rest
    # 6.
    grid_to_dc = grid_to_battery = 0.0
    if curve is not None and dl_short > 0:
        grid_to_dc = curve.ac(x) - curve.ac(x - dl_short)
        x -= dl_short
        ac_to_dc += dl_short
    if charge_left > 0:
        if on_bus and curve is not None:
            grid_to_battery = curve.ac(x) - curve.ac(x - charge_left)
            x -= charge_left
        else:
            grid_to_battery = charge_left
    return SlotFlow(
        start=_NOWHERE,
        soc_start=None,
        soc_end=None,
        pv_to_load=pv_to_house + pv_to_dc,
        pv_to_battery=pv_to_battery,
        pv_export=pv_export,
        battery_to_load=battery_to_load,
        grid_to_load=short + grid_to_dc,
        grid_to_battery=grid_to_battery,
        dc_pv_to_battery=dc_pv_to_battery,
        dc_pv_to_ac=dc_pv_to_ac,
        dc_pv_to_dc_load=dc_pv_to_dc_load,
        battery_to_dc_load=battery_to_dc_load,
        ac_to_dc_load=ac_to_dc,
        dispatch=SlotDispatch(
            crossing=x,
            battery_port=charge - discharge,
            battery_port_in=charge,
            battery_port_out=discharge,
        ),
        # The export the meters see stays with the battery's AC: a metered slot leaves
        # it at 0 (``Plant.metered_flow``), and :func:`flows_of` books it.
        battery_export=battery_export,
    )


def _floor(paths: Paths, x: float, ac_kw: float) -> float:
    """The lowest crossing the hybrid can rectify to from ``x`` taking ``ac_kw`` AC, as
    the dispatch bounds it: no more than that AC, nor beyond its rating."""
    curve = paths.curve
    assert curve is not None  # a crossing needs the hybrid
    floor = curve.dc(curve.ac(x) - ac_kw)
    if not math.isinf(paths.rating):
        floor = max(floor, curve.dc(-paths.rating))
    return floor


#: A split's start until the caller places it.
_NOWHERE = datetime(2000, 1, 1, tzinfo=UTC)


def flows_of(flow: SlotFlow) -> Flows:
    """A split's sources and sinks (:class:`Flows`)."""
    return Flows(
        consumed=flow.pv_to_load + flow.battery_to_load + flow.grid_to_load,
        pv_to_load=flow.pv_to_load,
        battery_to_load=flow.battery_to_load,
        grid_to_load=flow.grid_to_load,
        pv_to_battery=flow.pv_to_battery,
        grid_to_battery=flow.grid_to_battery,
        pv_export=flow.pv_export,
        battery_export=flow.battery_export,
        dc_pv_to_battery=flow.dc_pv_to_battery,
        battery_to_dc_load=flow.battery_to_dc_load,
    )
