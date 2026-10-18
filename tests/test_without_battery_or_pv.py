"""A plant without a battery, without PV, or with neither.

- **No battery:** the plant has no storage and projects no SOC; the price rule has no
  make-up, so a kWh is spilling PV at the export it displaces or the grid's at the
  import price.
- **No PV:** the plant takes PV of 0 and no PV forecast; its surplus is 0.
- **Neither:** every kWh is the import price, the floor case.

The fallback causes follow the configuration: an input the plant does not have is
never a cause, and passing one is a mistake.
"""

from dataclasses import replace
from datetime import UTC, datetime, time, timedelta

import pytest
from hypothesis import given
from hypothesis import strategies as st

from engine_fixtures import (
    EXAMPLE_PLANT,
    EXPORT,
    IMPORT,
    NOW,
    PACIFIC,
    config_of,
    inputs,
    live_reading,
    pv_forecast,
    received,
    site_meters,
)
from legacy_dispatch import assert_matches, simulate
from microgrid.engine import (
    Engine,
    EngineConfig,
    LiveValue,
    Pricing,
    Regime,
    Served,
    Step,
    ValuationKind,
)
from microgrid.engine._live import importing_now, revalue
from microgrid.engine.extend import MODEL, Event, SlotFlow, price_flows
from microgrid.engine.series import SLOT, Forecast, Interval, Price, Quantiles, Series
from microgrid.engine.testing import assert_plant_model_contract
from microgrid.plant import Dispatch, GridCharge, GridLimits, Plant, PlantConfig, PvArray
from microgrid.plant.flows import surplus

T0 = datetime(2026, 9, 23, 7, 0, tzinfo=UTC)  # midnight PDT
HOURS = SLOT / timedelta(hours=1)
WINDOW = GridCharge(start=time(12), end=time(15), target_soc=0.65, timing="late")


def pv_only(export_limit_kw: float | None = None) -> Plant:
    return Plant(
        PlantConfig(
            tz=PACIFIC,
            pv=(PvArray("roof", "ac"),),
            grid=GridLimits(export_limit_kw=export_limit_kw),
        )
    )


def battery_only(window: GridCharge | None = None) -> Plant:
    site = config_of(EXAMPLE_PLANT)
    return Plant(
        PlantConfig(
            tz=PACIFIC,
            battery=site.battery,
            dispatch=Dispatch(grid_charge=() if window is None else (window,)),
        )
    )


def grid_only() -> Plant:
    return Plant(PlantConfig(tz=PACIFIC))


def _starts(count: int, first: datetime = T0) -> list[datetime]:
    return [first + k * SLOT for k in range(count)]


# --- The plant -------------------------------------------------------------------


@pytest.mark.parametrize(
    "plant", [pv_only(), pv_only(0.0), battery_only(), battery_only(WINDOW), grid_only()]
)
def test_every_such_plant_keeps_the_port_contract(plant: Plant) -> None:
    assert_plant_model_contract(plant)


def test_without_a_battery_pv_serves_the_house_and_the_rest_is_exported() -> None:
    starts = _starts(3)
    flows = pv_only().project(starts, [SLOT] * 3, [3.0, 0.5, 0.0], [1.0, 1.0, 0.8], None)
    assert [(f.pv_to_load, f.pv_export, f.grid_to_load) for f in flows] == [
        (1.0, 2.0, 0.0),
        (0.5, 0.0, 0.5),
        (0.0, 0.0, 0.8),
    ]
    assert all(f.soc_start is None and f.soc_end is None for f in flows)
    assert all(f.moving == 0.0 and f.inverter_draw == 0.0 for f in flows)
    # Above an export limit, the rest is curtailed.
    (limited,) = pv_only(0.5).project(starts[:1], [SLOT], [3.0], [1.0], None)
    assert (limited.pv_export, limited.pv_curtail) == (0.5, 1.5)


