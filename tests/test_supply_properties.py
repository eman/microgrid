"""Properties of what serves a kWh and of the supply stack."""

from datetime import UTC, datetime, time, timedelta
from functools import partial
from zoneinfo import ZoneInfo

from hypothesis import given, settings
from hypothesis import strategies as st

from engine_fixtures import plant_of, rerun_cost
from legacy_dispatch import NativePlant, PlantStorage, simulate
from microgrid.engine._supply import supply_stack
from microgrid.engine._value import (
    Event,
    Served,
    price_flows,
)
from microgrid.engine._value import (
    value_energy as _value_energy,
)
from microgrid.plant import WORKING_KW, GridCharge
from microgrid.plant._battery import Battery
from microgrid.plant.flows import SlotFlow

#: The make-up rule's arithmetic, tested as such: what replaces the kWh
#: (``Pricing.battery_value = "replacement"``; the default prices a battery kWh at
#: its stored cost).
value_energy = partial(_value_energy, battery_value="replacement")

T0 = datetime(2026, 9, 25, 7, 0, tzinfo=UTC)
SLOT = timedelta(minutes=10)
HOURS = SLOT / timedelta(hours=1)
power = st.floats(0, 10, allow_nan=False)
price = st.floats(0.0, 0.6, allow_nan=False)
plants = st.builds(
    lambda eta_c, eta_d, reserve, wear, q: NativePlant(
        Battery(
            10.0,
            5.0,
            5.0,
            eta_c,
            eta_d,
            reserve_soc=reserve,
            wear_charge_per_kwh=wear,
            wear_discharge_per_kwh=wear,
            loss_charge_per_kw=q,
            loss_discharge_per_kw=q,
        ),
        ZoneInfo("America/Los_Angeles"),
    ),
    st.floats(0.8, 1.0),
    st.floats(0.8, 1.0),
    st.floats(0, 0.2),
    st.floats(0.0, 0.05),
    st.sampled_from([0.0, 0.004]),
)
days = st.lists(st.tuples(power, power, price, price), min_size=2, max_size=48)


@settings(max_examples=200)
@given(plants, days, st.floats(0, 1))
def test_the_stack_starts_where_the_rule_serves_the_kwh(
    plant: NativePlant, day: list[tuple[float, float, float, float]], soc: float
) -> None:
    battery = plant.battery
    soc = max(soc, battery.reserve_soc)
    starts = [T0 + i * SLOT for i in range(len(day))]
    flows = simulate(plant, starts, [d[0] for d in day], [d[1] for d in day], soc)
    p_import, p_export = [d[2] for d in day], [d[3] for d in day]
    priced = price_flows(PlantStorage(plant.battery), flows, p_import, p_export, 144)
    for t, flow in enumerate(flows):
        served, cost, stored = priced.served[t], priced.prices[t], priced.stored_value[t]
        low, high = battery.dead_band(
            stored,
            charging_kw=flow.pv_to_battery + flow.grid_to_battery,
            discharging_kw=flow.battery_to_load,
        )
        assert low <= high + 1e-12  # the dead band never inverts
        if served is Served.GRID:
            assert cost == p_import[t]
        elif served is Served.PV:
            assert cost == low
        elif served is Served.BATTERY:
            assert cost == high
        else:
            assert cost <= p_export[t] + 1e-12  # the export, or 0 curtailed
        steps = supply_stack(
            PlantStorage(plant.battery),
            flow,
            HOURS,
            served=served,
            price=cost,
            p_import=p_import[t],
            stored_value=stored,
        )
        assert steps[-1].source is Served.GRID and steps[-1].kw is None
        assert all(step.kw is not None and step.kw > 0 for step in steps[:-1])
        order = [Served.SPILL, Served.PV, Served.BATTERY, Served.GRID]
        assert [step.source for step in steps] == sorted(
            (step.source for step in steps), key=order.index
        )
        for step in steps:
            if step.source is Served.BATTERY:
                assert step.kw is not None
                assert step.kw <= battery.discharge_kw - flow.battery_to_load + 1e-9
        # A small load pays the first step's price: the slot's, in the same run.
        if served is not Served.BATTERY or len(steps) > 1:
            assert steps[0].source is served and steps[0].per_kwh == cost


