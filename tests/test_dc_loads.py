"""Loads by connection point: a DC-bus load served by DC PV on its bus, then the battery
there, then the AC side through the hybrid within its rating. Constructed cases,
lossless so the arithmetic is kW in and out; the generated plants are
``test_paths.py``'s."""

from dataclasses import replace
from datetime import UTC, datetime, time, timedelta

import pytest

from engine_fixtures import (
    NOW,
    PACIFIC,
    inputs,
    live_reading,
    load,
    load_forecast,
    load_starts,
    reading,
    received,
    rerun_cost,
)
from microgrid.engine import (
    Cause,
    Engine,
    EngineConfig,
    Pricing,
    Regime,
    Served,
    ValuationKind,
)
from microgrid.engine._live import revalue
from microgrid.engine._value import value_energy
from microgrid.engine.extend import Event, SlotFlow, price_flows
from microgrid.engine.series import SLOT, Forecast, Series
from microgrid.plant import (
    Battery,
    BatteryPart,
    Conversion,
    Dispatch,
    ExportWindow,
    GridCharge,
    GridLimits,
    HoldRule,
    Inverter,
    Load,
    Plant,
    PlantConfig,
    PlantConfigError,
    PvArray,
)
from microgrid.plant.flows import regimes, surplus

T0 = datetime(2026, 9, 25, 7, 0, tzinfo=UTC)  # 00:00 PDT
LOSSLESS = Conversion(eta_in=1.0, eta_out=1.0)
#: A battery with no loss: its kW in and out are stored kW.
CLEAN = Battery(10.0, 20.0, 20.0, 1.0, 1.0)
LOADS = (Load("house"), Load("ev", "dc_bus"))


def _at(hour: int, count: int = 1) -> list[datetime]:
    return [T0 + timedelta(hours=hour) + k * SLOT for k in range(count)]


def _folded(
    *,
    rating: float = 10.0,
    pv: tuple[PvArray, ...] = (PvArray("strings", "dc"),),
    grid: GridLimits | None = None,
    dispatch: Dispatch | None = None,
) -> Plant:
    """A lossless battery folded on the hybrid's DC bus, with the DC-bus load beside it."""
    return Plant(
        PlantConfig(
            tz=PACIFIC,
            pv=pv,
            battery=BatteryPart(CLEAN, "dc_bus", dc_charge_kw=10.0, dc_discharge_kw=10.0),
            hybrid=Inverter(ac_rating_kw=rating),
            grid=grid or GridLimits(),
            loads=LOADS,
            dispatch=dispatch or Dispatch(),
        )
    )


def _one(plant: Plant, pv: float, house: float, ev: float, soc: float | None) -> SlotFlow:
    (flow,) = plant.project(_at(10), [SLOT], [pv], [house], soc, dc_load_kw=[ev])
    return flow


def test_dc_pv_serves_the_dc_load_on_its_bus_first() -> None:
    # 5 kW of DC PV: 3 to the DC-bus load as it is, 1 across to the house, 1 into the battery.
    flow = _one(_folded(), pv=5.0, house=1.0, ev=3.0, soc=0.5)
    assert flow.dc_pv_to_dc_load == pytest.approx(3.0)
    assert flow.dc_pv_to_ac == pytest.approx(1.0)
    assert flow.pv_to_load == pytest.approx(1.0)
    assert flow.dc_pv_to_battery == pytest.approx(1.0)
    assert flow.grid_to_load == pytest.approx(0.0)


def test_ac_pv_serves_the_dc_load_through_the_hybrid() -> None:
    # 4 kW of AC PV: 1 to the house, 2 rectified to the DC-bus load, 1 exported.
    plant = Plant(
        PlantConfig(
            tz=PACIFIC,
            pv=(PvArray("roof", "ac"),),
            hybrid=Inverter(conversion=LOSSLESS),
            loads=LOADS,
        )
    )
    flow = _one(plant, pv=4.0, house=1.0, ev=2.0, soc=None)
    assert flow.pv_to_load == pytest.approx(3.0)  # the AC the hybrid takes is in it
    assert flow.ac_to_dc_load == pytest.approx(2.0)
    assert flow.dispatch.crossing == pytest.approx(-2.0)
    assert flow.pv_export == pytest.approx(1.0)


