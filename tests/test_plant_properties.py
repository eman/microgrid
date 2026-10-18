"""Properties of the plant model and of surplus."""

from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from engine_fixtures import soc as soc_of
from legacy_dispatch import NativePlant, simulate
from microgrid.engine._timeline import SLOT
from microgrid.plant import WORKING_KW, GridCharge
from microgrid.plant._battery import Battery, Grid
from microgrid.plant.flows import surplus

HOURS = SLOT / timedelta(hours=1)
T0 = datetime(2026, 9, 23, 7, 0, tzinfo=UTC)
MORNING = datetime(2026, 9, 23, 15, 0, tzinfo=UTC)  # 08:00 PDT
PACIFIC = ZoneInfo("America/Los_Angeles")
power = st.floats(0, 12, allow_nan=False)
plants = st.builds(
    lambda cap, charge, discharge, eta_c, eta_d, reserve, standby, working, q_c, q_d: NativePlant(
        battery=Battery(
            cap,
            charge,
            discharge,
            eta_c,
            eta_d,
            reserve_soc=reserve,
            loss_charge_per_kw=q_c,
            loss_discharge_per_kw=q_d,
        ),
        timezone=ZoneInfo("America/Los_Angeles"),
        standby_kw=standby,
        working_kw=working,
    ),
    st.floats(2, 30),
    st.floats(1, 10),
    st.floats(1, 10),
    st.floats(0.8, 1.0),
    st.floats(0.8, 1.0),
    st.floats(0, 0.2),
    st.floats(0, 0.3),
    st.sampled_from([0.0, 0.05]),
    st.sampled_from([0.0, 0.005]),  # the reference plant's fitted 0.0046 and 0.0015
    st.sampled_from([0.0, 0.002]),
)


@given(plants, st.lists(st.tuples(power, power), min_size=1, max_size=36), st.floats(0, 1))
def test_flows_balance_and_respect_limits(
    plant: NativePlant, profile: list[tuple[float, float]], soc: float
) -> None:
    battery = plant.battery
    soc = max(soc, battery.reserve_soc)
    starts = [T0 + i * SLOT for i in range(len(profile))]
    pv = [p for p, _ in profile]
    load = [q for _, q in profile]
    flows = simulate(plant, starts, pv, load, soc)
    for flow, p, q in zip(flows, pv, load, strict=True):
        assert abs(flow.pv_to_load + flow.pv_spare - p) < 1e-9
        charging = flow.pv_to_battery + flow.grid_to_battery
        # The inverter draws its standby, and working_kw more only where the battery
        # then moves more than WORKING_KW.
        extra = flow.pv_to_load + flow.battery_to_load + flow.grid_to_load - q - plant.standby_kw
        assert abs(extra) < 1e-9 or abs(extra - plant.working_kw) < 1e-9
        if plant.working_kw > 0 and abs(extra - plant.working_kw) < 1e-9:
            assert flow.moving > WORKING_KW
        assert charging <= battery.charge_kw + 1e-9
        assert flow.battery_to_load <= battery.discharge_kw + 1e-9
        stored = battery.stored_by(charging) * HOURS
        drawn = battery.drawn_by(flow.battery_to_load) * HOURS
        change = (soc_of(flow.soc_end) - soc_of(flow.soc_start)) * battery.capacity_kwh
        assert abs(change - (stored - drawn)) < 1e-6
        assert battery.reserve_soc - 1e-9 <= soc_of(flow.soc_end) <= 1 + 1e-9
        assert min(flow.pv_export, flow.grid_to_load, flow.battery_to_load) >= -1e-12