@given(
    st.lists(
        st.tuples(st.floats(0, 12, allow_nan=False), st.floats(0, 12, allow_nan=False)),
        min_size=1,
        max_size=36,
    ),
    st.one_of(st.none(), st.floats(0, 8, allow_nan=False)),
)
def test_a_plant_without_a_battery_balances_every_slot(
    day: list[tuple[float, float]], limit: float | None
) -> None:
    plant = pv_only(limit)
    pv, house = [d[0] for d in day], [d[1] for d in day]
    flows = plant.project(_starts(len(day)), [SLOT] * len(day), pv, house, None)
    for flow, sun, need in zip(flows, pv, house, strict=True):
        assert flow.pv_to_load + flow.pv_export + flow.pv_curtail == pytest.approx(sun)
        assert flow.pv_to_load + flow.grid_to_load == pytest.approx(need)
        assert min(flow.pv_export, flow.pv_curtail, flow.grid_to_load) >= 0
        if limit is not None:
            assert flow.pv_export <= limit
        assert flow.soc_end is None and flow.moving == 0.0


def test_without_pv_the_battery_runs_todays_dispatch_at_pv_of_zero() -> None:
    for window in (None, WINDOW):
        plant = battery_only(window)
        assert not plant.has_pv and plant.storage is not None
        native = replace(EXAMPLE_PLANT, grid_charge=window)
        starts, zeros, house = _starts(144), [0.0] * 144, [0.7] * 144
        assert_matches(
            plant.project(starts, [SLOT] * 144, zeros, house, 0.6),
            simulate(native, starts, zeros, house, 0.6, step=SLOT),
        )


def test_a_plant_refuses_what_it_does_not_have() -> None:
    starts = _starts(1)
    with pytest.raises(ValueError, match="has no battery: its SOC is None"):
        pv_only().project(starts, [SLOT], [1.0], [1.0], 0.5)
    with pytest.raises(ValueError, match="has a battery: it needs the SOC"):
        battery_only().project(starts, [SLOT], [0.0], [1.0], None)
    with pytest.raises(ValueError, match="has no PV: its PV is 0"):
        battery_only().project(starts, [SLOT], [1.0], [1.0], 0.5)
    # Each has only the meters it has: no battery meter, no PV meter.
    assert dict(pv_only().meters) == {"pv": "pv", "grid": "grid"}
    assert dict(grid_only().meters) == {"grid": "grid"}
    with pytest.raises(ValueError, match="not the plant's meters"):
        pv_only().read_meters({"pv": 1.0, "grid": 0.0, "battery": 0.5})
    with pytest.raises(ValueError, match="not the plant's meters"):
        grid_only().read_meters({"pv": 1.0, "grid": 0.0})
    with pytest.raises(ValueError, match="SOC is None exactly when"):
        pv_only().metered_flow(T0, 0.5, {"pv": 1.0, "grid": 0.0})
    with pytest.raises(ValueError, match="SOC is None exactly when"):
        battery_only().metered_flow(T0, None, {"grid": 0.0, "battery": 0.0})
    for plant in (pv_only(), grid_only()):
        assert plant.storage is None
        with pytest.raises(Exception, match="battery: the plant has none"):
            _ = plant.battery


def test_a_plant_without_a_battery_reads_its_meters_as_allocate_does_at_zero() -> None:
    # The split is ``allocate``'s with the battery's terms at 0: PV serves the house,
    # what is exported beyond it is PV, the grid serves the rest.
    plant, site = pv_only(), Plant(config_of(EXAMPLE_PLANT))
    for sun, grid in ((3.0, -1.5), (0.4, 0.9), (0.0, 1.2), (2.0, 0.0)):
        mine = plant.read_meters(site_meters(plant, sun, grid))
        theirs = site.read_meters(site_meters(site, sun, grid))
        assert mine == theirs
    flows = plant.read_meters(site_meters(plant, 3.0, -1.5))
    assert flows is not None and plant.own_draw_kw(flows) == 0.0


# --- The price rule ----------------------------------------------------------------


def _flow(
    i: int,
    *,
    pv_to_load: float = 0.0,
    pv_export: float = 0.0,
    pv_curtail: float = 0.0,
    grid_to_load: float = 0.0,
) -> SlotFlow:
    return SlotFlow(
        start=T0 + i * SLOT,
        soc_start=None,
        soc_end=None,
        pv_to_load=pv_to_load,
        pv_export=pv_export,
        pv_curtail=pv_curtail,
        grid_to_load=grid_to_load,
    )