def test_the_battery_on_the_bus_serves_the_dc_load_as_it_is() -> None:
    # No PV: the battery gives the DC-bus load 4 kW on its bus and the house 2 across.
    flow = _one(_folded(pv=()), pv=0.0, house=2.0, ev=4.0, soc=0.5)
    assert flow.battery_to_dc_load == pytest.approx(4.0)
    assert flow.battery_to_load == pytest.approx(2.0)
    assert flow.dispatch.battery_port == pytest.approx(-6.0)
    assert flow.dispatch.crossing == pytest.approx(2.0)  # only the house's share crosses
    assert flow.grid_to_load == pytest.approx(0.0)


def test_a_battery_on_its_own_inverter_serves_the_dc_load_through_the_hybrid() -> None:
    plant = Plant(
        PlantConfig(
            tz=PACIFIC,
            battery=BatteryPart(CLEAN, "ac", inverter=Inverter()),
            hybrid=Inverter(conversion=LOSSLESS),
            loads=LOADS,
        )
    )
    flow = _one(plant, pv=0.0, house=2.0, ev=3.0, soc=0.5)
    assert flow.battery_to_load == pytest.approx(5.0)  # its AC, the hybrid's share in it
    assert flow.ac_to_dc_load == pytest.approx(3.0)
    assert flow.battery_to_dc_load == 0.0
    assert flow.grid_to_load == pytest.approx(0.0)


def test_the_hybrids_rating_leaves_a_dc_load_unserved() -> None:
    # The grid reaches the DC-bus load only through the hybrid: 3 kW of its 5.
    plant = Plant(
        PlantConfig(
            tz=PACIFIC,
            hybrid=Inverter(ac_rating_kw=3.0, conversion=LOSSLESS),
            loads=LOADS,
        )
    )
    flow = _one(plant, pv=0.0, house=1.0, ev=5.0, soc=None)
    assert flow.grid_to_load == pytest.approx(4.0)
    assert flow.ac_to_dc_load == pytest.approx(3.0)
    assert flow.unserved == pytest.approx(2.0)


def test_a_top_up_leaves_the_loads_their_share_of_the_import_limit() -> None:
    # A 5 kW limit: the house 2 and the DC-bus load 2 through the hybrid, the top-up the 1 left.
    window = GridCharge(start=time(10), end=time(12), target_soc=1.0)
    plant = _folded(
        pv=(), grid=GridLimits(import_limit_kw=5.0), dispatch=Dispatch(grid_charge=(window,))
    )
    (flow,) = plant.project(_at(10), [SLOT], [0.0], [2.0], 0.2, dc_load_kw=[2.0])
    assert flow.dispatch.held
    assert flow.ac_to_dc_load == pytest.approx(2.0)
    assert flow.grid_to_battery == pytest.approx(1.0)
    assert flow.grid_to_load + flow.grid_to_battery == pytest.approx(5.0)


def test_no_dc_load_is_a_dc_load_of_zero() -> None:
    plant = _folded()
    starts = _at(0, 96)
    pv = [max(0.0, 6.0 - abs(k - 48) / 6) for k in range(96)]
    house = [1.0 + (k % 7) / 5 for k in range(96)]
    assert plant.project(starts, [SLOT] * 96, pv, house, 0.4) == plant.project(
        starts, [SLOT] * 96, pv, house, 0.4, dc_load_kw=[0.0] * 96
    )


def test_a_plant_without_a_dc_bus_load_draws_none() -> None:
    plant = Plant(PlantConfig(tz=PACIFIC, pv=(PvArray("roof", "ac"),)))
    assert not plant.has_dc_loads
    with pytest.raises(ValueError, match="no DC-bus load"):
        plant.project(_at(10), [SLOT], [1.0], [1.0], None, dc_load_kw=[1.0])
    plant.project(_at(10), [SLOT], [1.0], [1.0], None, dc_load_kw=[0.0])


# --- Through the engine ---------------------------------------------------------------

#: A neutral battery, folded on the hybrid's DC bus with a DC-bus load beside it.
SITE = Battery(
    12.0,
    6.0,
    6.0,
    0.96,
    0.96,
    loss_charge_per_kw=0.003,
    loss_discharge_per_kw=0.003,
    meter_charge=0.99,
    meter_discharge=1.0,
    wear_charge_per_kwh=0.02,
    wear_discharge_per_kwh=0.02,
)
EV_PLANT = Plant(
    PlantConfig(
        tz=PACIFIC,
        pv=(PvArray("roof", "ac"),),
        battery=BatteryPart(SITE, "dc_bus", dc_charge_kw=6.3, dc_discharge_kw=6.2),
        hybrid=Inverter(ac_rating_kw=10.0, standby_kw=0.03, working_kw=0.06),
        loads=LOADS,
    )
)


