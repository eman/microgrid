"""The dispatch's parts: several grid-charge windows, and the charge order (PV first, or
the target first)."""

from dataclasses import replace
from datetime import UTC, datetime, time, timedelta

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from engine_fixtures import EXAMPLE_PLANT, PACIFIC, config_of
from legacy_dispatch import NativePlant, simulate
from microgrid.engine.extend import Event, SlotFlow, price_flows
from microgrid.engine.series import SLOT
from microgrid.plant import (
    Battery,
    BatteryPart,
    Dispatch,
    ExportWindow,
    GridCharge,
    GridLimits,
    Inverter,
    Plant,
    PlantConfig,
    PvArray,
)
from microgrid.plant._config import ChargeOrder
from microgrid.plant._paths import PathStorage

T0 = datetime(2026, 9, 25, 7, 0, tzinfo=UTC)  # 00:00 PDT
NIGHT = GridCharge(start=time(1), end=time(4), target_soc=0.5)
NOON = GridCharge(start=time(12), end=time(15), target_soc=0.9)


def _starts(count: int, first: datetime = T0) -> list[datetime]:
    return [first + k * SLOT for k in range(count)]


def _plant(
    *windows: GridCharge, order: ChargeOrder = "pv_first", native: NativePlant = EXAMPLE_PLANT
) -> Plant:
    config = config_of(native)
    return Plant(
        replace(
            config,
            dispatch=replace(config.dispatch, grid_charge=windows, charge_order=order),
        )
    )


def test_each_window_tops_the_battery_up() -> None:
    # No PV, a steady house: the night window lifts the battery to half, the noon one
    # to 90 %, and between them the house draws it down.
    plant = _plant(NIGHT, NOON)
    assert isinstance(plant.storage, PathStorage)
    flows = plant.project(_starts(144), [SLOT] * 144, [0.0] * 144, [0.5] * 144, 0.2)
    at = {f.start.astimezone(PACIFIC).time(): f for f in flows}
    assert at[time(3, 50)].soc_end == pytest.approx(0.5, abs=0.01)
    assert at[time(14, 50)].soc_end == pytest.approx(0.9, abs=0.01)
    before_noon = at[time(11, 50)].soc_end
    assert before_noon is not None and before_noon < 0.5
    assert any(f.grid_to_battery > 0 for f in flows if f.start.astimezone(PACIFIC).hour < 4)
    assert any(f.grid_to_battery > 0 for f in flows if 12 <= f.start.astimezone(PACIFIC).hour < 15)


natives = st.builds(
    lambda eta, reserve: NativePlant(
        Battery(10.0, 5.0, 6.0, eta, eta, reserve_soc=reserve, saturation_soc=0.97),
        PACIFIC,
        standby_kw=0.03,
        working_kw=0.06,
        grid_charge=NIGHT,
    ),
    st.floats(0.85, 1.0),
    st.floats(0, 0.2),
)


@settings(max_examples=60)
@given(
    natives,
    st.lists(
        st.tuples(st.floats(0, 8, allow_nan=False), st.floats(0, 5, allow_nan=False)),
        min_size=1,
        max_size=66,  # 00:00 to 11:00: the noon window never comes
    ),
    st.floats(0, 1),
)
def test_a_window_that_never_comes_changes_nothing(
    native: NativePlant, day: list[tuple[float, float]], soc: float
) -> None:
    starts, pv, house = _starts(len(day)), [d[0] for d in day], [d[1] for d in day]
    general = _plant(NIGHT, NOON, native=native).project(starts, [SLOT] * len(day), pv, house, soc)
    today = simulate(native, starts, pv, house, soc, step=SLOT)
    for mine, theirs in zip(general, today, strict=True):
        assert mine.soc_end == pytest.approx(theirs.soc_end, abs=1e-9)
        assert mine.grid_to_battery == pytest.approx(theirs.grid_to_battery, abs=1e-9)
        assert mine.grid_to_load == pytest.approx(theirs.grid_to_load, abs=1e-9)


def test_the_target_first_does_not_wait_for_pv() -> None:
    # The noon window, PV coming that would reach the target by its end: PV first, the
    # grid waits for it; the target first, the grid tops up from the window's start.
    starts = _starts(18, T0 + timedelta(hours=12))
    pv, house = [6.0] * 18, [0.5] * 18
    waits = _plant(NOON).project(starts, [SLOT] * 18, pv, house, 0.5)
    tops = _plant(NOON, order="target_first").project(starts, [SLOT] * 18, pv, house, 0.5)
    assert waits[0].grid_to_battery == 0.0
    assert tops[0].grid_to_battery > 0.0


