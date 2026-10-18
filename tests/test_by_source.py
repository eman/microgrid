"""What serves a kWh, values by source and the supply stack (tariffkit#67 R-2, R-3,
R-10)."""

from datetime import UTC, datetime, time, timedelta
from functools import partial
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from engine_fixtures import plant_of, rerun_cost
from legacy_dispatch import NativePlant, PlantStorage, simulate
from microgrid.engine._supply import Step, supply_stack
from microgrid.engine._value import (
    OBSERVED,
    Displaces,
    EnergyValue,
    Event,
    PvRouting,
    Served,
    importing,
    price_flows,
)
from microgrid.engine._value import (
    value_energy as _value_energy,
)
from microgrid.plant import GridCharge
from microgrid.plant._battery import Battery
from microgrid.plant.flows import SlotFlow

#: The make-up rule's arithmetic, tested as such: what replaces the kWh
#: (``Pricing.battery_value = "replacement"``; the default prices a battery kWh at
#: its stored cost).
value_energy = partial(_value_energy, battery_value="replacement")

PACIFIC = ZoneInfo("America/Los_Angeles")
T0 = datetime(2026, 9, 25, 7, 0, tzinfo=UTC)  # midnight PDT
SLOT = timedelta(minutes=10)
HOURS = 1 / 6
BATTERY = Battery(
    10.0,
    5.0,
    5.0,
    eta_charge=0.9,
    eta_discharge=0.9,
    reserve_soc=0.1,
    wear_charge_per_kwh=0.02,
    wear_discharge_per_kwh=0.02,
)
PLANT = NativePlant(BATTERY, PACIFIC)
IMPORT, EXPORT = 0.40, 0.08
#: A load too small to move an event, kW: re-simulated, it costs the slot's price.
TINY_KW = 0.001
#: Hand-built flows carry no port flows: the rule over today's storage, the oracle.
STORAGE = PlantStorage(PLANT.battery)


def _flow(i: int, soc_start: float, soc_end: float, **kw: Any) -> SlotFlow:
    return SlotFlow(start=T0 + i * SLOT, soc_start=soc_start, soc_end=soc_end, **kw)


def _starts(count: int) -> list[datetime]:
    return [T0 + i * SLOT for i in range(count)]


def _value(
    plant: NativePlant,
    pv: list[float],
    load: list[float],
    soc: float,
    p_import: list[float] | None = None,
) -> EnergyValue:
    count = len(pv)
    return value_energy(
        plant_of(plant),
        _starts(count),
        [pv],
        load,
        soc,
        p_import or [IMPORT] * count,
        [EXPORT] * count,
        step=SLOT,
    )


def _brute(energy: EnergyValue, slot: int) -> float:
    """The plant run again with a vanishing load in ``slot``: what it changed, priced."""
    assert energy.model is not None
    return rerun_cost(energy.model, 0, slot, TINY_KW)


# What serves a kWh, and at what cost (_value.price_flows)


def test_after_a_fill_pv_costs_the_export_and_the_battery_a_round_trip() -> None:
    """A kWh the battery gives now is refilled from PV that would have spilled."""
    night, day = [0.0] * 6, [4.0] * 60  # 3 kW spare: within the 5 kW charge limit
    energy = _value(PLANT, night + day, [1.0] * 66, 0.5)
    assert energy.model is not None
    priced = energy.model.priced[0]
    assert priced.make_up_event[0] is Event.FILL and priced.served[0] is Served.BATTERY
    assert energy.price[0] == pytest.approx(BATTERY.minimum_spread(EXPORT))
    assert energy.price[0] == pytest.approx((EXPORT + 0.02) / 0.81 + 0.02)
    charging = 8  # the morning: PV charging the battery
    assert priced.served[charging] is Served.PV
    assert energy.price[charging] == pytest.approx(EXPORT)  # a kWh less spilled later
    for slot in (0, charging):
        assert energy.price[slot] == pytest.approx(_brute(energy, slot), rel=1e-3)