grid_charges = st.one_of(
    st.none(),
    st.builds(
        lambda start, length, target: GridCharge(
            start=time(start), end=time(start + length), target_soc=target
        ),
        st.integers(9, 13),
        st.integers(1, 3),
        st.floats(0.3, 1.0),
    ),
)
richer_plants = st.builds(
    lambda cap, charge, discharge, eta_c, eta_d, reserve, saturation, standby, limit, window: (
        NativePlant(
            battery=Battery(
                cap, charge, discharge, eta_c, eta_d, reserve_soc=reserve, saturation_soc=saturation
            ),
            timezone=PACIFIC,
            grid=Grid(export_limit_kw=limit),
            standby_kw=standby,
            grid_charge=window,
        )
    ),
    st.floats(0.5, 30),
    st.floats(1, 10),
    st.floats(1, 10),
    st.floats(0.8, 1.0),
    st.floats(0.8, 1.0),
    st.floats(0, 0.2),
    st.floats(0.8, 1.0),
    st.floats(0, 0.3),
    st.one_of(st.none(), st.floats(0, 5)),
    grid_charges,
)
# From 08:00 PDT, 6 h of slots; PV may drop to 0 (a night-like gap splits blocks).
day_profiles = st.lists(
    st.tuples(st.one_of(st.just(0.0), st.floats(0.05, 12)), st.floats(0, 5)),
    min_size=2,
    max_size=36,
)


@settings(max_examples=300, deadline=None)
@given(richer_plants, day_profiles, st.floats(0, 1))
def test_surplus_is_only_pv_the_site_cannot_use(
    plant: NativePlant, profile: list[tuple[float, float]], soc: float
) -> None:
    """The definition of surplus: exported or curtailed PV, and only once the battery
    is full or charging as fast as it can."""
    soc = min(max(soc, plant.battery.reserve_soc), plant.battery.saturation_soc)
    starts = [MORNING + i * SLOT for i in range(len(profile))]
    pv = [p for p, _ in profile]
    load = [q for _, q in profile]
    flows = simulate(plant, starts, pv, load, soc)
    found = surplus(flows)
    battery = plant.battery
    for flow, kw in zip(flows, found.kw, strict=True):
        assert kw >= 0
        assert kw == 0 or kw == pytest.approx(flow.pv_export + flow.pv_curtail, abs=1e-9)
        if kw > 0:
            full = soc_of(flow.soc_end) >= battery.saturation_soc - 1e-9
            at_limit = flow.pv_to_battery + flow.grid_to_battery >= battery.charge_kw - 1e-9
            assert full or at_limit


batteries = st.builds(
    lambda eta_c, eta_d, q_c, q_d, a, b: Battery(
        16.0,
        8.4,
        9.6,
        eta_c,
        eta_d,
        loss_charge_per_kw=q_c,
        loss_discharge_per_kw=q_d,
        meter_charge=a,
        meter_discharge=b,
    ),
    st.floats(0.8, 1.0),
    st.floats(0.8, 1.0),
    st.floats(0, 0.01),
    st.floats(0, 0.005),
    st.floats(0.95, 1.05),
    st.floats(0.95, 1.05),
)


@given(batteries, st.floats(0, 8.4), st.floats(0, 9.6))
def test_the_conversion_inverts_and_loses_more_at_power(
    battery: Battery, charge: float, discharge: float
) -> None:
    """AC to stored and back are each other's inverse; the margin loses at least
    the average, and nothing is ever gained."""
    stored = battery.stored_by(charge)
    assert battery.charge_for(stored) == pytest.approx(charge, abs=1e-9)
    assert stored <= battery.eta_charge * charge + 1e-9
    drawn = battery.drawn_by(discharge)
    assert battery.delivered_by(drawn) == pytest.approx(discharge, abs=1e-9)
    assert drawn >= discharge / battery.eta_discharge - 1e-9
    if charge > 1e-6:
        assert battery.eta_charge_at(charge) <= stored / charge + 1e-9
    if discharge > 1e-6:
        assert battery.eta_discharge_at(discharge) <= discharge / drawn + 1e-9
    metered = stored / battery.meter_charge  # what the battery's own meter reads
    assert battery.charge_ac(metered) == pytest.approx(charge, abs=1e-9)
    assert battery.discharge_ac(drawn / battery.meter_discharge) == pytest.approx(
        discharge, abs=1e-9
    )
