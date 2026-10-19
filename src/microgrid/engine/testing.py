"""For a client's own plant model: the contract every plant model keeps.

:func:`assert_plant_model_contract` runs a plant model (``extend.PlantModel``) over a
few synthetic days and raises :class:`AssertionError` where it breaks what the port
promises. It needs no test framework; call it from any.

It also runs the stored cost over the model (``Storage.stored_per_kwh``), the price a
client's own model gets by default.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta, timezone
from itertools import pairwise

from microgrid.engine._stored import Amount, Lots, project_average
from microgrid.engine._timeline import SLOT
from microgrid.plant import Plant
from microgrid.plant.flows import SlotFlow
from microgrid.plant.port import ChargeSource, PlantModel, Storage

__all__ = ["assert_plant_model_contract"]

_T0 = datetime(2026, 9, 25, 7, 0, tzinfo=UTC)
_HOURS = SLOT / timedelta(hours=1)
#: A zone that is not UTC: the plant model must refuse a time in it.
_ELSEWHERE = timezone(timedelta(hours=-7))
#: The paths of a DC bus, a battery's export and unserved load, each 0 on a plant without.
_PATHS = (
    "dc_pv_to_battery",
    "dc_pv_to_ac",
    "dc_pv_to_dc_load",
    "battery_to_dc_load",
    "battery_export",
    "ac_to_dc_load",
    "unserved",
)


def _check(condition: bool, what: str) -> None:
    if not condition:
        raise AssertionError(what)


def _close(a: float, b: float, what: str, *, tolerance: float = 1e-6) -> None:
    _check(abs(a - b) <= tolerance, f"{what}: {a} != {b}")


def _raises(call: Callable[[], object], match: str, what: str) -> None:
    try:
        call()
    except ValueError as error:
        _check(re.search(match, str(error)) is not None, f"{what}: {error}")
        return
    raise AssertionError(f"{what}: no ValueError")


def _day(pv_peak: float, load: float, slots: int = 144) -> tuple[list[float], list[float]]:
    pv = [max(0.0, pv_peak * (1 - abs(k - 78) / 36)) for k in range(slots)]
    return pv, [load] * slots


def _imbalance(flow: SlotFlow, pv: float, load: float, capacity: float, hours: float) -> float:
    """Sources less sinks in ``flow``'s slot, kW, from its fields and the slot's inputs,
    ``pv`` and ``load`` (``SlotFlow``'s *Balance*): 0 where the energy balances."""
    stored = 0.0  # without a battery nothing is stored
    if flow.soc_end is not None and flow.soc_start is not None:
        stored = (flow.soc_end - flow.soc_start) * capacity / hours
    sources = pv + flow.grid_to_load + flow.grid_to_battery
    sinks = (
        load
        - flow.unserved
        + flow.inverter_draw
        + flow.pv_export
        + flow.battery_export
        + flow.pv_curtail
        + flow.pv_clipped
        + flow.conversion_loss
        + stored
    )
    return sources - sinks


#: The prices the stored cost is checked at: an import and an export, per kWh.
_IMPORT, _EXPORT = 0.30, 0.05


def _stored_cost(storage: Storage, flows: Sequence[SlotFlow]) -> None:
    """The stored cost over ``flows`` (``project_average``, the price's default rule):
    each charge converts by a stored kWh per kWh in (0, 1.5] (a battery meter's factor
    is at most 1.5, ``Battery``), and the running average lies between nothing and the
    dearest kWh charged, as bought where nothing is known."""
    pers = [storage.stored_per_kwh(ChargeSource.GRID)]
    for flow in flows:
        for source, kw in (
            (ChargeSource.PV, flow.pv_to_battery),
            (ChargeSource.GRID, flow.grid_to_battery),
            (ChargeSource.DC_PV, flow.dc_pv_to_battery),
        ):
            if kw > 1e-9:
                pers.append(storage.stored_per_kwh(source, flow))
    _check(all(0 < per <= 1.5 for per in pers), "a stored kWh per kWh charged, in (0, 1.5]")
    count = len(flows)
    dearest = _IMPORT / min(pers)
    for lots in (None, Lots(_T0, {}, Amount(1.0, 0.1))):
        costs = project_average(
            storage, flows, [_HOURS] * count, [_IMPORT] * count, [_EXPORT] * count, lots=lots
        )
        _check(len(costs) == count, "a stored cost per slot")
        _check(
            all(cost is None or 0 <= cost <= dearest + 1e-9 for cost in costs),
            "the stored cost between nothing and the dearest kWh charged",
        )
        first = flows[0].soc_start
        if lots is not None and first is not None and first * storage.capacity_kwh > 1e-6:
            _close(costs[0] or 0.0, 0.1, "the stored cost starts at the lots' own")


def assert_plant_model_contract(plant: PlantModel) -> None:
    """What every plant model keeps, whatever its topology, with a battery or without,
    with PV or without. Raises :class:`AssertionError` naming what broke.

    Only what the port promises: no conversion (a DC array loses some PV on its way
    to an AC load) and no dispatch (more load can raise a dynamic top-up target, so
    the battery need not end lower); and the stored cost, the price's default rule,
    over its projections (``Storage.stored_per_kwh``)."""
    storage = plant.storage
    if storage is not None:
        _check(storage.capacity_kwh > 0, "capacity")
        _check(0 <= storage.reserve_soc < storage.saturation_soc <= 1, "reserve, saturation")
    capacity = storage.capacity_kwh if storage is not None else 0.0
    for pv_peak, load, start_soc in ((0.0, 0.7, 0.5), (6.0, 0.7, 0.2), (9.0, 2.5, 0.95)):
        pv, house = _day(pv_peak if plant.has_pv else 0.0, load)
        soc = start_soc if storage is not None else None
        starts = [_T0 + k * SLOT for k in range(len(pv))]
        durations = [SLOT] * len(pv)
        # A DC-bus load, where the plant has one, drawing every third slot.
        ev = [3.0 if k % 3 == 0 and plant.has_dc_loads else 0.0 for k in range(len(pv))]
        flows = plant.project(starts, durations, pv, house, soc, dc_load_kw=ev)
        # Every slot it was given, in order, the SOC carried from one to the next.
        _check([f.start for f in flows] == starts, "every slot, in order")
        first = flows[0].soc_start
        if soc is None:
            _check(first is None, "no SOC without a battery")
        else:
            _check(first is not None and abs(first - soc) <= 1e-6 * soc, "the SOC it was given")
        for before, after in pairwise(flows):
            _check(after.soc_start == before.soc_end, "the SOC carried")
        for flow, sun, need, dc_need in zip(flows, pv, house, ev, strict=True):
            if storage is None:
                # No battery: no SOC, and nothing flows to or from one.
                _check(flow.soc_end is None, "no SOC without a battery")
                _check(flow.moving == 0.0, "nothing moves without a battery")
            else:
                _check(flow.soc_end is not None, "an SOC")
                assert flow.soc_end is not None
                _check(-1e-9 <= flow.soc_end <= 1 + 1e-9, "the SOC in 0-1")
            if not plant.has_pv:
                _check(flow.pv_to_load + flow.pv_spare + flow.pv_clipped == 0.0, "no PV")
            for name in (
                "pv_to_load",
                "pv_to_battery",
                "pv_export",
                "pv_curtail",
                "pv_clipped",
                "battery_to_load",
                "grid_to_load",
                "grid_to_battery",
                *_PATHS,
                "conversion_loss",
                "inverter_draw",
            ):
                _check(getattr(flow, name) >= -1e-9, f"{name} not negative")
            # No more PV goes anywhere than there was; the loads are served, plus the
            # plant's own draw, or what is not is unserved (the AC fields hold what the
            # hybrid takes for the DC-bus loads, at least what it delivers them).
            routed = flow.pv_to_load + flow.pv_spare + flow.pv_clipped
            _check(routed <= sun + 1e-6, "no more PV routed than there was")
            served = flow.pv_to_load + flow.battery_to_load + flow.grid_to_load
            served += flow.dc_pv_to_dc_load + flow.battery_to_dc_load
            _check(served + flow.unserved >= need + dc_need - 1e-6, "the loads served")
            _check(
                flow.dc_pv_to_dc_load + flow.battery_to_dc_load + flow.ac_to_dc_load
                <= dc_need + 1e-9,
                "no more to the DC-bus loads than they draw",
            )
            imbalance = _imbalance(flow, sun, need + dc_need, capacity, _HOURS)
            _close(imbalance, 0.0, "the slot's energy balances")
            if storage is not None:
                _check(storage.headroom(flow, _HOURS) >= 0, "headroom")
                _check(storage.divertible_kw(flow) >= 0, "divertible")
                low, high = storage.dead_band(0.3, flow)
                _check(low <= high + 1e-12, "the dead band")
        if storage is not None:
            _stored_cost(storage, flows)
        # A what-if is the same call with more load: as long, and that load served.
        heavier = [kw + 1.0 for kw in house]
        more = plant.project(starts, durations, pv, heavier, soc, dc_load_kw=ev)
        _check([f.start for f in more] == starts, "a what-if as long")
        for flow, need in zip(more, heavier, strict=True):
            served = flow.pv_to_load + flow.battery_to_load + flow.grid_to_load
            _check(served >= need - 1e-6, "a what-if's load served")
        # ``stop`` ends a projection early.
        stopped = plant.project(
            starts, durations, pv, house, soc, dc_load_kw=ev, stop=lambda i, _: i == 2
        )
        _check(len(stopped) == 3 and tuple(stopped) == tuple(flows[:3]), "stop")
    # PV per array: named as the plant's arrays, each its own series.
    _check(plant.has_pv is bool(plant.pv_arrays), "PV, and its arrays")
    if plant.pv_arrays:
        pv, house = _day(4.0, 0.7)
        starts = [_T0 + k * SLOT for k in range(len(pv))]
        share = 1.0 / len(plant.pv_arrays)
        per_array = {name: [kw * share for kw in pv] for name in plant.pv_arrays}
        soc = 0.5 if storage is not None else None
        flows = plant.project(starts, [SLOT] * len(pv), per_array, house, soc)
        _check([f.start for f in flows] == starts, "PV per array, every slot")
        _raises(
            lambda: plant.project(starts, [SLOT] * len(pv), {"nowhere": pv}, house, soc),
            "must name exactly",
            "an array the plant does not have",
        )
    # Its meters, by its configuration: each a value of its kind.
    soc_now = 0.6 if storage is not None else None
    pv_now = 3.0 if plant.has_pv else 0.0
    pv_meters = [name for name, kind in plant.meters.items() if kind == "pv"]
    by_kind = {"grid": -0.5, "battery": 1.2, "consumption": 1.0, "dc_load": 0.5}
    reading: dict[str, float | None] = {
        name: pv_now / len(pv_meters) if kind == "pv" else by_kind[kind]
        for name, kind in plant.meters.items()
    }
    now = plant.metered_flow(_T0, soc_now, reading)
    _check(now is not None, "a metered flow")
    assert now is not None
    _check((now.start, now.soc_start, now.soc_end) == (_T0, soc_now, soc_now), "its slot")
    _raises(
        lambda: plant.metered_flow(_T0.astimezone(_ELSEWHERE), soc_now, reading),
        "UTC",
        "a time not in UTC",
    )
    meters = plant.read_meters(reading)
    _check(meters is not None, "the meters read")
    assert meters is not None
    # The PV metered is all routed: AC PV as it is; a DC array's crosses with its
    # loss, so no more than was metered.
    routed = meters.pv_to_load + meters.pv_to_battery + meters.pv_export + meters.dc_pv_to_battery
    if isinstance(plant, Plant) and all(a.coupling == "ac" for a in plant.config.pv):
        _close(routed, pv_now, "AC PV metered, all routed")
    else:
        _check(routed <= pv_now + 1e-9, "no more PV routed than metered")
    for value in (
        meters.consumed,
        meters.pv_to_load,
        meters.battery_to_load,
        meters.grid_to_load,
        meters.pv_to_battery,
        meters.grid_to_battery,
        meters.pv_export,
        meters.battery_export,
    ):
        _check(value >= -1e-9, "a metered flow not negative")
    total = pv_now if plant.has_pv else 0.0
    measured = plant.pv_from_meters(reading, per_array=False)
    if plant.pv_per_array:
        _check(measured is None, "no one total")
        per = plant.pv_from_meters(reading, per_array=True)
        _check(isinstance(per, Mapping) and set(per) == set(plant.pv_arrays), "PV per array")
    elif plant.has_pv:
        _check(isinstance(measured, Sequence), "the PV metered")
        assert isinstance(measured, Sequence)
        _close(measured[0], total, "the PV metered")
    # A meter unknown leaves the split unknown; a name the plant does not meter raises.
    _check(plant.read_meters({**reading, "grid": None}) is None, "a meter unknown")
    without_grid = {k: v for k, v in reading.items() if k != "grid"}
    _check(plant.metered_flow(_T0, soc_now, without_grid) is None, "a meter missing")
    _raises(
        lambda: plant.read_meters({**reading, "elsewhere": 1.0}),
        "not the plant's meters",
        "a meter the plant does not have",
    )