def test_after_a_top_up_the_battery_is_bought_back_in_the_window() -> None:
    window = GridCharge(time(1), time(3), target_soc=0.9)
    plant = NativePlant(BATTERY, PACIFIC, grid_charge=window)
    prices = [IMPORT] * 6 + [0.25] * 12 + [IMPORT] * 18
    energy = _value(plant, [0.0] * 36, [1.0] * 36, 0.5, prices)
    assert energy.model is not None
    priced = energy.model.priced[0]
    assert priced.make_up_event[0] is Event.TOPUP and priced.make_up[0] == 0.25
    assert energy.price[0] == pytest.approx(BATTERY.minimum_spread(0.25))
    assert energy.price[0] == pytest.approx(_brute(energy, 0), rel=1e-3)
    topping = next(i for i, flow in enumerate(energy.model.flows[0]) if flow.grid_to_battery)
    assert priced.served[topping] is Served.GRID  # the plant holds the battery then


def test_a_battery_that_runs_empty_costs_the_import_one_for_one() -> None:
    """Drawing a kWh now only moves the discharge earlier. No more goes through the
    battery, so no round trip: the make-up itself."""
    energy = _value(PLANT, [0.0] * 30, [3.0] * 30, 0.4)
    assert energy.model is not None
    priced = energy.model.priced[0]
    assert priced.make_up_event[0] is Event.EMPTY and priced.served[0] is Served.BATTERY
    assert energy.price[0] == pytest.approx(IMPORT)
    assert energy.price[0] < BATTERY.minimum_spread(IMPORT)
    assert energy.price[0] == pytest.approx(_brute(energy, 0), rel=1e-3)
    battery = energy.battery_value[0]
    assert battery is not None and battery.displaces is Displaces.LATER_IMPORT
    assert battery.per_kwh == pytest.approx(IMPORT)


def test_pv_diverted_from_a_battery_that_runs_empty_saves_the_round_trip() -> None:
    """A kWh of PV the battery does not store is eta_rt kWh less it delivers, and the
    wear of that round trip is saved."""
    pv = [2.0] * 3 + [0.0] * 27
    energy = _value(PLANT, pv, [1.0] * 3 + [3.0] * 27, 0.3)
    assert energy.model is not None
    priced = energy.model.priced[0]
    assert priced.served[0] is Served.PV and priced.make_up_event[0] is Event.EMPTY
    expected = 0.81 * (IMPORT - 0.02) - 0.02
    assert energy.price[0] == pytest.approx(expected)
    assert energy.price[0] == pytest.approx(_brute(energy, 0), rel=1e-3)
    pv_value = energy.pv_value[0]
    assert pv_value is not None and pv_value.routing is PvRouting.BATTERY
    assert pv_value.per_kwh == pytest.approx(expected)


def test_past_the_horizon_a_battery_kwh_is_the_days_import() -> None:
    energy = _value(PLANT, [0.0] * 12, [0.5] * 12, 0.9)
    assert energy.model is not None
    assert energy.model.priced[0].make_up_event[0] is Event.HORIZON
    assert energy.price[0] == pytest.approx(IMPORT)
    assert energy.price[0] == pytest.approx(_brute(energy, 0), rel=1e-3)


def test_the_inverters_loss_is_taken_at_the_power_it_already_converts() -> None:
    """A battery discharging hard gives its next kWh at a higher loss."""
    growing = Battery(
        10.0,
        5.0,
        5.0,
        eta_charge=0.9,
        eta_discharge=0.9,
        reserve_soc=0.1,
        loss_discharge_per_kw=0.01,
    )
    plant = NativePlant(growing, PACIFIC)
    light = _value(plant, [0.0] * 6 + [8.0] * 60, [0.5] * 66, 0.5)
    heavy = _value(plant, [0.0] * 6 + [8.0] * 60, [4.0] * 66, 0.5)
    assert heavy.price[0] > light.price[0]
    for energy in (light, heavy):
        assert energy.price[0] == pytest.approx(_brute(energy, 0), rel=1e-3)


def test_an_empty_battery_an_import_or_the_limit_is_the_grid() -> None:
    flows = [
        _flow(0, 0.10, 0.10),  # at the reserve, nothing charging: nothing to give
        _flow(1, 0.50, 0.40, battery_to_load=5.0, grid_to_load=1.0),  # past its limit
        _flow(2, 0.50, 0.45, battery_to_load=5.0),  # at its limit, the house not importing
    ]
    priced = price_flows(STORAGE, flows, [IMPORT] * 3, [EXPORT] * 3, 144)
    assert priced.served == (Served.GRID,) * 3 and priced.prices == (IMPORT,) * 3


