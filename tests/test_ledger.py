"""The battery's lots (docs/methodology/lots.md): ``microgrid.engine.ledger``."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from engine_fixtures import plant_of
from legacy_dispatch import NativePlant
from microgrid.engine.extend import Flows
from microgrid.engine.ledger import (
    Amount,
    Ledger,
    Source,
    floor_per_kwh,
    integrate,
    run_ledger,
    soc_boundary,
)
from microgrid.engine.series import SLOT, Series, alive
from microgrid.plant import Battery, Load, Meter, Plant

T0 = datetime(2026, 9, 1, 7, 0, tzinfo=UTC)
#: 90 % each way, so the numbers read easily.
BATTERY = Battery(
    capacity_kwh=10.0, charge_kw=5.0, discharge_kw=5.0, eta_charge=0.9, eta_discharge=0.9
)
HOUR = 1.0


def _is(amount: Amount, kwh: float, value: float) -> bool:
    return amount.kwh == pytest.approx(kwh) and amount.value == pytest.approx(value)


def _flows(**kw: float) -> Flows:
    values = dict.fromkeys(
        (
            "consumed",
            "pv_to_load",
            "battery_to_load",
            "grid_to_load",
            "pv_to_battery",
            "grid_to_battery",
            "pv_export",
            "battery_export",
        ),
        0.0,
    )
    values.update(kw)
    return Flows(**values)


LOSSLESS = Battery(
    capacity_kwh=10.0, charge_kw=5.0, discharge_kw=5.0, eta_charge=1.0, eta_discharge=1.0
)
#: The ledger reads meters through a plant model: this site's, with that battery.
LOSSLESS_PLANT = plant_of(NativePlant(LOSSLESS, ZoneInfo("Europe/Berlin")))
#: That battery folded on a hybrid's DC bus with a load beside it.
_BUS = plant_of(NativePlant(LOSSLESS, ZoneInfo("Europe/Berlin")), connection="dc_bus").config
DC_PLANT = Plant(
    replace(
        _BUS,
        loads=(*_BUS.loads, Load("ev", "dc_bus")),
        battery=replace(_BUS.battery, dc_charge_kw=5.0, dc_discharge_kw=5.0)
        if _BUS.battery
        else None,
        hybrid=replace(_BUS.hybrid, ac_rating_kw=10.0) if _BUS.hybrid else None,
    )
)


Samples = list[tuple[datetime, float]]


def _integrate_by_kind(
    starts: list[datetime],
    step: timedelta,
    pv: Samples,
    grid: Samples,
    battery: Samples,
    beats: list[datetime],
    *,
    alive_limit: timedelta,
    plant: Plant,
    dc_load: Samples | None = None,
) -> list[Flows | None]:
    """:func:`integrate` over samples given by what each meter measures: a client's
    mapping of its meters onto the plant's (the plant's own names here)."""
    end = starts[-1] + step if starts else T0
    times = [at for one in (pv, grid, battery, dc_load or []) for at, _ in one]
    known = max([end, *times, *beats])
    by_kind = {"pv": pv, "grid": grid, "battery": battery, "dc_load": dc_load}
    names = {kind: name for name, kind in plant.meters.items()}
    # Each meter given, under the plant's name for it (or its own, which the plant
    # then refuses): a meter missing, or one too many, is the integration's to catch.
    series = {
        names.get(kind, kind): Series.of(samples, known_until=known)
        for kind, samples in by_kind.items()
        if samples is not None
    }
    beat = Series.of(((at, 1.0) for at in beats), known_until=known)
    return integrate(series, starts, step, plant=plant, alive_spans=alive(beat, alive_limit))


#: The 90 % battery on its own inverter.
BATTERY_PLANT = plant_of(NativePlant(BATTERY, ZoneInfo("Europe/Berlin")))


def _split(plant: Plant, pv_kw: float, grid_kw: float, battery_kw: float) -> Flows:
    """One reading of ``plant``'s meters, split (``Plant.read_meters``)."""
    flows = plant.read_meters({"pv": pv_kw, "grid": grid_kw, "battery": battery_kw})
    assert flows is not None
    return flows