def test_without_storage_a_kwh_is_spill_or_import() -> None:
    flows = [
        _flow(0, pv_to_load=1.0, pv_export=2.0),  # spilling: the export it displaces
        _flow(1, pv_to_load=1.0, pv_curtail=2.0),  # curtailed: 0
        _flow(2, pv_to_load=1.0),  # PV just covers the house: the kWh tips it to import
        _flow(3, grid_to_load=0.8),  # the house imports
    ]
    p_import, p_export = [0.3, 0.3, 0.4, 0.5], [0.1, 0.1, 0.1, 0.1]
    priced = price_flows(None, flows, p_import, p_export, day=4)
    assert priced.prices == (0.1, 0.0, 0.4, 0.5)
    assert priced.served == (Served.SPILL, Served.SPILL, Served.GRID, Served.GRID)
    assert priced.events == (Event.SPILL, Event.SPILL, Event.IMPORT, Event.IMPORT)
    assert priced.spilled == (True, True, False, False)
    assert priced.stored_value == (0.0,) * 4
    assert priced.make_up == priced.prices and priced.event_at == (0, 1, 2, 3)
    assert priced.resolved == (True,) * 4
    # No battery covers the expensive hours ahead: covered only where there are none.
    assert priced.covered[:3] == (False, False, False)


# --- The engine --------------------------------------------------------------------


def _engine(plant: Plant, **kw: object) -> Engine:
    return Engine(
        EngineConfig(
            plant=plant,
            pricing=Pricing(scenarios=3),
            **kw,  # type: ignore[arg-type]
        )
    )


def test_grid_only_is_the_floor_case() -> None:
    valuation = _engine(grid_only()).value(
        inputs(pv=None, soc=None, dispatch_as_modelled=None), at=NOW
    )
    assert valuation is not None
    # Valued, not the fallback; the kind keeps its name, "stored_value_v1", for a plant
    # without a battery.
    assert valuation.kind is not ValuationKind.FALLBACK_IMPORT and valuation.degraded == ()
    assert set(valuation.inputs) == {"prices", "load_forecast", "plant_model"}
    for slot in valuation.slots:
        assert slot.marginal_cost == slot.marginal_cost_p10 == IMPORT
        assert (slot.surplus_kw, slot.certainty_spill, slot.regime) == (0.0, 0.0, Regime.IMPORT)
        assert slot.pv_value is None and slot.battery_value is None
        assert slot.supply == (Step(Served.GRID, None, IMPORT),)


def test_pv_only_prices_spill_at_the_export_and_the_rest_at_the_import() -> None:
    valuation = _engine(pv_only()).value(inputs(soc=None, dispatch_as_modelled=None), at=NOW)
    assert valuation is not None
    assert valuation.degraded == ()
    assert set(valuation.inputs) == {"prices", "pv_forecast", "load_forecast", "plant_model"}
    noon, night = valuation.slots[0], valuation.slots[60]  # 12:03 and 22:00 PDT
    # At noon every scenario spills (the 5th percentile is 2.75 kW against 0.7).
    assert noon.marginal_cost == pytest.approx(EXPORT)
    assert (noon.certainty_spill, noon.regime) == (1.0, Regime.EXPORT)
    assert noon.surplus_kw == pytest.approx(5.0 - 0.7)
    assert noon.battery_value is None and noon.pv_value is not None
    assert noon.supply[0].source is Served.SPILL and noon.supply[-1].source is Served.GRID
    assert night.marginal_cost == IMPORT and night.surplus_kw == 0.0
    assert night.battery_value is None and night.pv_value is None
    assert all(slot.battery_value is None for slot in valuation.slots)


def test_pv_only_that_may_not_export_spills_at_nothing() -> None:
    valuation = _engine(pv_only(0.0)).value(inputs(soc=None, dispatch_as_modelled=None), at=NOW)
    assert valuation is not None
    noon = valuation.slots[0]
    assert (noon.marginal_cost, noon.regime) == (0.0, Regime.CURTAIL)