def _engine(plant: Plant = EV_PLANT) -> Engine:
    return Engine(
        EngineConfig(
            plant=plant,
            pricing=Pricing(scenarios=3),
            smoothing=timedelta(0),
        )
    )


def _evening_kw(kw: float) -> list[float | None]:
    """``kw`` from 18:00 to 21:00 local on :func:`load_starts`, nothing else."""
    return [kw if 18 <= start.astimezone(PACIFIC).hour < 21 else 0.0 for start in load_starts()]


def _evening_ev(kw: float) -> Series[Forecast[float]]:
    """7 kW from 18:00 to 21:00 local, nothing else."""
    return received(load_forecast(load_starts(), _evening_kw(kw)))


def test_the_engine_values_a_plant_with_a_dc_bus_load() -> None:
    engine = _engine()
    idle = engine.value(inputs(dc_load=_evening_ev(0.0)), at=NOW)
    charging = engine.value(inputs(dc_load=_evening_ev(7.0)), at=NOW)
    assert idle is not None and charging is not None
    assert charging.kind is ValuationKind.STORED_VALUE_V1 and charging.degraded == ()
    assert charging.model is not None and charging.model.dc_load_kw is not None
    # The DC-bus load drains the battery in the evening, so a kWh later is imported sooner.
    late = [i for i, start in enumerate(charging.starts) if start.astimezone(PACIFIC).hour == 23]
    assert charging.slots[late[0]].marginal_cost > idle.slots[late[0]].marginal_cost


def test_a_missing_dc_bus_load_forecast_is_the_load_forecast_cause() -> None:
    valuation = _engine().value(inputs(), at=NOW)
    assert valuation is not None
    assert valuation.kind is ValuationKind.FALLBACK_IMPORT
    assert Cause.LOAD_FORECAST in valuation.degraded


def test_a_plant_without_a_dc_bus_load_takes_no_forecast_or_reading_of_one() -> None:
    plant = Plant(PlantConfig(tz=PACIFIC, pv=(PvArray("roof", "ac"),)))
    engine = _engine(plant)
    with pytest.raises(ValueError, match="no DC-bus load"):
        engine.value(
            inputs(soc=None, dispatch_as_modelled=None, dc_load=load(kw=1.0)),
            at=NOW,
        )
    with pytest.raises(ValueError, match="not the plant's meters"):
        engine.live_session().update(
            live_reading(soc=None, dispatch_as_modelled=None, dc_load_kw=1.0), None, None
        )


def test_the_live_value_reads_the_dc_bus_load() -> None:
    engine = _engine()
    valuation = engine.value(inputs(dc_load=_evening_ev(0.0)), at=NOW)
    assert valuation is not None
    until = NOW + timedelta(hours=1)
    missing = engine.live_session().update(live_reading(), valuation, until)
    assert missing is not None and Cause.LOAD_POWER in missing.degraded
    # The battery full at noon: 6 kW of PV spills past the house, until the DC-bus load takes it.
    read = engine.live_session().update(live_reading(dc_load_kw=0.0), valuation, until)
    assert read is not None and read.degraded == () and read.surplus_kw > 4.0
    busy = engine.live_session().update(live_reading(dc_load_kw=7.0), valuation, until)
    assert busy is not None and busy.surplus_kw == 0.0


# --- Hold rules ------------------------------------------------------------------------

HOLD = HoldRule(dc_load_above_kw=1.0, start_below_soc=0.65)


def _held_plant(**dispatch: object) -> Plant:
    return _folded(pv=(), dispatch=Dispatch(hold_rules=(HOLD,), **dispatch))  # type: ignore[arg-type]


def _session(plant: Plant, soc: float, ev: list[float]) -> tuple[SlotFlow, ...]:
    count = len(ev)
    return plant.project(
        _at(18, count), [SLOT] * count, [0.0] * count, [1.0] * count, soc, dc_load_kw=ev
    )