def test_allocation_follows_the_plants_order() -> None:
    sunny = _split(LOSSLESS_PLANT, 5.0, -1.0, 3.0)
    assert (sunny.pv_to_load, sunny.pv_to_battery, sunny.pv_export) == (1.0, 3.0, 1.0)
    assert sunny.grid_to_battery == 0.0 and sunny.battery_export == 0.0
    evening_export = _split(LOSSLESS_PLANT, 0.0, -3.0, -4.0)
    assert evening_export.battery_export == 3.0 and evening_export.battery_to_load == 1.0
    assert evening_export.pv_export == 0.0 and evening_export.grid_to_load == 0.0
    grid_charge = _split(LOSSLESS_PLANT, 1.0, 6.0, 5.0)
    assert (grid_charge.pv_to_load, grid_charge.grid_to_load) == (1.0, 1.0)
    assert (grid_charge.pv_to_battery, grid_charge.grid_to_battery) == (0.0, 5.0)
    night = _split(LOSSLESS_PLANT, 0.0, 0.2, -0.5)
    assert night.battery_to_load == 0.5 and night.grid_to_load == pytest.approx(0.2)


def test_a_reading_that_does_not_balance_books_pv_to_the_battery_first() -> None:
    # The grid exporting more than PV while the battery charges (sensor timing) is
    # booked as the general dispatch orders it, PV charging the battery before any is
    # exported, so no grid lot; today's split (``allocate``) booked all the PV to export
    # and the charge to the grid.
    flows = _split(LOSSLESS_PLANT, 5.0, -6.0, 2.0)
    assert flows.pv_to_battery == pytest.approx(2.0)
    assert flows.grid_to_battery == 0.0
    assert flows.pv_export == pytest.approx(3.0)
    assert flows.consumed == 0.0


def test_the_battery_meter_is_dc_side_so_allocation_converts_it() -> None:
    """Charging 1.8 kW at the battery took 2 kW of AC; discharging 1 kW delivers 0.9."""
    charging = _split(BATTERY_PLANT, 3.0, 0.0, 1.8)
    assert charging.pv_to_battery == pytest.approx(2.0) and charging.grid_to_battery == 0.0
    ledger = Ledger(BATTERY)
    slot = ledger.step(T0, charging, HOUR, pv_price=0.09, grid_price=0.4, stored_after=None)
    assert slot.held[Source.PV].kwh == pytest.approx(1.8)  # what the battery meter saw stored
    discharging = _split(BATTERY_PLANT, 0.0, 0.1, -1.0)
    assert discharging.battery_to_load == pytest.approx(0.9)
    assert discharging.grid_to_load == pytest.approx(0.1)


def test_consumption_is_the_energy_balance_through_the_dc_meter() -> None:
    def consumed(pv_kw: float, grid_kw: float, battery_kw: float) -> float:
        return _split(BATTERY_PLANT, pv_kw, grid_kw, battery_kw).consumed

    assert consumed(3.0, -0.5, 1.8) == pytest.approx(0.5)  # 3 - 0.5 - 2
    assert consumed(0.0, 0.1, -1.0) == pytest.approx(1.0)  # 0.1 + 0.9
    assert consumed(0.0, -1.0, 0.0) == 0.0  # never negative


def test_a_lots_basis_is_its_price_over_the_charge_efficiency() -> None:
    """A PV lot's basis is its opportunity cost at charge time divided by η_c."""
    ledger = Ledger(BATTERY)
    slot = ledger.step(
        T0, _flows(pv_to_battery=2.0), HOUR, pv_price=0.09, grid_price=0.4, stored_after=None
    )
    assert _is(slot.charged[Source.PV], 1.8, 0.18)
    assert slot.held[Source.PV].unit == pytest.approx(0.1)
    # Delivering 0.81 AC kWh draws 0.9 stored: the conversion loss is part of the cost.
    slot = ledger.step(
        T0, _flows(battery_to_load=0.81), HOUR, pv_price=0.09, grid_price=0.4, stored_after=None
    )
    assert _is(slot.to_load[Source.PV], 0.9, 0.09)


