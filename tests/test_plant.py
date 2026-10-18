"""The plant model, and the surplus and regimes it leaves."""

from datetime import UTC, datetime, time, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

import pytest

from engine_fixtures import plant_of
from engine_fixtures import soc as soc_of
from legacy_dispatch import NativePlant, simulate
from microgrid.engine._surplus import forecast_surplus
from microgrid.engine._timeline import SLOT
from microgrid.plant import GridCharge
from microgrid.plant._battery import Battery, Grid
from microgrid.plant.flows import Regime, SlotFlow, Surplus, regimes, surplus

PACIFIC = ZoneInfo("America/Los_Angeles")
T0 = datetime(2026, 9, 23, 7, 0, tzinfo=UTC)  # 00:00 PDT
HOURS = SLOT / timedelta(hours=1)
BATTERY = Battery(
    capacity_kwh=10.0, charge_kw=5.0, discharge_kw=5.0, eta_charge=1.0, eta_discharge=1.0
)
PLANT = NativePlant(battery=BATTERY, timezone=PACIFIC)


def _starts(count: int, first: datetime = T0) -> list[datetime]:
    return [first + i * SLOT for i in range(count)]


def test_pv_serves_the_house_then_the_battery_then_export() -> None:
    (flow,) = simulate(PLANT, _starts(1), [4.0], [1.0], 0.5)
    assert flow.pv_to_load == 1.0
    assert flow.pv_to_battery == 3.0
    assert flow.pv_export == 0.0
    assert flow.soc_end == pytest.approx(0.5 + 3.0 * HOURS / 10)


def test_a_full_battery_exports() -> None:
    (flow,) = simulate(PLANT, _starts(1), [4.0], [1.0], 1.0)
    assert flow.pv_to_battery == 0.0
    assert flow.pv_export == 3.0


def test_charge_limit_then_export() -> None:
    (flow,) = simulate(PLANT, _starts(1), [9.0], [1.0], 0.2)
    assert flow.pv_to_battery == 5.0
    assert flow.pv_export == 3.0


def test_shortfall_from_battery_to_its_floor_then_grid() -> None:
    plant = NativePlant(
        battery=Battery(10.0, 5.0, 5.0, eta_charge=1.0, eta_discharge=1.0, reserve_soc=0.1),
        timezone=PACIFIC,
    )
    flows = simulate(plant, _starts(2), [0.0, 0.0], [3.0, 3.0], 0.1 + 3.0 * HOURS / 10)
    assert flows[0].battery_to_load == pytest.approx(3.0)
    assert flows[1].battery_to_load == pytest.approx(0.0)
    assert flows[1].grid_to_load == pytest.approx(3.0)
    assert flows[1].soc_end == pytest.approx(0.1)


def test_losses_and_standby() -> None:
    plant = NativePlant(
        battery=Battery(10.0, 5.0, 5.0, eta_charge=0.9, eta_discharge=0.8),
        timezone=PACIFIC,
        standby_kw=0.5,
    )
    charge, discharge = simulate(plant, _starts(2), [3.5, 0.0], [1.0, 1.0], 0.5)
    assert charge.pv_to_load == 1.5  # the house plus standby
    assert soc_of(charge.soc_end) - soc_of(charge.soc_start) == pytest.approx(
        2.0 * 0.9 * HOURS / 10
    )
    assert discharge.battery_to_load == 1.5
    assert soc_of(discharge.soc_start) - soc_of(discharge.soc_end) == pytest.approx(
        1.5 / 0.8 * HOURS / 10
    )


def test_grid_charge_tops_up_what_pv_will_not_deliver() -> None:
    plant = NativePlant(
        battery=BATTERY,
        timezone=PACIFIC,
        grid_charge=GridCharge(start=time(12), end=time(15), target_soc=1.0),
    )
    noon = datetime(2026, 9, 23, 19, 0, tzinfo=UTC)  # 12:00 PDT
    starts = _starts(24, noon)  # 12:00-16:00
    flows = simulate(plant, starts, [1.0] * 24, [1.0] * 24, 0.2)
    in_window = [f for f in flows if f.start < datetime(2026, 9, 23, 22, 0, tzinfo=UTC)]
    assert any(f.grid_to_battery > 0 for f in in_window)
    assert all(f.grid_to_battery == 0 for f in flows[18:])  # none after 15:00
    assert in_window[-1].soc_end == pytest.approx(1.0)
    assert all(f.battery_to_load == 0 for f in flows if f.grid_to_battery > 0)


