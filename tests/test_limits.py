"""The grid's import and export limits: the dispatch at the import limit, unserved load,
and the price rule there; and curtailment first under an export limit."""

from dataclasses import replace
from datetime import UTC, datetime, time, timedelta

import pytest

from engine_fixtures import NOW, PACIFIC, inputs
from microgrid.engine import Engine, EngineConfig, Pricing, Regime, Served
from microgrid.engine._supply import supply_stack
from microgrid.engine._value import _spill_value
from microgrid.engine.extend import MODEL, ChargeSource, Event, SlotFlow, price_flows
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

T0 = datetime(2026, 9, 25, 7, 0, tzinfo=UTC)  # 00:00 PDT
LIMIT = 5.0
LOSSLESS = Battery(10.0, 8.0, 9.6, 1.0, 1.0, reserve_soc=0.1)


def _plant(
    battery: Battery | None = LOSSLESS, window: GridCharge | None = None, limit: float = LIMIT
) -> Plant:
    return Plant(
        PlantConfig(
            tz=PACIFIC,
            pv=(PvArray("roof", "ac"),),
            battery=None if battery is None else BatteryPart(battery, "ac", inverter=Inverter()),
            grid=GridLimits(import_limit_kw=limit),
            dispatch=Dispatch(grid_charge=() if window is None else (window,)),
        )
    )


def _at(hour: int, count: int = 1) -> list[datetime]:
    return [T0 + timedelta(hours=hour) + k * SLOT for k in range(count)]


def test_the_limit_caps_the_grids_charge_first() -> None:
    # In the window, the house 3 kW on the grid; the top-up gets what the 5 kW
    # limit leaves, 2 kW.
    window = GridCharge(start=time(12), end=time(15), target_soc=1.0)
    (flow,) = _plant(window=window).project(_at(12), [SLOT], [0.0], [3.0], 0.2)
    assert flow.grid_to_load == pytest.approx(3.0)
    assert flow.grid_to_battery == pytest.approx(2.0)
    assert flow.grid_to_load + flow.grid_to_battery <= LIMIT + 1e-9


def test_above_the_limit_the_battery_serves_beyond_its_policy() -> None:
    # The house 7 kW, held in the window (it would be on the grid): the battery
    # gives the 2 kW the limit leaves, down to its reserve.
    window = GridCharge(start=time(12), end=time(15), target_soc=1.0)
    (flow,) = _plant(window=window).project(_at(12), [SLOT], [0.0], [7.0], 0.4)
    assert flow.dispatch.held
    assert flow.grid_to_load == pytest.approx(LIMIT)
    assert flow.battery_to_load == pytest.approx(2.0)
    assert flow.grid_to_battery == 0.0 and flow.unserved == 0.0


def test_what_neither_serves_is_unserved() -> None:
    # The battery at its reserve, the house 7: 2 kW unserved.
    (flow,) = _plant().project(_at(18), [SLOT], [0.0], [7.0], 0.1)
    assert flow.grid_to_load == pytest.approx(LIMIT)
    assert flow.unserved == pytest.approx(2.0)
    # Without a battery the same.
    (bare,) = _plant(None).project(_at(18), [SLOT], [0.0], [7.0], None)
    assert (bare.grid_to_load, bare.unserved) == pytest.approx((LIMIT, 2.0))


def _priced(flows: list[SlotFlow], plant: Plant, p_import: list[float]) -> object:
    storage = plant.storage
    return price_flows(
        storage, flows, p_import, [0.05] * len(flows), day=len(flows), import_limit_kw=LIMIT
    )


def test_the_price_at_the_limit_follows_what_serves() -> None:
    plant = _plant()
    storage = plant.storage
    assert storage is not None
    # Unserved: the import price as a floor, the event named.
    unserved = SlotFlow(start=T0, soc_start=0.1, soc_end=0.1, grid_to_load=LIMIT, unserved=2.0)
    priced = price_flows(storage, [unserved], [0.592], [0.05], day=1, import_limit_kw=LIMIT)
    assert priced.prices == (0.592,) and priced.events == (Event.UNSERVED,)
    assert priced.served == (Served.GRID,)
    # The grid's charge diverted: the band's charge side, served by the grid.
    charging = SlotFlow(
        start=T0, soc_start=0.5, soc_end=0.52, grid_to_load=3.0, grid_to_battery=2.0
    )
    priced = price_flows(storage, [charging], [0.4], [0.05], day=1, import_limit_kw=LIMIT)
    low, _ = storage.dead_band(priced.stored_value[0], charging)
    assert priced.served == (Served.GRID,) and priced.prices[0] == pytest.approx(low)
    # The battery with more to give: its band's discharge side.
    giving = SlotFlow(
        start=T0, soc_start=0.5, soc_end=0.48, grid_to_load=LIMIT, battery_to_load=2.0
    )
    priced = price_flows(storage, [giving], [0.4], [0.05], day=1, import_limit_kw=LIMIT)
    _, high = storage.dead_band(priced.stored_value[0], giving)
    assert priced.served == (Served.BATTERY,) and priced.prices[0] == pytest.approx(high)
    # Below the limit nothing changes.
    under = SlotFlow(start=T0, soc_start=0.5, soc_end=0.5, grid_to_load=1.0)
    assert price_flows(storage, [under], [0.4], [0.05], day=1, import_limit_kw=LIMIT) == (
        price_flows(storage, [under], [0.4], [0.05], day=1)
    )