def test_a_session_that_starts_below_the_rule_holds_the_battery() -> None:
    flows = _session(_held_plant(), 0.5, [0.0, 7.0, 7.0, 7.0, 0.0])
    assert flows[0].battery_to_load > 0 and not flows[0].dispatch.held  # before: self-consumption
    for flow in flows[1:4]:
        assert flow.dispatch.held
        assert flow.battery_out == 0.0
        assert flow.ac_to_dc_load == pytest.approx(7.0)  # the grid, through the hybrid
        assert flow.grid_to_load == pytest.approx(8.0)
    assert not flows[4].dispatch.held and flows[4].battery_to_load > 0  # after: again


def test_a_session_that_starts_above_the_rule_drains_the_battery_throughout() -> None:
    # It starts at 70 %, and goes on draining below 65 %: the start decides.
    flows = _session(_held_plant(), 0.70, [7.0] * 3)
    assert all(not flow.dispatch.held and flow.battery_to_dc_load > 0 for flow in flows)
    last = flows[-1].soc_end
    assert last is not None and last < 0.65


def test_a_held_session_does_not_export() -> None:
    evening = ExportWindow(time(18), time(20), power_kw=3.0, floor_soc=0.2)
    flows = _session(_held_plant(export_windows=(evening,)), 0.5, [0.0, 7.0, 0.0])
    assert flows[0].battery_export == pytest.approx(3.0)
    assert flows[1].dispatch.held and flows[1].battery_export == 0.0
    assert flows[2].battery_export == pytest.approx(3.0)  # the window goes on after it


def test_a_kwh_in_a_held_slot_is_imported() -> None:
    plant = _held_plant()
    flows = _session(plant, 0.5, [0.0, 7.0, 7.0, 0.0])
    priced = price_flows(plant.storage, flows, [0.4] * 4, [0.05] * 4, day=4)
    assert priced.served[1] is Served.GRID and priced.prices[1] == 0.4


def test_hold_rules_round_trip_and_are_checked() -> None:
    config = PlantConfig(
        tz=PACIFIC,
        battery=BatteryPart(CLEAN, "dc_bus", dc_charge_kw=10.0, dc_discharge_kw=10.0),
        hybrid=Inverter(ac_rating_kw=10.0),
        loads=LOADS,
        dispatch=Dispatch(hold_rules=(HOLD,)),
    )
    mapping = config.to_mapping()
    assert mapping["dispatch"]["hold_rules"] == [  # type: ignore[index]
        {"dc_load_above_kw": 1.0, "start_below_soc": 0.65}
    ]
    assert PlantConfig.from_mapping(mapping, tz=PACIFIC) == config
    with pytest.raises(PlantConfigError) as raised:
        PlantConfig(
            tz=PACIFIC,
            pv=(PvArray("roof", "ac"),),
            dispatch=Dispatch(hold_rules=(HoldRule(-1.0, 0.0),)),
        )
    assert set(raised.value.errors) == {
        "dispatch.hold_rules[0].dc_load_above_kw: must not be negative",
        "dispatch.hold_rules[0].start_below_soc: must be in (0, 1]",
    }
    with pytest.raises(PlantConfigError) as raised:
        PlantConfig(tz=PACIFIC, pv=(PvArray("roof", "ac"),), dispatch=Dispatch(hold_rules=(HOLD,)))
    assert set(raised.value.errors) == {
        "dispatch.hold_rules[0]: a hold rule needs a battery",
        "dispatch.hold_rules[0]: a hold rule needs a DC-bus load (its session is one's draw)",
    }
    low = Battery(10.0, 20.0, 20.0, 1.0, 1.0, reserve_soc=0.7)
    with pytest.raises(PlantConfigError, match="start_below_soc: must be above the battery's"):
        replace(config, battery=BatteryPart(low, "dc_bus", dc_charge_kw=10.0, dc_discharge_kw=10.0))
    bad = {**mapping, "dispatch": {"hold_rules": [{"dc_load_above_kw": 1.0}]}}
    with pytest.raises(PlantConfigError, match=r"hold_rules\[0\]\.start_below_soc"):
        PlantConfig.from_mapping(bad, tz=PACIFIC)


# --- Edge cases ------------------------------------------------------------------------


def test_the_live_warm_up_with_a_dc_bus_load_is_smoothing() -> None:
    # Smoothing on, the DC-bus load read: the warm-up publishes the issue's price, as a
    # plant without one does, not the load_power fallback.
    engine = Engine(EngineConfig(plant=EV_PLANT, pricing=Pricing(scenarios=3)))
    valuation = engine.value(inputs(dc_load=_evening_ev(0.0)), at=NOW)
    assert valuation is not None
    value = engine.live_session().update(
        live_reading(dc_load_kw=0.0), valuation, NOW + timedelta(hours=1)
    )
    assert value is not None and value.degraded == ("smoothing",)
    assert value.marginal_cost == valuation.slots[0].marginal_cost