def test_the_slot_under_way_is_judged_on_power() -> None:
    """One minute left, the plant holding the battery and the house importing 0.5 kW is
    an import, not float residue."""
    held = _flow(0, 0.6, 0.6, grid_to_load=0.5)
    assert importing(STORAGE, held, OBSERVED)
    assert not importing(STORAGE, _flow(0, 0.6, 0.6, grid_to_load=0.05), OBSERVED)


# Values by source (_value.value_energy)


def test_values_by_source_weigh_the_scenarios_that_have_them() -> None:
    """Slot 0 spills in the bright scenario and charges the battery in the dull one."""
    count = 30
    bright, dull = [6.0] * count, [1.5] * count
    energy = value_energy(
        plant_of(PLANT),
        _starts(count),
        [dull, bright],
        [0.5] * count,
        0.9,
        [IMPORT] * count,
        [EXPORT] * count,
        step=SLOT,
        weights=[0.3, 0.7],
    )
    assert energy.model is not None
    dull_priced, bright_priced = energy.model.priced
    assert bright_priced.served[0] is Served.SPILL and dull_priced.served[0] is Served.PV
    pv = energy.pv_value[0]
    assert pv is not None and pv.weight == pytest.approx(1.0)
    assert pv.routing is PvRouting.EXPORT  # 0.7 of the weight
    assert pv.per_kwh == pytest.approx(0.3 * dull_priced.prices[0] + 0.7 * EXPORT)
    battery = energy.battery_value[0]
    assert battery is not None and battery.displaces is Displaces.EXPORT
    assert battery.per_kwh == pytest.approx(BATTERY.minimum_spread(EXPORT))


def test_a_tie_goes_to_the_first_reason() -> None:
    count = 30
    energy = value_energy(
        plant_of(PLANT),
        _starts(count),
        [[1.5] * count, [6.0] * count],
        [0.5] * count,
        0.9,
        [IMPORT] * count,
        [EXPORT] * count,
        step=SLOT,
    )
    pv = energy.pv_value[0]
    assert pv is not None and pv.routing is PvRouting.EXPORT  # 0.5 each: export first


def test_no_pv_at_night_and_no_battery_when_empty() -> None:
    energy = _value(PLANT, [0.0] * 3, [1.0] * 3, 0.1)
    assert energy.pv_value == (None, None, None)
    assert energy.battery_value == (None, None, None)
    assert energy.price == pytest.approx((IMPORT,) * 3)


# The supply stack (_supply.supply_stack)


def _stack(flow: SlotFlow, served: Served, price: float, stored: float) -> tuple[Step, ...]:
    return supply_stack(
        STORAGE,
        flow,
        HOURS,
        served=served,
        price=price,
        p_import=IMPORT,
        stored_value=stored,
    )


def test_the_stack_follows_the_plants_order_at_its_dead_band() -> None:
    stored = BATTERY.refill_value(EXPORT)
    flow = _flow(0, 0.99, 1.0, pv_to_battery=0.4, pv_export=1.0)
    steps = _stack(flow, Served.SPILL, EXPORT, stored)
    assert [step.source for step in steps] == [
        Served.SPILL,
        Served.PV,
        Served.BATTERY,
        Served.GRID,
    ]
    low, high = BATTERY.dead_band(stored)
    assert [step.per_kwh for step in steps] == pytest.approx([EXPORT, low, high, IMPORT])
    assert [step.kw for step in steps[:3]] == pytest.approx([1.0, 0.4, 5.0])


def test_the_grid_alone_where_the_grid_serves() -> None:
    held = _flow(0, 0.6, 0.6, grid_to_load=2.0)  # the plant holds it in its window
    assert _stack(held, Served.GRID, IMPORT, 0.3) == (Step(Served.GRID, None, IMPORT),)


def test_no_battery_step_from_the_pv_it_just_charged() -> None:
    """At its reserve with PV charging, the charge is the pv step's; the battery had
    nothing of its own to give."""
    flow = _flow(0, 0.10, 0.12, pv_to_battery=1.2 / 0.9)
    steps = _stack(flow, Served.PV, 0.1, 0.2)
    assert [step.source for step in steps] == [Served.PV, Served.GRID]


def test_an_emptying_battery_offers_its_energy() -> None:
    low = _flow(0, 0.12, 0.11, battery_to_load=0.5)  # 0.1 kWh above the reserve
    battery, _ = _stack(low, Served.BATTERY, 0.3, 0.3)
    assert battery.source is Served.BATTERY
    assert battery.kw == pytest.approx(0.1 * 0.9 / HOURS)  # energy-limited


# The inputs