def test_a_late_top_up_takes_the_same_energy_as_late_as_it_can() -> None:
    """Measured on the reference plant from 2026-09-17: its top-ups started 13:50-14:50,
    not at 12:00."""
    noon = datetime(2026, 9, 23, 19, 0, tzinfo=UTC)  # 12:00 PDT
    starts = _starts(24, noon)  # 12:00-16:00

    def run(timing: Literal["early", "late"]) -> tuple[SlotFlow, ...]:
        window = GridCharge(start=time(12), end=time(15), target_soc=1.0, timing=timing)
        plant = NativePlant(battery=BATTERY, timezone=PACIFIC, grid_charge=window)
        return simulate(plant, starts, [1.0] * 24, [1.0] * 24, 0.2)

    early, late = run("early"), run("late")
    energy = [sum(f.grid_to_battery for f in flows) * HOURS for flows in (early, late)]
    assert energy[0] == pytest.approx(energy[1]) == pytest.approx(8.0)
    assert early[0].grid_to_battery == pytest.approx(5.0)  # from the window's start
    # 8 kWh at 5 kW is 9.6 slots: the last nine at the limit, a part slot before them.
    charging = [i for i, f in enumerate(late) if f.grid_to_battery > 0]
    assert charging == list(range(8, 18))
    assert all(late[i].grid_to_battery == pytest.approx(5.0) for i in range(9, 18))
    assert late[17].soc_end == pytest.approx(1.0)  # full by 15:00 all the same
    assert all(f.grid_to_battery == 0 for f in late[18:])


def test_the_battery_is_held_in_the_window_before_a_late_top_up() -> None:
    """While it is short of its target in the window the plant does not discharge
    (09-22: the SOC sat at 59.9 % through a DC-bus load's session before the top-up)."""
    noon = datetime(2026, 9, 23, 19, 0, tzinfo=UTC)
    window = GridCharge(start=time(12), end=time(15), target_soc=0.6, timing="late")
    plant = NativePlant(battery=BATTERY, timezone=PACIFIC, grid_charge=window)
    flows = simulate(plant, _starts(18, noon), [0.0] * 18, [2.0] * 18, 0.5)
    assert all(f.battery_to_load == 0.0 and f.grid_to_load == 2.0 for f in flows)
    assert flows[-1].soc_end == pytest.approx(0.6)
    assert flows[0].grid_to_battery == 0.0  # 1 kWh short: one slot at the end


def test_grid_charge_covers_the_expensive_period_when_told_to() -> None:
    """The plant's rule: the target is 100 % if the sun can do it, else the kWh the
    projected load over the non-off-peak period needs."""
    plant = NativePlant(
        battery=BATTERY,
        timezone=PACIFIC,
        grid_charge=GridCharge(start=time(12), end=time(15), cover_until=time(0)),
    )
    noon = datetime(2026, 9, 23, 19, 0, tzinfo=UTC)  # 12:00 PDT
    starts = _starts(72, noon)  # 12:00 to midnight
    flows = simulate(plant, starts, [0.0] * 72, [1.0] * 72, 0.2)  # 9 kWh needed after 15:00
    at_end = [f for f in flows if f.start < datetime(2026, 9, 23, 22, 0, tzinfo=UTC)][-1]
    assert at_end.soc_end == pytest.approx(0.9)  # 9 kWh, not the full 10
    assert flows[-1].soc_end == pytest.approx(0.0, abs=1e-9)  # and it lasts to midnight
    lighter = simulate(plant, starts, [0.0] * 72, [0.5] * 72, 0.2)
    assert [f for f in lighter if f.start < datetime(2026, 9, 23, 22, 0, tzinfo=UTC)][
        -1
    ].soc_end == pytest.approx(0.45)
    # PV during the expensive period reduces what the battery must cover.
    sunny_evening = simulate(plant, starts, [0.5] * 72, [1.0] * 72, 0.2)
    assert [f for f in sunny_evening if f.start < datetime(2026, 9, 23, 22, 0, tzinfo=UTC)][
        -1
    ].soc_end == pytest.approx(0.45)