def test_without_pv_the_valuation_is_pv_of_zero() -> None:
    # A battery-only plant takes no PV forecast; its valuation is the same plant's with
    # a forecast of 0 (one scenario, where the forecast gives three identical ones).
    plant = battery_only()
    alone = _engine(plant).value(inputs(pv=None), at=NOW)
    assert alone is not None
    assert alone.degraded == () and "pv_forecast" not in alone.inputs
    with_pv = Plant(config_of(EXAMPLE_PLANT))
    sunny = pv_forecast()
    dark = received(
        Forecast(
            tuple(Interval(i.start, i.end, Quantiles.point(0.0)) for i in sunny.intervals),
            sunny.issued_at,
        )
    )
    zero = _engine(with_pv).value(inputs(pv=dark), at=NOW)
    assert zero is not None
    for mine, theirs in zip(alone.slots, zero.slots, strict=True):
        assert mine.surplus_kw == theirs.surplus_kw == 0.0
        assert mine.regime is theirs.regime
        assert mine.marginal_cost == pytest.approx(theirs.marginal_cost, abs=1e-12)
        assert mine.certainty_covered == pytest.approx(theirs.certainty_covered)
        assert mine.pv_value is None
        # The battery's values too: their amounts, not only that there are some.
        if theirs.battery_value is None:
            assert mine.battery_value is None
        else:
            assert mine.battery_value is not None
            assert mine.battery_value.per_kwh == pytest.approx(
                theirs.battery_value.per_kwh, abs=1e-12
            )
            assert mine.battery_value.displaces is theirs.battery_value.displaces


def test_the_causes_follow_the_plant() -> None:
    # An input the plant has is still a cause when missing.
    missing_pv = _engine(pv_only()).value(
        inputs(pv=None, soc=None, dispatch_as_modelled=None), at=NOW
    )
    assert missing_pv is not None and missing_pv.degraded == ("pv_forecast",)
    missing_soc = _engine(battery_only()).value(inputs(pv=None, soc=None), at=NOW)
    assert missing_soc is not None and missing_soc.degraded == ("battery_soc",)
    # Passing an input the plant does not have is a mistake.
    with pytest.raises(ValueError, match="no PV: pass no PV forecast"):
        _engine(battery_only()).value(inputs(), at=NOW)
    with pytest.raises(ValueError, match="no battery: pass no SOC"):
        _engine(pv_only()).value(inputs(), at=NOW)
    with pytest.raises(ValueError, match="no battery: pass no SOC"):
        _engine(pv_only()).value(inputs(soc=None), at=NOW)


# --- The live value ----------------------------------------------------------------


def _live(plant: Plant, **reading: object) -> LiveValue:
    """The live value of ``plant`` for ``reading``, against its own valuation, with no
    smoothing to wait for."""
    engine = _engine(plant, smoothing=timedelta(0))
    valued = inputs()
    if not plant.has_pv:
        valued = replace(valued, pv=None)
        reading = {"pv_kw": None, **reading}
    if plant.storage is None:
        valued = replace(valued, soc=None, dispatch_as_modelled=None)
        reading = {"soc": None, "dispatch_as_modelled": None, **reading}
    valuation = engine.value(valued, at=NOW)
    assert valuation is not None
    value = engine.live_session().update(
        live_reading(**reading), valuation, valid_until=NOW + timedelta(hours=1)
    )
    assert value is not None
    return value


def test_live_without_a_battery_spills_or_imports() -> None:
    sunny = _live(pv_only(), pv_kw=6.0, load_kw=0.7)
    assert sunny.degraded == () and sunny.regime is Regime.EXPORT
    assert sunny.marginal_cost == pytest.approx(EXPORT)
    # The grid meter alone says the house imports.
    importing = _live(pv_only(), pv_kw=0.0, load_kw=1.0, grid_kw=1.0)
    assert importing.regime is Regime.IMPORT and importing.marginal_cost == IMPORT
    # Without a battery there is no regulation to allow for: above the meters' floor
    # (0.12 kW) the house imports, where a battery with room would regulate up to 0.3.
    for grid_kw, imports in ((0.05, False), (0.2, True)):
        assert (
            importing_now(pv_only(), None, now=NOW, meters={"pv": 0.0, "grid": grid_kw}) is imports
        )
    site = Plant(config_of(EXAMPLE_PLANT))
    assert not importing_now(site, 0.6, now=NOW, meters=site_meters(site, grid=0.2))
    dark = _live(grid_only(), load_kw=1.0, grid_kw=1.0)
    assert dark.degraded == () and dark.marginal_cost == IMPORT