def test_the_inputs_need_their_lengths() -> None:
    with pytest.raises(ValueError, match="durations"):
        value_energy(
            plant_of(PLANT),
            _starts(2),
            [[0.0, 0.0]],
            [0.5, 0.5],
            0.5,
            [IMPORT] * 2,
            [EXPORT] * 2,
            step=SLOT,
            durations=[SLOT],
        )


# The window after its top-up, a dawn empty, the slot under way, and the inverter's draw.

SITE_BATTERY = Battery(
    16.0,
    8.4,
    9.6,
    eta_charge=0.94,
    eta_discharge=0.94,
    reserve_soc=0.1,
    wear_charge_per_kwh=0.02,
    wear_discharge_per_kwh=0.02,
)
NOON = datetime(2026, 9, 25, 19, 0, tzinfo=UTC)  # 12:00 PDT


@pytest.mark.parametrize("target", [0.65, 1.0])
@pytest.mark.parametrize("timing", ["early", "late"])
def test_a_kwh_taken_in_the_window_after_its_top_up_is_bought_back_in_it(
    target: float, timing: str
) -> None:
    """(H1) After an early top-up the battery is on course to end the window at its
    target: a kWh taken later in the window is bought back by the next slot's top-up.
    The window's last slot too: PV charges before the plant decides its top-up."""
    window = GridCharge(time(12), time(15), target_soc=target, timing=timing)  # type: ignore[arg-type]
    plant = NativePlant(SITE_BATTERY, PACIFIC, standby_kw=0.11, grid_charge=window)
    count = 60  # to 22:00
    pv = [2.5] * 24 + [0.0] * 36  # PV until 16:00
    prices = [0.373] * 24 + [0.592] * 30 + [0.45] * 6
    energy = value_energy(
        plant_of(plant),
        [NOON + i * SLOT for i in range(count)],
        [pv],
        [0.5] * count,
        0.3,
        prices,
        [EXPORT] * count,
        step=SLOT,
    )
    assert energy.model is not None
    for slot in (6, 9, 12, 15, 17):  # 13:00 to 14:50
        assert energy.price[slot] == pytest.approx(_brute(energy, slot), rel=1e-3)
        assert energy.model.priced[0].make_up_event[slot] is Event.TOPUP


def test_a_battery_that_empties_at_dawn_with_little_short_is_empty() -> None:
    """(H2) The model has no sensor noise: under 0.12 kW short at the reserve is empty.
    0.9 kWh above the reserve: five slots at 1 kW take 0.833, the sixth at 0.45 kW is
    about 0.05 kW short, and then PV covers the house."""
    load = [1.0] * 5 + [0.45] + [0.5] * 18
    pv = [0.0] * 6 + [3.0] * 18
    energy = _value(PLANT, pv, load, 0.2)
    assert energy.model is not None
    dawn = energy.model.flows[0][5]
    assert 0 < dawn.grid_to_load < 0.12 and dawn.soc_end == pytest.approx(0.1)
    priced = energy.model.priced[0]
    assert priced.make_up_event[0] is Event.EMPTY and priced.event_at[0] == 5
    assert energy.price[0] == pytest.approx(IMPORT)
    assert energy.price[0] == pytest.approx(_brute(energy, 0), rel=1e-3)


def test_the_slot_under_way_is_priced_for_what_is_left_of_it() -> None:
    count = 30
    energy = value_energy(
        plant_of(PLANT),
        _starts(count),
        [[0.0] * count],
        [3.0] * count,
        0.4,
        [IMPORT] * count,
        [EXPORT] * count,
        step=SLOT,
        first_remaining=timedelta(minutes=2),
    )
    assert energy.model is not None
    assert energy.price[0] == pytest.approx(_brute(energy, 0), rel=1e-3)


def test_the_inverters_draw_is_not_paid_by_a_trickle() -> None:
    """(M1) The draw while the battery works is paid where it then moves more than
    WORKING_KW: not for 30 W of spare PV at the reserve, nor 30 W of discharge."""
    plant = NativePlant(BATTERY, PACIFIC, working_kw=0.05)
    trickle = simulate(plant, _starts(1), [1.03], [1.0], 0.1)[0]
    assert trickle.grid_to_load == 0.0 and trickle.pv_to_battery == pytest.approx(0.03)
    small = simulate(plant, _starts(1), [0.97], [1.0], 0.5)[0]
    assert small.battery_to_load == pytest.approx(0.03)