def test_an_empty_into_unserved_load_is_the_floor_not_an_earlier_import() -> None:
    # A kWh at 15:00 empties the battery at 19:00 into unserved load: it costs
    # the 19:00 import (0.592) as a floor, not the cheapest import before (0.430).
    plant = _plant()
    storage = plant.storage
    assert storage is not None
    flows = [
        SlotFlow(start=T0, soc_start=0.5, soc_end=0.45, battery_to_load=2.0),
        SlotFlow(start=T0 + SLOT, soc_start=0.45, soc_end=0.4, battery_to_load=2.0),
        SlotFlow(start=T0 + 2 * SLOT, soc_start=0.1, soc_end=0.1, grid_to_load=LIMIT, unserved=1.0),
    ]
    priced = price_flows(
        storage, flows, [0.43, 0.43, 0.592], [0.05] * 3, day=3, import_limit_kw=LIMIT
    )
    assert priced.make_up_event[0] is Event.UNSERVED
    assert priced.stored_value[0] == pytest.approx(storage.displaced_value(0.592, flows[2]))


def test_the_stack_at_the_limit_has_no_grid_room() -> None:
    plant = _plant()
    storage = plant.storage
    assert storage is not None
    charging = SlotFlow(
        start=T0, soc_start=0.5, soc_end=0.52, grid_to_load=3.0, grid_to_battery=2.0
    )
    steps = supply_stack(
        storage,
        charging,
        1 / 6,
        served=Served.GRID,
        price=0.3,
        p_import=0.4,
        stored_value=0.4,
        import_limit_kw=LIMIT,
    )
    assert [step.source for step in steps] == [Served.GRID, Served.BATTERY]
    assert steps[0].kw == pytest.approx(2.0)
    assert all(step.kw is not None for step in steps)  # no unlimited grid
    unserved = SlotFlow(start=T0, soc_start=0.1, soc_end=0.1, grid_to_load=LIMIT, unserved=2.0)
    only = supply_stack(
        storage,
        unserved,
        1 / 6,
        served=Served.GRID,
        price=0.592,
        p_import=0.592,
        stored_value=0.4,
        import_limit_kw=LIMIT,
    )
    assert only == (only[0],) and only[0].kw == 0.0 and only[0].per_kwh == 0.592


def test_the_engine_weighs_unserved_slots_and_their_regime() -> None:
    # Grid only, a 0.5 kW limit and a 0.7 kW house: every slot is unserved, at the
    # import as a floor, its regime import, and weighed 1.
    plant = Plant(PlantConfig(tz=PACIFIC, grid=GridLimits(import_limit_kw=0.5)))
    valuation = Engine(EngineConfig(plant=plant, pricing=Pricing(scenarios=3))).value(
        inputs(pv=None, soc=None, dispatch_as_modelled=None), at=NOW
    )
    assert valuation is not None
    slot = valuation.slots[0]
    assert slot.unserved == 1.0 and slot.regime is Regime.IMPORT
    assert slot.marginal_cost == slot.p_import


def test_curtailed_pv_is_taken_first() -> None:
    # Above an export limit a kWh more takes the curtailed PV, worth nothing, not the
    # energy-weighted blend of export and curtailment: the export is the lesser of the
    # spare PV and the limit, so a kWh less of spare comes off the curtailment first.
    curtailing = SlotFlow(start=NOW, soc_start=0.5, soc_end=0.5, pv_export=2.0, pv_curtail=1.0)
    assert _spill_value(curtailing, 0.1) == 0.0
    exporting = SlotFlow(start=NOW, soc_start=0.5, soc_end=0.5, pv_export=2.0)
    assert _spill_value(exporting, 0.1) == pytest.approx(0.1)
    assert MODEL.spill_kw < 1.0