def test_the_target_first_buys_back_at_the_fill_not_a_top_up() -> None:
    # This site's battery, the noon window at 100 %, SOC 0.9 at 09:00, no PV until noon,
    # then 12 kW that fills the battery and exports. The grid never charges, so a 09:00
    # kWh is bought back at the fill's export, under either order: PV still charges
    # first within a slot.
    first = T0 + timedelta(hours=9)
    starts = _starts(24, first)
    pv = [0.0] * 18 + [12.0] * 6
    house = [0.5] * 24
    window = GridCharge(start=time(12), end=time(15))
    prices_in, prices_out = [0.40] * 24, [0.05] * 24
    for order in ("pv_first", "target_first"):
        plant = _plant(NIGHT, window, order=order)
        storage = plant.storage
        assert storage is not None
        flows = plant.project(starts, [SLOT] * 24, pv, house, 0.9)
        assert all(f.grid_to_battery == 0.0 for f in flows)
        priced = price_flows(storage, flows, prices_in, prices_out, day=24)
        assert priced.make_up_event[0] is Event.FILL, order
        assert priced.make_up[0] == pytest.approx(0.05)


def test_a_windows_cover_stops_at_the_next_window() -> None:
    # No PV, a 0.5 kW house, a night window covering until 18:00 and a noon window: the
    # night window buys for the hours to noon only, and the noon window for after it.
    night = GridCharge(start=time(1), end=time(4), target_soc=1.0, cover_until=time(18))
    alone = _plant(night, GridCharge(start=time(23), end=time(23, 50)))
    with_noon = _plant(night, NOON)
    zeros, house = [0.0] * 144, [0.5] * 144
    bought = {}
    for name, plant in (("alone", alone), ("with noon", with_noon)):
        flows = plant.project(_starts(144), [SLOT] * 144, zeros, house, 0.1)
        night_kwh = sum(
            f.grid_to_battery for f in flows if f.start.astimezone(PACIFIC).hour < 4
        ) * (SLOT / timedelta(hours=1))
        bought[name] = night_kwh
    assert bought["with noon"] < bought["alone"] - 1.0


@pytest.mark.parametrize("cover", [time(13), time(15)])
def test_a_cover_within_its_own_window_is_refused(cover: time) -> None:
    # It would mean the next day, inside the next day's window, which tops up first:
    # today's branch and the general dispatch would read it differently.
    with pytest.raises(ValueError, match="cover_until must not fall within its own window"):
        GridCharge(start=time(12), end=time(15), cover_until=cover)
    GridCharge(start=time(12), end=time(15), cover_until=time(12))  # at its start: fine
    GridCharge(start=time(12), end=time(15), cover_until=time(0))  # this site's


# --- Battery export -------------------------------------------------------------------

EVENING = ExportWindow(start=time(18), end=time(19), power_kw=5.0, floor_soc=0.3)
#: This site's fitted battery, on its own inverter: the worked figures below are on its
#: curve, so these tests keep its numbers.
SITE = Battery(
    16.5,
    8.4,
    9.6,
    0.987,
    0.954,
    loss_charge_per_kw=0.0046,
    loss_discharge_per_kw=0.0015,
    meter_charge=0.987,
    meter_discharge=1.017,
    wear_charge_per_kwh=0.02,
    wear_discharge_per_kwh=0.02,
)


def _exporting(battery: Battery, window: ExportWindow) -> Plant:
    return Plant(
        PlantConfig(
            tz=PACIFIC,
            pv=(PvArray("roof", "ac"),),
            battery=BatteryPart(battery, "ac", inverter=Inverter()),
            dispatch=Dispatch(export_windows=(window,)),
        )
    )


def _evening(plant: Plant, soc: float, house: float = 1.0) -> tuple[SlotFlow, ...]:
    """18:00 PDT, no PV, a steady house, one slot from ``soc``."""
    start = T0 + timedelta(hours=18)
    return plant.project([start], [SLOT], [0.0], [house], soc)