@settings(max_examples=200)
@given(days, st.floats(0, 1))
def test_without_loss_growth_a_round_trip_only_where_the_kwh_is_refilled(
    day: list[tuple[float, float, float, float]], soc: float
) -> None:
    """After a fill or a top-up a battery kWh costs the make-up
    through a round trip; after an empty battery or the horizon, the make-up itself."""
    battery = Battery(10.0, 5.0, 5.0, 0.9, 0.95, wear_charge_per_kwh=0.02,
                      wear_discharge_per_kwh=0.03)  # fmt: skip
    plant = NativePlant(battery, ZoneInfo("America/Los_Angeles"))
    starts = [T0 + i * SLOT for i in range(len(day))]
    flows = simulate(plant, starts, [d[0] for d in day], [d[1] for d in day], soc)
    priced = price_flows(
        PlantStorage(plant.battery), flows, [d[2] for d in day], [d[3] for d in day], 144
    )
    for t in range(len(flows)):
        if priced.served[t] is not Served.BATTERY:
            continue
        make_up = priced.make_up[t]
        if priced.make_up_event[t] in (Event.FILL, Event.TOPUP):
            expected = battery.minimum_spread(make_up)
        else:
            expected = make_up
        assert abs(priced.prices[t] - expected) < 1e-9


#: A vanishing load, kW: the price of one more kWh, re-simulated (``rerun_cost``).
TINY_KW = 1e-4
PACIFIC = ZoneInfo("America/Los_Angeles")
#: A flow this close to one of the rule's floors, or to the inverter's working
#: threshold, can be tipped over it by TINY_KW: an edge, not a mismatch.
_EDGE_KW = 0.002


def _at_an_edge(plant: NativePlant, flows: tuple[SlotFlow, ...], first: int, last: int) -> bool:
    for flow in flows[first : last + 1]:
        quantities = (
            flow.pv_export + flow.pv_curtail,
            flow.pv_to_battery,
            flow.grid_to_load,
            flow.grid_to_battery,
        )
        if any(0 < q <= _EDGE_KW for q in quantities):
            return True
        band = (WORKING_KW - _EDGE_KW, WORKING_KW + plant.working_kw + _EDGE_KW)
        if plant.working_kw and band[0] <= flow.moving <= band[1]:
            return True
    return False


@settings(max_examples=40, deadline=None)
@given(
    st.floats(1, 10),
    st.floats(0.1, 1.0),
    st.sampled_from([None, (0.65, "early"), (1.0, "early"), (0.65, "late"), (1.0, "late")]),
    st.sampled_from([7, 13, 17, 19, 23]),
    st.lists(st.floats(0.25, 3.0), min_size=24, max_size=24),
)
def test_every_slots_price_is_what_one_more_kwh_costs_the_plant(
    peak: float,
    soc: float,
    window: tuple[float, str] | None,
    hour: int,
    loads: list[float],
) -> None:
    """The rule, event by event, against running the plant again with a vanishing load,
    over site-like plants (the conversion and draws measured on the reference plant),
    with and without the grid-charge window, early and late."""
    battery = Battery(
        16.0,
        8.4,
        9.6,
        0.987,
        0.954,
        reserve_soc=0.1,
        wear_charge_per_kwh=0.02,
        wear_discharge_per_kwh=0.02,
        loss_charge_per_kw=0.0046,
        loss_discharge_per_kw=0.0015,
        meter_charge=0.987,
        meter_discharge=1.017,
    )
    grid_charge = (
        None
        if window is None
        else GridCharge(time(12), time(15), target_soc=window[0], timing=window[1])  # type: ignore[arg-type]
    )
    plant = NativePlant(
        battery, PACIFIC, standby_kw=0.042, working_kw=0.049, grid_charge=grid_charge
    )
    start = datetime(2026, 9, 25, hour, tzinfo=UTC)
    count = 144
    starts = [start + i * SLOT for i in range(count)]
    local = [s.astimezone(PACIFIC).hour + s.astimezone(PACIFIC).minute / 60 for s in starts]
    pv = [max(0.0, peak * (1 - ((h - 13) / 6) ** 2)) if 7 < h < 19 else 0.0 for h in local]
    load = [loads[i // 6 % 24] for i in range(count)]
    p_import = [
        0.592 if 16 <= h < 21 else 0.45 if 15 <= h < 16 or h >= 21 else 0.373 for h in local
    ]
    p_export = [0.12 if 16 <= h < 21 else 0.06 for h in local]
    energy = value_energy(
        plant_of(plant),
        starts,
        [pv],
        load,
        max(soc, battery.reserve_soc),
        p_import,
        p_export,
        step=SLOT,
    )
    assert energy.model is not None
    flows, priced = energy.model.flows[0], energy.model.priced[0]
    for t in range(0, count, 5):
        if _at_an_edge(plant, flows, t, min(priced.event_at[t], count - 1)):
            continue
        cost = rerun_cost(energy.model, 0, t, TINY_KW)
        assert abs(cost - priced.prices[t]) <= 1e-3 + 2e-3 * abs(cost), (
            t,
            priced.served[t],
            priced.make_up_event[t],
        )