def test_the_storage_knows_the_grid_cannot_buy_at_the_limit() -> None:
    # The window's 4 kW house and a 1 kW top-up fill the 5 kW limit:
    # the grid cannot buy more, so a kWh before the window is not bought back there.
    window = GridCharge(start=time(12), end=time(15), target_soc=1.0)
    plant = _plant(window=window)
    storage = plant.storage
    assert storage is not None
    at_limit = SlotFlow(
        start=T0 + timedelta(hours=12),
        soc_start=0.3,
        soc_end=0.32,
        grid_to_load=4.0,
        grid_to_battery=1.0,
    )
    assert not storage.can_charge_more(at_limit, ChargeSource.GRID, margin_kw=1e-9)
    under = replace(at_limit, grid_to_load=2.0)
    assert storage.can_charge_more(under, ChargeSource.GRID, margin_kw=1e-9)
    starts = [T0 + timedelta(hours=11, minutes=50) + k * SLOT for k in range(20)]
    flows = plant.project(starts, [SLOT] * 20, [0.0] * 20, [4.0] * 20, 0.3)
    priced = price_flows(storage, flows, [0.4] * 20, [0.05] * 20, day=20, import_limit_kw=LIMIT)
    assert priced.make_up_event[0] is not Event.TOPUP


def test_importing_at_the_limit_is_not_covered() -> None:
    # The battery at its reserve, a 3 kW limit, the house 6 kW
    # through the expensive hours: they import, so they are not covered.
    plant = _plant(limit=3.0)
    storage = plant.storage
    assert storage is not None
    flows = [
        SlotFlow(start=T0 + k * SLOT, soc_start=0.1, soc_end=0.1, grid_to_load=3.0, unserved=3.0)
        for k in range(6)
    ]
    cheap = [SlotFlow(start=T0 + (6 + k) * SLOT, soc_start=0.1, soc_end=0.1) for k in range(6)]
    priced = price_flows(
        storage, flows + cheap, [0.6] * 6 + [0.3] * 6, [0.05] * 12, day=12, import_limit_kw=3.0
    )
    assert not any(priced.covered[:6])


def test_the_export_gives_way_to_the_house() -> None:
    # A 5 kW limit, a 5 kW export window, the battery able to give
    # 8, the house 10: the house takes the battery before any of it goes unserved.
    battery = Battery(10.0, 8.0, 8.0, 1.0, 1.0, reserve_soc=0.1)
    plant = Plant(
        PlantConfig(
            tz=PACIFIC,
            pv=(PvArray("roof", "ac"),),
            battery=BatteryPart(battery, "ac", inverter=Inverter()),
            grid=GridLimits(import_limit_kw=LIMIT, export_limit_kw=6.0),
            dispatch=Dispatch(
                export_windows=(ExportWindow(time(18), time(19), power_kw=5.0, floor_soc=0.2),)
            ),
        )
    )
    (flow,) = plant.project(_at(18), [SLOT], [0.0], [10.0], 0.9)
    # The meter nets export against import: the house takes all 8 kW the battery
    # gives, imports the other 2, and nothing is exported, not 3 kW exported beside 5
    # imported, which no meter sees.
    assert flow.unserved == pytest.approx(0.0, abs=1e-9)
    assert flow.battery_to_load == pytest.approx(8.0)
    assert flow.battery_export == pytest.approx(0.0)
    assert flow.grid_to_load == pytest.approx(2.0)


def test_the_cap_holds_on_a_dc_bus_netted() -> None:
    # A folded battery with DC PV on its bus, DC PV serving the house while the grid
    # tops up: the meter's import, netted, stays within the limit.
    site = Battery(16.5, 8.4, 9.6, 0.987, 0.954, meter_charge=0.987, meter_discharge=1.017)
    window = GridCharge(start=time(12), end=time(15), target_soc=1.0)
    plant = Plant(
        PlantConfig(
            tz=PACIFIC,
            pv=(PvArray("strings", "dc"),),
            battery=BatteryPart(site, "dc_bus", dc_charge_kw=8.0, dc_discharge_kw=9.0),
            hybrid=Inverter(ac_rating_kw=8.0),
            grid=GridLimits(import_limit_kw=3.0),
            dispatch=Dispatch(grid_charge=(window,)),
        )
    )
    flows = plant.project(_at(12, 6), [SLOT] * 6, [2.0] * 6, [3.0] * 6, 0.2)
    for flow in flows:
        assert flow.grid_to_load + flow.grid_to_battery <= 3.0 + 1e-9
    assert any(flow.grid_to_battery > 0 for flow in flows)


def test_a_late_top_up_starts_earlier_under_the_cap() -> None:
    # Late timing leaves the top-up to what the rest of the window could still add; a
    # cap leaves less room ahead, so it starts sooner.
    window = GridCharge(start=time(12), end=time(15), target_soc=1.0, timing="late")
    starts = _at(12, 18)

    def first_top_up(limit: float) -> int:
        flows = _plant(window=window, limit=limit).project(
            starts, [SLOT] * 18, [0.0] * 18, [1.0] * 18, 0.2
        )
        return next(i for i, f in enumerate(flows) if f.grid_to_battery > 1e-9)

    assert first_top_up(3.0) < first_top_up(100.0)