def _floor_in_the_slot(battery: Battery, window: ExportWindow, house: float) -> float:
    """An SOC that reaches ``window``'s floor half way through the slot."""
    out = battery.drawn_by(window.power_kw + house) * (SLOT / timedelta(hours=1)) / 2
    return window.floor_soc + out / battery.capacity_kwh


def test_the_run_ends_at_its_floor_part_way_through_the_slot() -> None:
    battery = Battery(10.0, 8.0, 8.0, 1.0, 1.0, reserve_soc=0.1)
    plant = _exporting(battery, EVENING)
    (flow,) = _evening(plant, _floor_in_the_slot(battery, EVENING, 1.0))
    # Half the slot exporting 5 kW with the house's 1 beside it, then the house alone
    # from below the floor: export 2.5 kW, the house 1, the battery at the floor less
    # the second half's draw.
    assert flow.battery_export == pytest.approx(2.5)
    assert flow.battery_to_load == pytest.approx(1.0)
    assert flow.soc_end == pytest.approx(0.3 - 1.0 / 12 / 10.0)
    end = flow.dispatch.run_end
    assert end is not None
    # Lossless: a stored kWh less ends the run 1/6 h sooner: 5/6 export lost, the
    # house's 1/6 carried (the floor is above the reserve), nothing imported.
    assert end.export_lost == pytest.approx(5 / 6, rel=1e-6)
    assert end.not_delivered == pytest.approx(5 / 6, rel=1e-6)
    assert end.carried == pytest.approx(1 / 6, rel=1e-6)
    assert end.imported == pytest.approx(0.0, abs=1e-6)


def test_at_the_reserve_the_house_imports_what_the_run_gave_it() -> None:
    battery = Battery(10.0, 8.0, 8.0, 1.0, 1.0, reserve_soc=0.3)
    plant = _exporting(battery, EVENING)
    (flow,) = _evening(plant, _floor_in_the_slot(battery, EVENING, 1.0))
    end = flow.dispatch.run_end
    assert end is not None
    assert end.imported == pytest.approx(1 / 6, rel=1e-6)
    assert end.not_delivered == pytest.approx(1.0, rel=1e-6)
    assert end.carried == pytest.approx(0.0, abs=1e-6)


def test_the_run_end_with_the_house_beside_it_on_this_sites_curve() -> None:
    # E = 5, h = 1 on this site's fitted battery: D(6) = 6.3506 stored kW, so a stored
    # kWh less loses E / D(6) = 0.7873 AC of export and carries D(1) / D(6) = 0.1653.
    plant = _exporting(SITE, EVENING)
    (flow,) = _evening(plant, _floor_in_the_slot(SITE, EVENING, 1.0))
    end = flow.dispatch.run_end
    assert end is not None
    assert end.export_lost == pytest.approx(0.7873, abs=1e-3)
    assert end.carried == pytest.approx(0.1653, abs=1e-3)


def test_no_run_end_where_the_run_goes_on_or_never_starts() -> None:
    battery = Battery(10.0, 8.0, 8.0, 1.0, 1.0, reserve_soc=0.1)
    plant = _exporting(battery, EVENING)
    (lasting,) = _evening(plant, 0.9)
    assert lasting.battery_export == pytest.approx(5.0) and lasting.dispatch.run_end is None
    (below,) = _evening(plant, 0.25)
    assert below.battery_export == 0.0 and below.dispatch.run_end is None


def test_the_export_event_prices_a_kwh_by_the_run_end() -> None:
    # A kWh at 17:00, the battery charging from PV; the run ends at 18:00 with the floor
    # at the reserve: v = export lost x p_exp + the house's import - the wear saved.
    battery = replace(SITE, reserve_soc=0.3)
    plant = _exporting(battery, EVENING)
    soc = _floor_in_the_slot(battery, EVENING, 1.0)
    starts = [T0 + timedelta(hours=17), T0 + timedelta(hours=18)]
    flows = plant.project(starts, [SLOT] * 2, [2.0, 0.0], [1.0, 1.0], soc)
    assert flows[1].dispatch.run_end is not None
    storage = plant.storage
    assert storage is not None
    priced = price_flows(storage, flows, [0.4, 0.6], [0.1, 1.236], day=2)
    assert priced.make_up_event[0] is Event.EXPORT
    end = flows[1].dispatch.run_end
    expected = (
        end.export_lost * 1.236
        + end.imported * 0.4
        - end.not_delivered * battery.wear_discharge_per_kwh
        + end.refilled * battery.wear_charge_per_kwh
    )
    assert priced.stored_value[0] == pytest.approx(expected)
    assert not priced.spilled[0]