def test_live_refuses_a_reading_of_what_the_plant_does_not_have() -> None:
    with pytest.raises(ValueError, match="no battery: read no SOC"):
        _engine(pv_only()).live_session().update(live_reading(), None, None)
    with pytest.raises(ValueError, match=r"not the plant's meters: \['pv'\]"):
        _engine(grid_only()).live_session().update(
            live_reading(soc=None, dispatch_as_modelled=None), None, None
        )


# --- Over generated plants ----------------------------------------------------------

parts = st.tuples(st.booleans(), st.booleans(), st.one_of(st.none(), st.floats(0, 6)))
days = st.lists(
    st.tuples(st.floats(0, 10, allow_nan=False), st.floats(0, 8, allow_nan=False)),
    min_size=1,
    max_size=48,
)


def _plant(has_pv: bool, has_battery: bool, limit: float | None) -> Plant:
    site = config_of(EXAMPLE_PLANT)
    return Plant(
        PlantConfig(
            tz=PACIFIC,
            pv=site.pv if has_pv else (),
            battery=site.battery if has_battery else None,
            grid=GridLimits(export_limit_kw=limit),
        )
    )


@given(parts, days, st.floats(0, 1))
def test_every_plant_balances_and_has_no_flow_through_a_part_it_lacks(
    chosen: tuple[bool, bool, float | None], day: list[tuple[float, float]], soc: float
) -> None:
    has_pv, has_battery, limit = chosen
    plant = _plant(has_pv, has_battery, limit)
    sun = [kw if has_pv else 0.0 for kw, _ in day]
    house = [kw for _, kw in day]
    flows = plant.project(
        _starts(len(day)), [SLOT] * len(day), sun, house, soc if has_battery else None
    )
    storage = plant.storage
    capacity = storage.capacity_kwh if storage is not None else 0.0
    found = surplus(flows)
    for flow, pv_kw, need, spare in zip(flows, sun, house, found.kw, strict=True):
        stored = 0.0
        if flow.soc_start is not None and flow.soc_end is not None:
            stored = (flow.soc_end - flow.soc_start) * capacity / HOURS
        sources = pv_kw + flow.grid_to_load + flow.grid_to_battery
        sinks = (
            need
            + flow.inverter_draw
            + flow.pv_export
            + flow.pv_curtail
            + flow.conversion_loss
            + stored
        )
        assert sources == pytest.approx(sinks, abs=1e-6)
        if not has_battery:
            assert flow.soc_end is None and flow.moving == 0.0
        if not has_pv:
            assert flow.pv_to_load + flow.pv_spare == 0.0 and spare == 0.0
        if limit is not None:
            assert flow.pv_export <= limit + 1e-9


@given(days, st.one_of(st.none(), st.floats(0, 6)))
def test_without_storage_every_price_is_the_export_nothing_or_the_import(
    day: list[tuple[float, float]], limit: float | None
) -> None:
    plant = _plant(True, False, limit)
    flows = plant.project(
        _starts(len(day)), [SLOT] * len(day), [d[0] for d in day], [d[1] for d in day], None
    )
    p_import = [0.3 + 0.01 * (k % 7) for k in range(len(flows))]
    p_export = [0.05 + 0.01 * (k % 5) for k in range(len(flows))]
    priced = price_flows(None, flows, p_import, p_export, day=144)
    for k, (flow, price, served) in enumerate(
        zip(flows, priced.prices, priced.served, strict=True)
    ):
        spilled = flow.pv_export + flow.pv_curtail
        if served is Served.SPILL:
            # The export it displaces; under a binding export limit the curtailed PV
            # first, worth nothing.
            assert spilled > MODEL.spill_kw
            if flow.pv_curtail > MODEL.spill_kw:
                assert price == 0.0
            else:
                assert price == pytest.approx(flow.pv_export * p_export[k] / spilled)
        else:
            assert spilled <= MODEL.spill_kw
            assert (served, price) == (Served.GRID, p_import[k])