def test_no_grid_charge_when_pv_will_fill_it() -> None:
    plant = NativePlant(
        battery=BATTERY,
        timezone=PACIFIC,
        grid_charge=GridCharge(start=time(12), end=time(15)),
    )
    noon = datetime(2026, 9, 23, 19, 0, tzinfo=UTC)
    flows = simulate(plant, _starts(18, noon), [5.0] * 18, [1.0] * 18, 0.5)
    assert all(f.grid_to_battery == 0 for f in flows)


def test_simulation_stops_at_an_unknown_input() -> None:
    flows = simulate(PLANT, _starts(4), [1.0, 1.0, None, 1.0], [0.5] * 4, 0.5)
    assert len(flows) == 2


def test_input_checks() -> None:
    with pytest.raises(ValueError, match="same length"):
        simulate(PLANT, _starts(2), [1.0], [1.0, 1.0], 0.5)
    with pytest.raises(ValueError, match="soc"):
        simulate(PLANT, _starts(1), [1.0], [1.0], 1.5)
    with pytest.raises(ValueError, match="UTC"):
        simulate(PLANT, [datetime(2026, 9, 23)], [1.0], [1.0], 0.5)
    with pytest.raises(ValueError):
        GridCharge(start=time(15), end=time(12))
    with pytest.raises(ValueError, match="timing"):
        GridCharge(start=time(12), end=time(15), timing="soon")  # type: ignore[arg-type]


def _day(pv_peak: float, load: float = 1.0) -> tuple[list[datetime], list[float], list[float]]:
    """A 24 h PDT day: PV a triangle from 07:00 to 19:00 peaking at pv_peak."""
    starts = _starts(144)
    pv = []
    for start in starts:
        hour = start.astimezone(PACIFIC).hour + start.astimezone(PACIFIC).minute / 60
        pv.append(max(0.0, pv_peak * (1 - abs(hour - 13) / 6)))
    return starts, pv, [load] * 144


Profile = list[float]


def _surplus(
    plant: NativePlant, starts: list[datetime], pv: Profile, load: Profile, soc: float
) -> tuple[tuple[SlotFlow, ...], Surplus]:
    flows = simulate(plant, starts, pv, load, soc)
    return flows, surplus(flows)


def test_no_surplus_while_the_battery_charges_with_room_to_spare() -> None:
    """Surplus is PV the site cannot use: none while the battery still takes it all."""
    starts, pv, load = _day(pv_peak=6.0)
    flows, found = _surplus(PLANT, starts, pv, load, 0.2)
    first_export = next(i for i, f in enumerate(flows) if f.pv_export > 0)
    assert all(kw == 0 for kw in found.kw[:first_export])  # charging, nothing exported
    assert found.kw[first_export] > 0
    for flow, kw in zip(flows, found.kw, strict=True):
        assert kw == pytest.approx(flow.pv_export + flow.pv_curtail, abs=1e-9)


def test_a_full_battery_exports_all_it_cannot_use() -> None:
    (flow,) = simulate(PLANT, _starts(1, datetime(2026, 9, 23, 20, tzinfo=UTC)), [4.0], [1.0], 1.0)
    assert surplus([flow]).kw == pytest.approx((4.0 - 1.0 - PLANT.standby_kw,))


def test_no_surplus_when_the_battery_will_not_fill() -> None:
    starts, pv, load = _day(pv_peak=2.0)
    flows, found = _surplus(PLANT, starts, pv, load, 0.0)
    assert max(found.kw) == 0.0
    assert all(f.pv_export == 0 for f in flows)


def test_grid_charging_slots_are_not_surplus() -> None:
    plant = NativePlant(
        battery=Battery(16.5, 8.4, 9.6),
        timezone=PACIFIC,
        standby_kw=0.11,
        grid_charge=GridCharge(start=time(12), end=time(15), target_soc=1.0),
    )
    starts, pv, _ = _day(pv_peak=5.0)
    load = [0.7] * len(starts)
    flows, found = _surplus(plant, starts, pv, load, 0.2)
    assert any(f.grid_to_battery > 0 for f in flows)
    for flow, kw, regime in zip(flows, found.kw, regimes(flows, found), strict=True):
        if flow.grid_to_battery > 0:
            assert kw == 0.0
            assert regime is Regime.IMPORT