def test_the_house_takes_grid_lots_first_and_exports_take_pv_lots_first() -> None:
    ledger = Ledger(BATTERY, unknown_kwh=0.5)
    ledger.step(
        T0,
        _flows(pv_to_battery=2.0, grid_to_battery=2.0),
        HOUR,
        pv_price=0.1,
        grid_price=0.4,
        stored_after=None,
    )
    load = ledger.step(
        T0, _flows(battery_to_load=0.9), HOUR, pv_price=0.1, grid_price=0.4, stored_after=None
    )
    assert load.to_load[Source.UNKNOWN].kwh == pytest.approx(0.5)  # unknown origin goes first
    assert load.to_load[Source.GRID].kwh == pytest.approx(0.5) and Source.PV not in load.to_load
    export = ledger.step(
        T0, _flows(battery_export=0.9), HOUR, pv_price=0.1, grid_price=0.4, stored_after=None
    )
    assert (
        export.to_export[Source.PV].kwh == pytest.approx(1.0)
        and Source.GRID not in export.to_export
    )


def test_the_meter_wins() -> None:
    ledger = Ledger(BATTERY)
    ledger.step(
        T0,
        _flows(pv_to_battery=2.0, grid_to_battery=2.0),
        HOUR,
        pv_price=0.1,
        grid_price=0.4,
        stored_after=None,
    )
    lower = ledger.step(
        T0, None, HOUR, pv_price=0.1, grid_price=0.4, stored_after=1.8
    )  # half the 3.6
    assert _is(lower.unexplained, -1.8, -(0.2 + 0.8) / 2)
    assert lower.held[Source.PV].kwh == pytest.approx(0.9) and lower.held[
        Source.GRID
    ].kwh == pytest.approx(0.9)
    higher = ledger.step(T0, None, HOUR, pv_price=0.1, grid_price=0.4, stored_after=2.8)
    # Scaled up pro rata at the lots' own basis: no free energy.
    assert _is(higher.unexplained, 1.0, (0.1 + 0.4) * (2.8 / 1.8 - 1))
    assert _is(higher.held[Source.PV], 1.4, 0.1 * 2.8 / 1.8)
    assert _is(higher.held[Source.GRID], 1.4, 0.4 * 2.8 / 1.8)
    assert higher.held[Source.UNKNOWN].kwh == 0.0


def test_delivering_more_than_the_model_holds_scales_the_lots_first() -> None:
    ledger = Ledger(BATTERY)
    ledger.step(
        T0, _flows(pv_to_battery=1.0), HOUR, pv_price=0.09, grid_price=0.4, stored_after=None
    )
    slot = ledger.step(
        T0, _flows(battery_to_load=1.8), HOUR, pv_price=0.09, grid_price=0.4, stored_after=None
    )
    assert _is(slot.unexplained, 1.1, 0.09 * 1.1 / 0.9)  # 0.9 held, 2.0 drawn: PV at its basis
    assert _is(slot.to_load[Source.PV], 2.0, 0.2)


def test_delivering_from_an_empty_model_is_energy_of_unknown_origin() -> None:
    ledger = Ledger(BATTERY)
    slot = ledger.step(
        T0, _flows(battery_to_load=0.9), HOUR, pv_price=0.1, grid_price=0.4, stored_after=None
    )
    assert _is(slot.unexplained, 1.0, 0.0)
    assert _is(slot.to_load[Source.UNKNOWN], 1.0, 0.0)


def test_run_ledger_checks_lengths() -> None:
    with pytest.raises(ValueError, match="same length"):
        run_ledger(BATTERY, [T0], [None, None], HOUR, [0.1], [0.4], [None])
    slots = run_ledger(
        BATTERY,
        [T0, T0 + timedelta(hours=1)],
        [None, None],
        HOUR,
        [0.1] * 2,
        [0.4] * 2,
        [None, 1.0],
    )
    assert slots[-1].held[Source.UNKNOWN].kwh == pytest.approx(1.0)


def test_the_soc_at_a_slot_boundary_is_between_the_two_means() -> None:
    means = [50.0, 40.0, None, 30.0]
    assert soc_boundary(means, 0) == 45.0
    assert soc_boundary(means, 1) == 40.0 and soc_boundary(means, 2) == 30.0
    assert soc_boundary(means, 3) == 30.0  # the last slot: its own mean
    assert soc_boundary([None, None], 0) is None