def test_the_guards_name_what_is_wrong() -> None:
    with pytest.raises(ValueError, match="must have the same length"):
        pv_only().project(_starts(2), [SLOT] * 2, [1.0], [1.0, 1.0], None)
    for plant in (pv_only(), grid_only()):
        with pytest.raises(Exception, match="battery: the plant has none"):
            _ = plant.inverter
    # A flow without an SOC handed to the rule for a plant with a battery is refused,
    # not read as empty.
    storage = battery_only().storage
    assert storage is not None
    soc_less = [_flow(0, grid_to_load=0.8)]
    with pytest.raises(ValueError, match="projects its SOC"):
        price_flows(storage, soc_less, [0.3], [0.1], day=1)
    with pytest.raises(ValueError, match="projects its SOC"):
        storage.headroom(soc_less[0], HOURS)


def _tou(now: datetime = NOW, hours: int = 49) -> Series[Forecast[Price]]:
    """Import at 0.30, and 0.60 from 16:00 to 21:00 local; export at 0.05."""
    first = now.replace(minute=0, second=0, microsecond=0)
    points = []
    for h in range(hours):
        start = first + timedelta(hours=h)
        peak = 16 <= start.astimezone(PACIFIC).hour < 21
        points.append(
            Interval(start, start + timedelta(hours=1), Price(0.6 if peak else 0.3, 0.05))
        )
    return received(Forecast(tuple(points), now))


def test_the_floor_case_follows_a_time_of_use_curve() -> None:
    # Grid only: every kWh at the import price of when it is used, across a price step
    # too.
    valuation = _engine(grid_only()).value(
        inputs(prices=_tou(), pv=None, soc=None, dispatch_as_modelled=None),
        at=NOW,
    )
    assert valuation is not None
    imports = [slot.p_import for slot in valuation.slots]
    assert {round(p, 2) for p in imports} == {0.3, 0.6}
    assert all(slot.marginal_cost == slot.p_import for slot in valuation.slots)


def test_a_battery_only_plant_values_the_battery() -> None:
    # No PV: the battery charges only in its grid-charge window, and gives what it
    # holds; its kWh are battery-valued, and the stack offers it.
    valuation = _engine(battery_only(WINDOW)).value(inputs(prices=_tou(), pv=None), at=NOW)
    assert valuation is not None and valuation.degraded == ()
    valued = [slot for slot in valuation.slots if slot.battery_value is not None]
    assert valued and all(slot.pv_value is None for slot in valuation.slots)
    assert any(step.source is Served.BATTERY for slot in valued for step in slot.supply)
    assert all(slot.surplus_kw == 0.0 for slot in valuation.slots)


def test_revalue_without_a_battery() -> None:
    valuation = _engine(pv_only()).value(inputs(soc=None, dispatch_as_modelled=None), at=NOW)
    assert valuation is not None and valuation.model is not None
    again = revalue(valuation.model, 0, NOW, measured_pv_kw=6.0, measured_load_kw=0.7, soc=None)
    assert again.surplus.regime is Regime.EXPORT
    assert again.surplus.kw == pytest.approx(6.0 - 0.7)
    assert again.energy.price[0] == pytest.approx(EXPORT)


def test_live_without_pv_with_a_battery() -> None:
    value = _live(battery_only(), load_kw=0.7, soc=0.6, dispatch_as_modelled=True)
    assert value.degraded == () and value.surplus_kw == 0.0
    # No PV, a battery at 60 %: it serves the house, and a kWh is the battery's.
    assert value.regime is Regime.BATTERY and value.battery_value is not None


def test_the_warm_up_without_a_battery_imports_where_the_valuation_spills() -> None:
    # Nothing measured spills yet, and there is no battery to read it as: the import.
    engine = _engine(pv_only())  # smoothing on: the first reading is a warm-up
    valuation = engine.value(inputs(soc=None, dispatch_as_modelled=None), at=NOW)
    assert valuation is not None and valuation.slots[0].regime is Regime.EXPORT
    value = engine.live_session().update(
        live_reading(soc=None, dispatch_as_modelled=None), valuation, NOW + timedelta(hours=1)
    )
    assert value is not None and value.degraded == ("smoothing",)
    assert value.regime is Regime.IMPORT