def test_a_held_battery_on_the_bus_serves_what_the_rating_keeps_from_the_grid() -> None:
    # Held, the DC-bus load's 12 kW: the grid reaches 10 through the hybrid, the battery the rest.
    flows = _session(_held_plant(), 0.5, [0.0, 12.0])
    assert flows[1].dispatch.held and flows[1].ac_to_dc_load == pytest.approx(10.0)
    assert flows[1].battery_to_dc_load == pytest.approx(2.0)
    assert flows[1].unserved == pytest.approx(0.0)
    # At its reserve it cannot: unserved, and reachable (a stored kWh would serve it).
    empty = _session(_held_plant(), 0.0, [12.0])
    assert empty[0].unserved == pytest.approx(2.0)
    assert empty[0].dispatch.unserved_unreachable == 0.0


def test_a_battery_off_the_bus_cannot_reach_what_the_rating_keeps() -> None:
    # On its own inverter it crosses the same rating: what the rating keeps from the DC-bus load
    # is unreachable, so an empty there is bought back, not the unserved floor.
    plant = Plant(
        PlantConfig(
            tz=PACIFIC,
            battery=BatteryPart(
                Battery(10.0, 20.0, 20.0, 1.0, 1.0, reserve_soc=0.1), "ac", inverter=Inverter()
            ),
            hybrid=Inverter(ac_rating_kw=3.0, conversion=LOSSLESS),
            loads=LOADS,
        )
    )
    count = 4
    flows = plant.project(
        _at(18, count), [SLOT] * count, [0.0] * count, [2.0] * count, 0.12, dc_load_kw=[5.0] * count
    )
    assert flows[0].unserved == pytest.approx(2.0)
    assert flows[0].dispatch.unserved_unreachable == pytest.approx(2.0)
    priced = price_flows(plant.storage, flows, [0.10, 0.40, 0.40, 0.40], [0.05] * count, day=count)
    assert all(event is not Event.UNSERVED for event in priced.make_up_event)


def test_a_session_under_way_holds_as_the_plant_decided_it() -> None:
    # A session that began at 70 % drains on below 65 %: told so, a projection that
    # starts in it at 60 % drains; not told, it decides by the SOC there and holds.
    plant = _held_plant()
    told = plant.project(_at(18), [SLOT], [0.0], [1.0], 0.6, dc_load_kw=[7.0], held_at_start=False)
    assert not told[0].dispatch.held and told[0].battery_to_dc_load > 0
    assert told[0].dispatch.hold_session is False
    guessed = _session(plant, 0.6, [7.0])
    assert guessed[0].dispatch.held and guessed[0].dispatch.hold_session is True
    assert _session(plant, 0.6, [0.0])[0].dispatch.hold_session is None  # no session


def test_the_engine_and_the_live_value_carry_the_sessions_state() -> None:
    # A forecast session at the slot under way is decided by the SOC there (60 %, under
    # the rule's 65 %: held), and the live value carries the issue's decision.
    engine = _engine(Plant(replace(EV_PLANT.config, dispatch=Dispatch(hold_rules=(HOLD,)))))
    drawing = received(load_forecast(load_starts(), [7.0] * 6 + _evening_kw(0.0)[6:]))
    valuation = engine.value(inputs(soc=reading(0.6, as_of=NOW), dc_load=drawing), at=NOW)
    assert valuation is not None and valuation.model is not None
    assert all(run[0].dispatch.hold_session is True for run in valuation.model.flows)
    model = valuation.model
    again = revalue(
        model, 0, NOW, measured_pv_kw=0.0, measured_load_kw=1.0, soc=0.6, measured_dc_load_kw=7.0
    )
    assert again.energy.model is not None
    assert all(run[0].dispatch.hold_session is True for run in again.energy.model.flows)