def test_the_floor_averages_what_a_load_would_draw() -> None:
    """Grid-charged energy before PV; the average basis of the draw per delivered kWh,
    plus wear. A sliver of grid energy must not price a load's whole run (on the
    reference plant, 190 Wh of grid energy once set a 0.42 floor over 9-16 kWh of 0.055
    PV)."""
    held = {
        Source.UNKNOWN: Amount(),
        Source.GRID: Amount(2.0, 0.8),  # 0.40 per stored kWh
        Source.PV: Amount(5.0, 0.5),  # 0.10
    }
    # 0.9 kWh delivered draws 1.0 stored, all grid.
    assert floor_per_kwh(held, BATTERY, draw_kwh=0.9, draw_kw=0.9) == pytest.approx(0.4 / 0.9)
    # 4.5 delivered draws 5.0 stored: 2 grid at 0.40 and 3 PV at 0.10.
    assert floor_per_kwh(held, BATTERY, draw_kwh=4.5, draw_kw=4.5) == pytest.approx(
        (0.8 + 0.3) / 4.5
    )
    sliver = held | {Source.GRID: Amount(0.19, 0.19 * 0.4)}
    assert floor_per_kwh(sliver, BATTERY, draw_kwh=4.5, draw_kw=4.5) == pytest.approx(
        (0.19 * 0.4 + (5.0 - 0.19) * 0.1) / 4.5
    )
    worn = Battery(
        10.0,
        5.0,
        5.0,
        eta_charge=0.9,
        eta_discharge=0.9,
        wear_charge_per_kwh=0.02,
        wear_discharge_per_kwh=0.02,
    )
    # A delivered kWh was charged as 1 / 0.81 AC kWh, and then discharged.
    assert floor_per_kwh(held, worn, draw_kwh=0.9, draw_kw=0.9) == pytest.approx(
        0.4 / 0.9 + 0.02 / 0.81 + 0.02
    )
    empty = dict.fromkeys(Source, Amount())
    assert floor_per_kwh(empty, BATTERY, draw_kwh=1.0, draw_kw=1.0) is None
    # The inverter loses more at power: a 5.1 kW draw costs more per kWh.
    growing = Battery(10.0, 5.0, 6.0, eta_charge=0.9, eta_discharge=0.9, loss_discharge_per_kw=0.01)
    light = floor_per_kwh(held, growing, draw_kwh=0.45, draw_kw=0.45)
    heavy = floor_per_kwh(held, growing, draw_kwh=0.45, draw_kw=5.1)
    assert light is not None and heavy is not None and heavy > light
    eta = 5.1 / growing.drawn_by(5.1)
    assert heavy == pytest.approx(0.4 / eta)
    with pytest.raises(ValueError, match="positive"):
        floor_per_kwh(held, BATTERY, draw_kwh=0.0, draw_kw=0.0)


def test_no_floor_while_energy_of_unknown_origin_would_be_drawn() -> None:
    """One rule for unknown energy: no known cost, so no floor."""
    held = {Source.UNKNOWN: Amount(1.0, 0.0), Source.GRID: Amount(), Source.PV: Amount(5.0, 0.5)}
    assert floor_per_kwh(held, BATTERY, draw_kwh=4.5, draw_kw=4.5) is None
    residue = held | {Source.UNKNOWN: Amount(0.0005, 0.0)}  # float residue: not energy
    assert floor_per_kwh(residue, BATTERY, draw_kwh=4.5, draw_kw=4.5) == pytest.approx(0.1 / 0.9)


def test_the_floor_leaves_the_reserve_alone() -> None:
    """Energy below the reserve cannot be drawn."""
    # 5 kWh with a 20 % reserve keeps 1 kWh back.
    reserved = Battery(5.0, 5.0, 5.0, eta_charge=0.9, eta_discharge=0.9, reserve_soc=0.2)
    held = {Source.UNKNOWN: Amount(), Source.GRID: Amount(1.0, 0.4), Source.PV: Amount(2.0, 0.2)}
    # 3 stored, 2 above the reserve: 1 grid and 1 PV, delivering 1.8.
    assert floor_per_kwh(held, reserved, draw_kwh=4.5, draw_kw=4.5) == pytest.approx(
        (0.4 + 0.1) / 1.8
    )
    at_reserve = {Source.UNKNOWN: Amount(), Source.GRID: Amount(), Source.PV: Amount(1.0, 0.1)}
    assert floor_per_kwh(at_reserve, reserved, draw_kwh=4.5, draw_kw=4.5) is None


MIN = timedelta(minutes=1)


def _at(minutes: float) -> datetime:
    return T0 + timedelta(minutes=minutes)