def test_the_plants_day_has_a_battery_night_and_an_export_afternoon() -> None:
    starts, pv, load = _day(pv_peak=6.0)
    flows, found = _surplus(PLANT, starts, pv, load, 0.2)
    kinds = regimes(flows, found)
    assert kinds[0] is Regime.BATTERY  # night, battery serving the house
    assert Regime.EXPORT in kinds
    (empty,) = simulate(PLANT, starts[:1], [0.0], [1.0], 0.0)
    assert regimes([empty], surplus([empty])) == (Regime.IMPORT,)


def test_an_export_limit_curtails_and_values_the_curtailed_part_at_nothing() -> None:
    plant = NativePlant(battery=BATTERY, timezone=PACIFIC, grid=Grid(export_limit_kw=2.0))
    starts = _starts(2, datetime(2026, 9, 23, 19, tzinfo=UTC))
    flows = simulate(plant, starts, [2.5, 8.0], [1.0, 1.0], 1.0)
    found = surplus(flows, [0.10, 0.20])
    assert found.value[0] == pytest.approx(0.10)  # under the limit: all exported
    assert flows[1].pv_export == pytest.approx(2.0) and flows[1].pv_curtail > 0
    assert found.value[1] == pytest.approx(2.0 * 0.20 / found.kw[1])
    assert regimes(flows, found) == (Regime.EXPORT, Regime.CURTAIL)  # mostly curtailed


def test_a_slot_already_under_way() -> None:
    """SOC now, with part of the first slot gone: only the rest of it moves energy."""
    start = _starts(1, datetime(2026, 9, 23, 20, tzinfo=UTC))
    (full_slot,) = simulate(PLANT, start, [4.0], [1.0], 0.5)
    (half_slot,) = simulate(PLANT, start, [4.0], [1.0], 0.5, first_remaining=SLOT / 2)
    assert soc_of(half_slot.soc_end) - 0.5 == pytest.approx((soc_of(full_slot.soc_end) - 0.5) / 2)
    with pytest.raises(ValueError, match="within one slot"):
        simulate(PLANT, start, [4.0], [1.0], 0.5, first_remaining=2 * SLOT)


def test_forecast_surplus_pessimistic_never_exceeds_expected_and_pads_unknowns() -> None:
    starts, pv, load = _day(pv_peak=6.0)
    pv10 = [p * 0.6 for p in pv]
    load_with_gap: list[float | None] = [*load[:-6], *([None] * 6)]
    result = forecast_surplus(plant_of(PLANT), starts, pv, pv10, load_with_gap, 0.2)
    pairs = zip(result.surplus_kw, result.surplus_kw_p10, strict=True)
    known = [(a, b) for a, b in pairs if a is not None]
    assert all(b is not None and b <= a for a, b in known)
    assert result.surplus_kw[-6:] == (None,) * 6
    assert result.regime[-1] is None
    assert len(result.flows) == 138


def test_surplus_is_additive_the_energy_there_is_to_share() -> None:
    starts, pv, load = _day(pv_peak=6.0)
    result = forecast_surplus(plant_of(PLANT), starts, pv, pv, load, 0.2)
    shared = sum(v for v in result.surplus_kw if v) * HOURS
    exported = sum(f.pv_export + f.pv_curtail for f in result.flows) * HOURS
    assert shared == pytest.approx(exported)


def test_per_slot_durations() -> None:
    starts = [T0 + timedelta(hours=12) + i * SLOT for i in range(3)]
    half = simulate(
        PLANT,
        starts,
        [4.0] * 3,
        [1.0] * 3,
        0.5,
        durations=[SLOT, SLOT / 2, SLOT],
    )
    full = simulate(PLANT, starts, [4.0] * 3, [1.0] * 3, 0.5)
    charged = [soc_of(f.soc_end) - soc_of(f.soc_start) for f in half]
    assert charged[1] == pytest.approx(charged[0] / 2)  # half the time, half the energy
    assert half[0] == full[0]
    with pytest.raises(ValueError, match="one length per slot"):
        simulate(PLANT, starts, [4.0] * 3, [1.0] * 3, 0.5, durations=[SLOT])
    with pytest.raises(ValueError, match="positive"):
        simulate(PLANT, starts, [4.0] * 3, [1.0] * 3, 0.5, durations=[SLOT, timedelta(0), SLOT])