@pytest.mark.parametrize(("soc", "held"), [(0.6, True), (0.8, False)])
def test_with_no_session_forecast_the_live_value_holds_by_the_soc_now(
    soc: float, held: bool
) -> None:
    """context-v1.5: an issue takes no DC-bus input (its forecast 0, no session read),
    so no issue slot opens a session. A session the meter shows live is decided by the
    SOC now against the hold rule (65 %): the request has nothing to say about it."""
    engine = _engine(Plant(replace(EV_PLANT.config, dispatch=Dispatch(hold_rules=(HOLD,)))))
    valuation = engine.value(inputs(soc=reading(soc, as_of=NOW), dc_load=_evening_ev(0.0)), at=NOW)
    assert valuation is not None and valuation.model is not None
    assert all(flow.dispatch.hold_session is None for run in valuation.model.flows for flow in run)
    again = revalue(
        valuation.model,
        0,
        NOW,
        measured_pv_kw=0.0,
        measured_load_kw=1.0,
        soc=soc,
        measured_dc_load_kw=7.0,
    )
    assert again.energy.model is not None
    first = [run[0] for run in again.energy.model.flows]
    assert all(flow.dispatch.hold_session is held for flow in first)
    assert all(flow.dispatch.held is held for flow in first)


def test_dc_pv_the_rating_keeps_from_the_house_is_not_the_batterys_regime() -> None:
    # DC PV 10 into a 5 kW hybrid with a 5 kW house: the rest charges the battery, but a
    # kWh more on the AC side is imported.
    plant = _folded(rating=5.0)
    flows = plant.project(_at(10), [SLOT], [10.0], [5.0], 0.5)
    storage = plant.storage
    assert storage is not None and flows[0].dc_pv_to_battery > 0
    (regime,) = regimes(flows, surplus(flows), None, storage.divertible_kw)
    assert regime is Regime.IMPORT


def test_at_the_import_limit_the_battery_on_the_bus_serves_the_dc_load_as_it_is() -> None:
    # Held, a 5 kW limit, the house 2 and the DC-bus load 7: the battery gives the
    # DC-bus load 4 kW on its bus beyond its policy, the grid the house 2 and the
    # DC-bus load 3 through the hybrid.
    plant = _folded(
        pv=(), grid=GridLimits(import_limit_kw=5.0), dispatch=Dispatch(hold_rules=(HOLD,))
    )
    (flow,) = plant.project(_at(18), [SLOT], [0.0], [2.0], 0.5, dc_load_kw=[7.0])
    assert flow.battery_to_dc_load == pytest.approx(4.0)
    assert flow.ac_to_dc_load == pytest.approx(3.0)
    assert flow.battery_to_load == pytest.approx(0.0)
    assert flow.grid_to_load == pytest.approx(5.0)
    assert flow.dispatch.crossing == pytest.approx(-3.0)
    assert flow.unserved == pytest.approx(0.0)


def test_the_top_up_need_not_cover_a_session_the_battery_is_held_through() -> None:
    # The window covers until 21:00; the DC-bus load's evening session, starting low, is held,
    # so the battery need not cover it and the window buys less.
    window = GridCharge(start=time(12), end=time(15), target_soc=1.0, cover_until=time(21))
    starts = _at(12, 54)  # 12:00 to 21:00

    def bought(holds: tuple[HoldRule, ...]) -> float:
        plant = _folded(pv=(), dispatch=Dispatch(grid_charge=(window,), hold_rules=holds))
        ev = [7.0 if 30 <= k < 48 else 0.0 for k in range(54)]
        flows = plant.project(starts, [SLOT] * 54, [0.0] * 54, [0.5] * 54, 0.1, dc_load_kw=ev)
        return sum(flow.grid_to_battery for flow in flows)

    assert bought((HOLD,)) < bought(())


def test_a_run_that_cuts_the_batterys_export_pays_the_export_lost() -> None:
    # In an export window a load the battery serves exports less: its run costs the
    # export it displaces, so a higher export price costs more.
    evening = ExportWindow(time(18), time(20), power_kw=3.0, floor_soc=0.1)
    plant = _folded(pv=(), dispatch=Dispatch(export_windows=(evening,)))
    starts = _at(18, 12)

    def cost(p_export: float) -> float:
        energy = value_energy(
            plant,
            starts,
            [[0.0] * 12],
            [1.0] * 12,
            0.9,
            [0.3] * 12,
            [p_export] * 12,
            step=SLOT,
            dc_load_kw=[0.0] * 12,
        )
        assert energy.model is not None
        return rerun_cost(energy.model, 0, 0, 5.0, timedelta(minutes=30))

    assert cost(0.30) > cost(0.05)