def test_integration_allocates_at_every_change_not_on_the_mean() -> None:
    """A DC-bus load's session starting mid-slot. The 10-minute net means look like the
    grid charging the battery; at sample resolution nothing charges from the grid."""
    beats = [_at(m) for m in range(0, 11)]
    pv = [(_at(-1), 4.4)]
    battery = [(_at(-1), 3.0), (_at(5), -1.0)]  # charging on PV, then discharging into the load
    grid = [(_at(-1), -0.4), (_at(5), 3.4)]  # exporting a little, then importing for the load
    (flows,) = _integrate_by_kind(
        [T0], SLOT, pv, grid, battery, beats, alive_limit=3 * MIN, plant=LOSSLESS_PLANT
    )
    assert flows is not None
    assert flows.grid_to_battery == pytest.approx(0.0)  # no fake grid lot
    assert flows.pv_to_battery == pytest.approx(1.5)  # 3 kW for half the slot
    means = _split(LOSSLESS_PLANT, 4.4, 1.5, 1.0)
    assert means.grid_to_battery > 0  # what the means would have said
    assert flows.consumed == pytest.approx(0.5 * 1.0 + 0.5 * (4.4 + 3.4 + 1.0))


def test_integration_needs_a_live_path_and_every_value() -> None:
    pv, grid, battery = [(_at(-1), 1.0)], [(_at(-1), 0.0)], [(_at(-1), 0.5)]
    silent = [_at(-1), _at(8)]  # a heartbeat before, then nothing for 9 minutes
    assert _integrate_by_kind(
        [T0], SLOT, pv, grid, battery, silent, alive_limit=3 * MIN, plant=LOSSLESS_PLANT
    ) == [None]
    beats = [_at(m) for m in range(-1, 11)]
    assert _integrate_by_kind(
        [T0], SLOT, [], grid, battery, beats, alive_limit=3 * MIN, plant=LOSSLESS_PLANT
    ) == [None]
    (flows,) = _integrate_by_kind(
        [T0], SLOT, pv, grid, battery, beats, alive_limit=3 * MIN, plant=LOSSLESS_PLANT
    )
    assert flows is not None and flows.pv_to_battery == pytest.approx(0.5)
    assert (
        _integrate_by_kind(
            [], SLOT, pv, grid, battery, beats, alive_limit=MIN, plant=LOSSLESS_PLANT
        )
        == []
    )


def test_the_battery_serving_a_dc_bus_load_draws_at_its_meter() -> None:
    """On its own DC bus the battery converts nothing; a folded battery draws
    ``meter_discharge`` stored kWh per DC kWh it gives, from the lots as load, kept
    apart from the house's AC draw, which converts."""
    metered = Battery(10.0, 5.0, 5.0, eta_charge=0.9, eta_discharge=0.9, meter_discharge=1.02)
    ledger = Ledger(metered)
    ledger.step(
        T0, _flows(pv_to_battery=5.0), HOUR, pv_price=0.09, grid_price=0.4, stored_after=None
    )
    slot = ledger.step(
        T0, _flows(battery_to_dc_load=2.0), HOUR, pv_price=0.09, grid_price=0.4, stored_after=None
    )
    assert slot.to_dc_load[Source.PV].kwh == pytest.approx(2.04)
    assert slot.dc_delivered_per_drawn == pytest.approx(1 / 1.02)
    assert slot.to_load == {} and slot.to_export == {} and slot.delivered_per_drawn == 0.0
    # Both at once: the house's AC draws through the conversion, the DC load at the meter.
    both = ledger.step(
        T0,
        _flows(battery_to_load=0.9, battery_to_dc_load=1.0),
        HOUR,
        pv_price=0.09,
        grid_price=0.4,
        stored_after=None,
    )
    assert sum(a.kwh for a in both.to_load.values()) == pytest.approx(
        0.9 / both.delivered_per_drawn
    )
    assert sum(a.kwh for a in both.to_dc_load.values()) == pytest.approx(1.02)