@pytest.mark.parametrize(("floor", "soc", "share"), [(0.0, 0.05, 0.5), (0.02, 0.03, 0.1)])
def test_a_low_floor_splits_the_slot_on_what_is_stored(
    floor: float, soc: float, share: float
) -> None:
    # A lossless 10 kWh battery, the reserve 0, 5 kW export and the house 1 drawing 1 kWh
    # a slot. The run takes the share of the slot that what is above the floor allows,
    # and never delivers more than it held.
    battery = Battery(10.0, 8.0, 8.0, 1.0, 1.0)
    plant = _exporting(battery, replace(EVENING, floor_soc=floor))
    (flow,) = _evening(plant, soc)
    hours = SLOT / timedelta(hours=1)
    assert flow.battery_export == pytest.approx(5.0 * share)
    delivered = (flow.battery_export + flow.battery_to_load) * hours
    assert delivered <= (soc - 0.0) * battery.capacity_kwh + 1e-9
    assert flow.dispatch.run_end is not None
    assert 0 < flow.dispatch.run_end.export_lost <= 5 / 6 + 1e-6
    assert 0 < flow.dispatch.run_end.not_delivered <= 1.0 + 1e-6


def test_a_window_that_reached_its_floor_does_not_export_again() -> None:
    # 5 kW export with PV yielding at a 5 kW limit, 2 kW PV, no house. Once at its floor
    # the window stops: PV charges the battery, which does not run up and export again
    # before the window ends.
    battery = Battery(10.0, 8.0, 8.0, 1.0, 1.0)
    plant = Plant(
        PlantConfig(
            tz=PACIFIC,
            pv=(PvArray("roof", "ac"),),
            battery=BatteryPart(battery, "ac", inverter=Inverter()),
            grid=GridLimits(export_limit_kw=5.0),
            dispatch=Dispatch(export_windows=(replace(EVENING, yields="pv"),)),
        )
    )
    starts = [T0 + timedelta(hours=18) + k * SLOT for k in range(6)]
    soc = _floor_in_the_slot(battery, EVENING, 0.0)
    flows = plant.project(starts, [SLOT] * 6, [2.0] * 6, [0.0] * 6, soc)
    assert flows[0].dispatch.run_end is not None
    assert all(f.battery_export == 0.0 and f.dispatch.run_end is None for f in flows[1:])
    assert all(f.pv_to_battery > 0 for f in flows[1:])


def test_the_carried_share_is_priced_by_the_next_event() -> None:
    # The floor above the reserve: the house's share of a stored kWh carries past the
    # run's end to the battery's empty later that night, priced from the kWh's slot.
    battery = Battery(10.0, 8.0, 8.0, 1.0, 1.0, reserve_soc=0.1)
    window = replace(EVENING, floor_soc=0.3)
    plant = _exporting(battery, window)
    soc = _floor_in_the_slot(battery, window, 1.0)
    count = 30
    starts = [T0 + timedelta(hours=17, minutes=50) + k * SLOT for k in range(count)]
    flows = plant.project(starts, [SLOT] * count, [0.0] * count, [1.0] * count, soc)
    end_at = next(i for i, f in enumerate(flows) if f.dispatch.run_end is not None)
    empty = next(
        i
        for i, f in enumerate(flows)
        if i > end_at
        and f.grid_to_load > 0.001
        and f.soc_end is not None
        and f.soc_end <= 0.1 + 1e-6
    )
    p_import = [0.4] * count
    p_import[3] = 0.25  # the cheapest hour before the empty, after the kWh's slot
    p_export = [1.236] * count
    storage = plant.storage
    assert storage is not None
    priced = price_flows(storage, flows, p_import, p_export, day=count)
    assert priced.make_up_event[0] is Event.EXPORT
    end = flows[end_at].dispatch.run_end
    assert end is not None and end.carried > 0.1
    v_next = storage.displaced_value(min(p_import[0 : empty + 1]), flows[empty])
    expected = (
        end.export_lost * p_export[end_at]
        - end.not_delivered * battery.wear_discharge_per_kwh
        + end.carried * v_next
    )
    assert priced.stored_value[0] == pytest.approx(expected)