def test_a_dc_bus_load_beyond_the_lots_scales_them_first() -> None:
    """More drawn, AC and DC together, than the model holds: the lots scale to it."""
    metered = Battery(10.0, 5.0, 5.0, eta_charge=0.9, eta_discharge=0.9, meter_discharge=1.02)
    ledger = Ledger(metered, unknown_kwh=1.0)
    slot = ledger.step(
        T0,
        _flows(battery_to_load=0.9, battery_to_dc_load=1.0),
        HOUR,
        pv_price=0.09,
        grid_price=0.4,
        stored_after=None,
    )
    need = 0.9 / slot.delivered_per_drawn + 1.02
    assert slot.unexplained.kwh == pytest.approx(need - 1.0)
    drawn = sum(a.kwh for a in [*slot.to_load.values(), *slot.to_dc_load.values()])
    assert drawn == pytest.approx(need)


def test_integration_reads_the_dc_bus_loads_meter() -> None:
    """A plant with a DC-bus load reads its meter at every change; a slot without
    it is unknown, and a plant without one takes no DC-bus samples."""
    beats = [_at(m) for m in range(0, 21)]
    pv = [(_at(-1), 0.0)]
    battery = [(_at(-1), -3.0)]  # discharging into the DC-bus load beside it
    grid = [(_at(-1), 0.5)]
    dc_load = [(_at(-1), 3.0)]
    (known,) = _integrate_by_kind(
        [T0], SLOT, pv, grid, battery, beats, alive_limit=3 * MIN, plant=DC_PLANT, dc_load=dc_load
    )
    assert known is not None
    assert known.battery_to_dc_load == pytest.approx(3.0)
    assert known.battery_to_load == 0.0 and known.grid_to_load == pytest.approx(0.5)
    late = [(_at(5), 3.0)]  # the DC-bus load's meter silent for the slot's first half
    assert _integrate_by_kind(
        [T0], SLOT, pv, grid, battery, beats, alive_limit=3 * MIN, plant=DC_PLANT, dc_load=late
    ) == [None]
    with pytest.raises(ValueError, match="one series per meter"):
        _integrate_by_kind(
            [T0], SLOT, pv, grid, battery, beats, alive_limit=3 * MIN, plant=DC_PLANT
        )
    with pytest.raises(ValueError, match="one series per meter"):
        _integrate_by_kind(
            [T0],
            SLOT,
            pv,
            grid,
            battery,
            beats,
            alive_limit=3 * MIN,
            plant=LOSSLESS_PLANT,
            dc_load=dc_load,
        )


def _held(kw: float, until: datetime) -> Series[float]:
    return Series.of([(T0 - MIN, kw)], known_until=until)


def test_integrate_reads_any_meter_set_under_the_plants_names() -> None:
    """The series are named as the plant names its meters, whatever
    they are called; the integration reads them through the plant."""
    renamed = Plant(
        replace(
            LOSSLESS_PLANT.config,
            meters=(Meter("solar", "pv"), Meter("utility", "grid"), Meter("bank", "battery")),
        )
    )
    end = T0 + SLOT
    series = {"solar": _held(2.0, end), "utility": _held(0.0, end), "bank": _held(1.0, end)}
    (flows,) = integrate(series, [T0], SLOT, plant=renamed)  # no alive spans: always alive
    assert flows is not None and flows.pv_to_battery == pytest.approx(1.0)
    same = {"pv": series["solar"], "grid": series["utility"], "battery": series["bank"]}
    assert integrate(same, [T0], SLOT, plant=LOSSLESS_PLANT) == [flows]
    with pytest.raises(ValueError, match="one series per meter"):
        integrate(same, [], SLOT, plant=renamed)  # checked even with no slots


def test_integrate_drops_a_reading_gone_stale() -> None:
    """``stale_nonzero``: a DC-bus load's reading other than 0, silent that long, is no
    value (the 0 that ended its session was lost): the slot is unknown."""
    end = T0 + SLOT
    series = {
        "pv": _held(0.0, end),
        "grid": _held(0.5, end),
        "battery": _held(-3.0, end),
        "dc_load": Series.of([(T0 - 20 * MIN, 3.0)], known_until=end),
    }
    (fresh,) = integrate(series, [T0], SLOT, plant=DC_PLANT)
    assert fresh is not None and fresh.battery_to_dc_load == pytest.approx(3.0)
    stale = {"dc_load": timedelta(minutes=25)}  # stale from 5 minutes into the slot
    assert integrate(series, [T0], SLOT, plant=DC_PLANT, stale_nonzero=stale) == [None]
